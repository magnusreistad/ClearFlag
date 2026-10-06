"""SCRUM-53: LangSmith tracing (app.investigation_agent.tracing) is dev observability only, off
by default, and must never make a network call in CI regardless of what's in the environment --
mirrors app.investigation_agent.llm.get_chat_model's own live-mode CI guard (see
test_investigation_agent_llm.py's test_live_mode_in_ci_raises_even_with_a_key_set), applied to
tracing instead of the model call itself.
"""

from datetime import datetime, timezone
from decimal import Decimal

import langchain_core.tracers.langchain
import langsmith
import langsmith.run_trees
import pytest
from langchain_core.messages import AIMessage

from app.investigation_agent import graph as graph_module
from app.investigation_agent.state import InvestigationState
from app.investigation_agent.tracing import (
    TRACING_ENV_VARS,
    enforce_no_tracing_in_ci,
    tracing_enabled,
)


@pytest.fixture(autouse=True)
def _clean_tracing_env(monkeypatch):
    for var in (*TRACING_ENV_VARS, "CI"):
        # setenv first so monkeypatch records each var's original state (even
        # "unset") and restores it at teardown: enforce_no_tracing_in_ci()
        # writes os.environ directly, and delenv alone records nothing for a
        # var that was absent, so its writes would leak into later tests.
        monkeypatch.setenv(var, "")
        monkeypatch.delenv(var)


def test_tracing_off_by_default():
    assert tracing_enabled() is False


def test_tracing_on_when_langsmith_tracing_is_set(monkeypatch):
    monkeypatch.setenv("LANGSMITH_TRACING", "true")

    assert tracing_enabled() is True


def test_tracing_on_via_legacy_langchain_tracing_v2(monkeypatch):
    monkeypatch.setenv("LANGCHAIN_TRACING_V2", "true")

    assert tracing_enabled() is True


def test_ci_forces_tracing_off_even_when_langsmith_tracing_is_set(monkeypatch):
    monkeypatch.setenv("LANGSMITH_TRACING", "true")
    monkeypatch.setenv("CI", "true")

    assert tracing_enabled() is False


def test_ci_forces_tracing_off_even_when_legacy_var_is_set(monkeypatch):
    monkeypatch.setenv("LANGCHAIN_TRACING_V2", "true")
    monkeypatch.setenv("CI", "true")

    assert tracing_enabled() is False


def test_enforce_no_tracing_in_ci_overrides_both_env_vars_in_place(monkeypatch):
    """enforce_no_tracing_in_ci mutates os.environ directly (not just its own read of it) --
    this confirms both the current and legacy var names get forced to "false" in CI, since a
    later reader that checks either name directly (rather than through tracing_enabled) must
    also see tracing off."""
    import os

    monkeypatch.setenv("LANGSMITH_TRACING", "true")
    monkeypatch.setenv("LANGCHAIN_TRACING_V2", "true")
    monkeypatch.setenv("CI", "true")

    enforce_no_tracing_in_ci()

    assert os.environ["LANGSMITH_TRACING"] == "false"
    assert os.environ["LANGCHAIN_TRACING_V2"] == "false"


@pytest.fixture
def _langsmith_env_cache_cleared():
    import langsmith.utils

    langsmith.utils.get_env_var.cache_clear()
    yield
    # monkeypatch restores os.environ, but langsmith caches what it read --
    # clear it so a "true" read here can't leak into later tests.
    langsmith.utils.get_env_var.cache_clear()


def test_enforce_no_tracing_in_ci_turns_off_langsmiths_own_tracing_check(monkeypatch, _langsmith_env_cache_cleared):
    """SCRUM-54 finding. enforce_no_tracing_in_ci() only rewrites os.environ,
    but langsmith reads tracing env vars through an lru_cache'd get_env_var
    -- and a graph run reads them at graph.invoke time, before
    compose_rationale ever calls enforce_no_tracing_in_ci(). The first
    tracing_is_enabled() call below stands in for that earlier read."""
    import langsmith.utils

    monkeypatch.setenv("LANGSMITH_TRACING", "true")
    monkeypatch.setenv("CI", "true")
    langsmith.utils.tracing_is_enabled()

    enforce_no_tracing_in_ci()

    assert langsmith.utils.tracing_is_enabled() is False


def test_enforce_no_tracing_in_ci_is_a_noop_outside_ci(monkeypatch):
    import os

    monkeypatch.setenv("LANGSMITH_TRACING", "true")

    enforce_no_tracing_in_ci()

    assert os.environ["LANGSMITH_TRACING"] == "true"


