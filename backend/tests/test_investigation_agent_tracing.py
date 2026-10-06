"""SCRUM-53: LangSmith tracing (app.investigation_agent.tracing) is dev observability only, off
by default, and must never make a network call in CI regardless of what's in the environment --
mirrors app.investigation_agent.llm.get_chat_model's own live-mode CI guard (see
test_investigation_agent_llm.py's test_live_mode_in_ci_raises_even_with_a_key_set), applied to
tracing instead of the model call itself.
"""

import pytest

from app.investigation_agent.tracing import enforce_no_tracing_in_ci, tracing_enabled


@pytest.fixture(autouse=True)
def _clean_tracing_env(monkeypatch):
    for var in ("LANGSMITH_TRACING", "LANGCHAIN_TRACING_V2", "CI"):
        monkeypatch.delenv(var, raising=False)


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


@pytest.mark.xfail(strict=True, reason="SCRUM-75: langsmith env cache; enforcement runs after graph.invoke")
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
