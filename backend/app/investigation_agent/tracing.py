"""SCRUM-53 addition. LangSmith tracing for the Investigation Agent graph -- dev observability
only, enabled purely via LangSmith's own environment variables (LANGSMITH_TRACING and friends;
see backend/.env.example). Off by default. There is no persistence or audit table here and this
ticket doesn't add one -- a trace is a debugging aid, never the system of record for a rationale
or its source.

Current env var names, confirmed against the installed langsmith==0.10.17 /
langchain-core==1.5.3 (langsmith.utils.get_env_var checks the LANGSMITH_ namespace first, then
falls back to the legacy LANGCHAIN_ namespace for backward compatibility):
    LANGSMITH_TRACING       "true" to enable (legacy alias: LANGCHAIN_TRACING_V2)
    LANGSMITH_API_KEY       required to actually reach LangSmith (legacy: LANGCHAIN_API_KEY)
    LANGSMITH_PROJECT       optional, defaults to "default" (legacy: LANGCHAIN_PROJECT)
    LANGSMITH_ENDPOINT      optional, defaults to LangSmith's hosted API

SCRUM-75: CI enforcement has to happen before graph.invoke, not inside a node. langchain-core
decides whether to attach a LangSmith tracer when the root run's callback manager is configured
(at graph.invoke), and once a run is being traced every node sees langsmith.utils
.tracing_is_enabled() as True via the current run tree -- so nothing a node does can turn the
run's tracing back off. See invoke_investigation_graph.
"""

import os
import uuid
from typing import Any

import langsmith
import langsmith.utils

from app.investigation_agent.llm import current_model_id
from app.investigation_agent.prompts import PROMPT_VERSION
from app.investigation_agent.state import InvestigationState

# Every name langsmith.utils.tracing_is_enabled() reads its on/off switch from: it checks
# get_env_var("TRACING_V2") then get_env_var("TRACING"), and get_env_var tries the LANGSMITH_
# namespace before the legacy LANGCHAIN_ one. All four must be forced off in CI -- leaving any
# one of them "true" still enables tracing (SCRUM-75 found LANGSMITH_TRACING_V2 did). The
# SCRUM-54 test guard (tests/no_live_guard.py) keeps its own copy of this list, checked against
# this one by test_no_live_calls.py.
TRACING_ENV_VARS = ("LANGSMITH_TRACING_V2", "LANGCHAIN_TRACING_V2", "LANGSMITH_TRACING", "LANGCHAIN_TRACING")


def _is_truthy(value: str) -> bool:
    """Mirrors app.investigation_agent.llm.get_chat_model's live-mode CI guard exactly (same
    truthy definition), duplicated rather than imported so this module doesn't reach into
    llm.py's private helper across a module boundary for what is otherwise a one-line check.
    """
    return value.strip().lower() not in ("", "false", "0", "no")


def enforce_no_tracing_in_ci() -> None:
    """The Design Doc's "no live LLM calls in CI" (SS7) extends to tracing: a LangSmith trace
    upload is itself a network call, so CI must never make one even if any TRACING_ENV_VARS
    name is set in the environment. Called at the graph's entry point (invoke_investigation_graph)
    before graph.invoke, rather than only at import time, since a test can set/unset CI after
    this module is first imported.

    Rewriting os.environ alone isn't enough (SCRUM-75): langsmith reads these through an
    lru_cache'd get_env_var, so a "true" read earlier in the process would survive. Its cache is
    cleared here too -- via getattr, so this stays a no-op rather than an AttributeError if a
    future langsmith stops caching that function.
    """
    if _is_truthy(os.getenv("CI", "")):
        for var in TRACING_ENV_VARS:
            os.environ[var] = "false"
        cache_clear = getattr(langsmith.utils.get_env_var, "cache_clear", None)
        if cache_clear is not None:
            cache_clear()


def tracing_enabled() -> bool:
    """Whether a trace would actually be attempted right now -- CI forced off first, then either
    tracing env var, since a caller deciding whether to touch the LangSmith network client (see
    invoke_investigation_graph below) needs the same enforcement the graph run itself gets.
    """
    enforce_no_tracing_in_ci()
    return _is_truthy(os.getenv("LANGSMITH_TRACING", os.getenv("LANGCHAIN_TRACING_V2", "")))


def invoke_investigation_graph(graph: Any, state: InvestigationState) -> InvestigationState:
    """Preferred entry point for running the compiled investigation_graph (exposed as
    app.investigation_agent.graph.invoke) when trace metadata is wanted: attaches
    transaction_id, the fired rule names, PROMPT_VERSION, and the model id up front via the
    run's config (so they're present even if the run never reaches compose_rationale, e.g. the
    amount_deviation-only no-tool path), then -- only if tracing actually ends up enabled --
    patches the same run afterward with the one field that's only known once the graph
    finishes: rationale_source. A patch failure (LangSmith unreachable, bad key, ...) is
    swallowed: this is a debugging aid, so it must never affect the investigation result itself.

    This is where CI enforcement lives (SCRUM-75), before graph.invoke -- see the module
    docstring for why it can't live inside a node. enforce_no_tracing_in_ci() turns off
    langsmith's process-wide env switch; on top of that, the run itself goes through
    langsmith's public tracing_context(enabled=False) in CI, which langsmith checks before any
    env var, cache or current run tree, so it holds even for a tracing switch langsmith may read
    under a name TRACING_ENV_VARS doesn't list. Outside CI it's enabled=None (inherit), so dev
    tracing is untouched.

    Tests exercising graph mechanics directly still call investigation_graph.invoke(state)
    without this wrapper, and so get no app-level CI enforcement: they rely on the SCRUM-54 test
    guard (tests/no_live_guard.py), which refuses to start the suite with any tracing switch on
    and blocks outbound network access.
    """
    enforce_no_tracing_in_ci()

    transaction = state["transaction"]
    run_id = uuid.uuid4()
    config = {
        "run_id": run_id,
        "tags": ["investigation-agent", f"prompt-{PROMPT_VERSION}"],
        "metadata": {
            "transaction_id": transaction["id"],
            "fired_rule_names": list(state.get("rule_names", [])),
            "prompt_version": PROMPT_VERSION,
            "model_id": current_model_id(),
        },
    }

    enabled = False if _is_truthy(os.getenv("CI", "")) else None
    with langsmith.tracing_context(enabled=enabled):
        result = graph.invoke(state, config=config)

    if tracing_enabled():
        try:
            from langsmith import Client

            Client().update_run(run_id, extra={"metadata": {"rationale_source": result.get("rationale_source")}})
        except Exception:  # noqa: BLE001, S110 -- dev observability only; a trace-patch failure must never affect the result, and there's no logging infra in this project to route it to.
            pass

    return result
