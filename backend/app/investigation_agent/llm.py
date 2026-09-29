"""Chat model factory for the Investigation Agent (SCRUM-55).

get_chat_model() is the ONLY place a chat model is ever constructed for this
graph. It governs the LLM only -- tool selection stays deterministic
(app.investigation_agent.graph.plan_tool_calls) and the tools themselves keep
hitting the real DB via SessionLocal (SCRUM-48-51) regardless of this
toggle; there is no mock mode for tools, since the database a tool queries
is already swapped out per-environment (real Postgres vs. the SQLite test
engine), not something this LLM toggle should also govern.

INVESTIGATION_AGENT_LLM_MODE selects "mock" (default) or "live". Unset,
blank, or any other value never resolves to live -- mock is the fallback for
the first two, and an unrecognized value is a config error rather than a
silent guess. Live mode is additionally gated by an API key and a hard CI
check, per the Investigation Agent Design Doc SS7: no live LLM calls in CI,
ever.
"""

import os
from collections.abc import Iterator

from langchain_anthropic import ChatAnthropic
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage

# Prefixing every mock response makes it impossible to mistake canned output
# for a real rationale in a demo -- see the Design Doc's "never fabricate"
# principle, applied here to the model itself rather than tool evidence.
MOCK_RESPONSE_PREFIX = "[MOCK]"

# Current Claude API model ID for Sonnet per docs.claude.com's models
# overview (no version of langchain-anthropic ships a built-in default --
# `model` is a required field -- so this is ours to pick and override via
# env var).
DEFAULT_MODEL = "claude-sonnet-5-5"
DEFAULT_TIMEOUT_SECONDS = 30.0
DEFAULT_MAX_RETRIES = 2


def _is_truthy(value: str) -> bool:
    return value.strip().lower() not in ("", "false", "0", "no")


def current_model_id() -> str:
    """The model identifier get_chat_model() would construct right now, without actually
    constructing a client (SCRUM-53: for LangSmith trace metadata --
    app.investigation_agent.tracing -- attached to a graph run's config before the run starts,
    since a run's initial metadata can't be amended once nodes are already executing).

    Doesn't validate INVESTIGATION_AGENT_LLM_MODE the way get_chat_model() does -- an invalid
    mode still gets a label here (the mode string itself), since this is only ever used for a
    trace's human-readable tag; get_chat_model() remains the sole place that actually raises on
    a bad config.
    """
    mode = os.getenv("INVESTIGATION_AGENT_LLM_MODE", "mock").strip().lower() or "mock"
    if mode != "live":
        return mode
    return os.getenv("INVESTIGATION_AGENT_LLM_MODEL", DEFAULT_MODEL)


def get_chat_model(*, responses: Iterator[AIMessage | str] | None = None) -> BaseChatModel:
    """Build the chat model for this process, per INVESTIGATION_AGENT_LLM_MODE.

    responses: mock mode only. Lets a test inject its own scripted
    AIMessage/str sequence (SCRUM-54 will build per-rule fixtures on top of
    this); defaults to a single canned, clearly-labeled response.
    """
    mode = os.getenv("INVESTIGATION_AGENT_LLM_MODE", "mock").strip().lower()
    if not mode:
        mode = "mock"
    if mode not in ("mock", "live"):
        raise ValueError(f"Unrecognized INVESTIGATION_AGENT_LLM_MODE={mode!r}; must be 'mock' or 'live'.")

    if mode == "mock":
        if responses is None:
            responses = iter([AIMessage(content=f"{MOCK_RESPONSE_PREFIX} canned investigation rationale")])
        return GenericFakeChatModel(messages=responses)

    # mode == "live" from here on. CI is checked before the API key so a demo
    # env that's missing both fails on the harder rule first.
    if _is_truthy(os.getenv("CI", "")):
        raise RuntimeError(
            "INVESTIGATION_AGENT_LLM_MODE=live is not allowed while CI is set -- "
            "the Investigation Agent Design Doc (SS7) prohibits live LLM calls in CI."
        )

    api_key = os.getenv("ANTHROPIC_API_KEY")
    if not api_key:
        raise RuntimeError(
            "INVESTIGATION_AGENT_LLM_MODE=live requires ANTHROPIC_API_KEY to be set. "
            "Refusing to silently fall back to mock -- set the key or use mock mode."
        )

    model = os.getenv("INVESTIGATION_AGENT_LLM_MODEL", DEFAULT_MODEL)
    timeout = float(os.getenv("INVESTIGATION_AGENT_LLM_TIMEOUT_SECONDS", DEFAULT_TIMEOUT_SECONDS))
    max_retries = int(os.getenv("INVESTIGATION_AGENT_LLM_MAX_RETRIES", DEFAULT_MAX_RETRIES))

    kwargs = {"model": model, "api_key": api_key, "timeout": timeout, "max_retries": max_retries}

    # Not passed at all by default: newer model generations (e.g. the
    # DEFAULT_MODEL above) reject `temperature` outright as deprecated in
    # favor of adaptive thinking, so omitting it is what lets those models
    # construct cleanly. Only set INVESTIGATION_AGENT_LLM_TEMPERATURE for a
    # model that still accepts it -- no model-name check here, so the API
    # itself is what rejects an incompatible combination.
    temperature_raw = os.getenv("INVESTIGATION_AGENT_LLM_TEMPERATURE", "").strip()
    if temperature_raw:
        kwargs["temperature"] = float(temperature_raw)

    return ChatAnthropic(**kwargs)
