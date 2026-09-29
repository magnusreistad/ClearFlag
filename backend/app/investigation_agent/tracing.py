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
"""

import os
import uuid
from typing import Any

from app.investigation_agent.llm import current_model_id
from app.investigation_agent.prompts import PROMPT_VERSION
from app.investigation_agent.state import InvestigationState

# Both namespaces a "tracing on" switch can live in -- see the module docstring. Both must be
# forced off in CI, not just one, since langsmith's own lookup falls back from LANGSMITH_ to
# LANGCHAIN_ (see langsmith.utils.get_env_var) -- leaving the legacy var set would still enable
# tracing even with the current one forced off.
_TRACING_ENV_VARS = ("LANGSMITH_TRACING", "LANGCHAIN_TRACING_V2")


def _is_truthy(value: str) -> bool:
    """Mirrors app.investigation_agent.llm.get_chat_model's live-mode CI guard exactly (same
    truthy definition), duplicated rather than imported so this module doesn't reach into
    llm.py's private helper across a module boundary for what is otherwise a one-line check.
    """
    return value.strip().lower() not in ("", "false", "0", "no")


def enforce_no_tracing_in_ci() -> None:
    """The Design Doc's "no live LLM calls in CI" (SS7) extends to tracing: a LangSmith trace
    upload is itself a network call, so CI must never make one even if LANGSMITH_TRACING (or
    the legacy LANGCHAIN_TRACING_V2) is set in the environment. Call this before any code path
    that could start a trace -- compose_rationale's model call, today -- rather than only at
    import time, since a test can set/unset CI after this module is first imported.
    """
    if _is_truthy(os.getenv("CI", "")):
        for var in _TRACING_ENV_VARS:
            os.environ[var] = "false"


def tracing_enabled() -> bool:
    """Whether a trace would actually be attempted right now -- CI forced off first, then either
    tracing env var, since a caller deciding whether to touch the LangSmith network client (see
    invoke_investigation_graph below) needs the same enforcement compose_rationale already
    applies to the model call itself.
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

    Tests exercising graph mechanics directly still call investigation_graph.invoke(state)
    without this wrapper -- compose_rationale's own enforce_no_tracing_in_ci() call is what
    actually guarantees no network call happens in CI either way.
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

    result = graph.invoke(state, config=config)

    if tracing_enabled():
        try:
            from langsmith import Client

            Client().update_run(run_id, extra={"metadata": {"rationale_source": result.get("rationale_source")}})
        except Exception:  # noqa: BLE001, S110 -- dev observability only; a trace-patch failure must never affect the result, and there's no logging infra in this project to route it to.
            pass

    return result