# SCRUM-75: graph runs through the app's real entry point (graph_module.invoke ->
# invoke_investigation_graph), observed at the point a trace would leave the
# process. Every LangSmith run -- LangChainTracer's and @traceable's alike -- is
# sent via RunTree.post()/.patch(); those are spied (and no-op'd), and the
# client langchain-core's tracer would build, plus the langsmith.Client the
# wrapper patches rationale_source through, are stubbed so no real Client
# (whose background thread reaches for the network on startup) is ever made.
# test_dev_tracing_still_traces_a_graph_run is the positive control: the same
# spy has to see a trace there, so a langsmith upgrade that sends runs some
# other way fails that test instead of letting the CI-path tests pass vacuously.


class _StubLangSmithClient:
    def __init__(self, attempts, *args, **kwargs):
        self._attempts = attempts

    def update_run(self, *args, **kwargs):
        self._attempts.append("update_run")

    def __getattr__(self, name):
        return lambda *args, **kwargs: None


@pytest.fixture
def trace_attempts(monkeypatch, _langsmith_env_cache_cleared):
    attempts = []
    monkeypatch.setattr(langsmith.run_trees.RunTree, "post", lambda self, *a, **k: attempts.append("post"))
    monkeypatch.setattr(langsmith.run_trees.RunTree, "patch", lambda self, *a, **k: attempts.append("patch"))
    monkeypatch.setattr(langchain_core.tracers.langchain, "get_client", lambda: _StubLangSmithClient(attempts))
    monkeypatch.setattr(langsmith, "Client", lambda *a, **k: _StubLangSmithClient(attempts))
    return attempts


def _amount_deviation_only_state() -> InvestigationState:
    """The no-tool path (no DB access), still reaching compose_rationale's model call."""
    return {
        "transaction": {
            "id": 75,
            "user_id": 1,
            "timestamp": datetime(2026, 1, 1, tzinfo=timezone.utc),
            "merchant": "Test Merchant",
            "category": "shopping",
            "amount": Decimal("500.00"),
            "latitude": 47.6062,
            "longitude": -122.3321,
            "location_label": "Seattle, WA",
        },
        "rule_names": ["amount_deviation"],
        "rule_values": {
            "amount_deviation": {
                "amount": Decimal("500.00"),
                "category_mean": Decimal("100.00"),
                "category_stdev": Decimal("10.00"),
                "percent_above_mean": Decimal("400.00"),
            }
        },
        "evidence": {},
        "rationale": "",
        "composed_rationale": "",
    }


@pytest.mark.parametrize("tracing_var", ["LANGSMITH_TRACING", "LANGSMITH_TRACING_V2"])
def test_ci_graph_run_attempts_no_trace_even_with_langsmiths_env_cache_warm(
    monkeypatch, scripted_llm, trace_attempts, tracing_var
):
    """The bug's precondition: langsmith has already read and cached tracing
    as on (the first tracing_is_enabled() call) before the app's CI
    enforcement runs. LANGSMITH_TRACING_V2 is a name the enforcement didn't
    cover before SCRUM-75."""
    import langsmith.utils

    monkeypatch.setenv(tracing_var, "true")
    monkeypatch.setenv("CI", "true")
    assert langsmith.utils.tracing_is_enabled() is True  # cache warmed with tracing on
    llm = scripted_llm(AIMessage(content="Spending was unusually high."))

    graph_module.invoke(_amount_deviation_only_state())

    assert llm.invocations == 1  # the run really reached the model call
    assert trace_attempts == []
    assert langsmith.utils.tracing_is_enabled() is False


@pytest.mark.skipif(not hasattr(langsmith, "configure"), reason="langsmith.configure needs langsmith>=0.4.10")
def test_ci_graph_run_attempts_no_trace_when_langsmith_is_switched_on_in_process(
    monkeypatch, scripted_llm, trace_attempts
):
    """Tracing switched on through a route the env-var enforcement can't
    reach -- langsmith's own process-wide configure(enabled=True), which it
    checks before any env var. Only the tracing_context(enabled=False) around
    the run keeps CI untraced here."""
    monkeypatch.setenv("CI", "true")
    langsmith.configure(enabled=True)
    try:
        llm = scripted_llm(AIMessage(content="Spending was unusually high."))

        graph_module.invoke(_amount_deviation_only_state())
    finally:
        langsmith.configure(enabled=None)

    assert llm.invocations == 1
    assert trace_attempts == []


def test_dev_tracing_still_traces_a_graph_run(monkeypatch, scripted_llm, trace_attempts):
    """CI unset, tracing on: the run is traced exactly as before SCRUM-75."""
    import langsmith.utils

    monkeypatch.setenv("LANGSMITH_TRACING", "true")
    assert langsmith.utils.tracing_is_enabled() is True
    scripted_llm(AIMessage(content="Spending was unusually high."))

    graph_module.invoke(_amount_deviation_only_state())

    assert trace_attempts.count("post") >= 1
    assert tracing_enabled() is True
