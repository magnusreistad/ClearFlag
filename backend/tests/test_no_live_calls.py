"""SCRUM-54: canaries for the no-live-calls guard (tests/no_live_guard.py,
wired up in tests/conftest.py). Each canary goes through a real client, not a
raw socket, so it proves the guard stops the libraries the app actually uses
-- and asserts LiveCallBlocked is in the exception chain, so a request that
failed for some other reason (DNS, a timeout) can't pass for a blocked one.
"""

import anthropic
import langsmith
import langsmith.utils
import pytest
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from no_live_guard import API_KEY_VARS, LiveCallBlocked, check_environment
from sqlalchemy import text

from app.database import engine
from app.investigation_agent.llm import get_chat_model


def _blocked_by_guard(exc: BaseException) -> bool:
    while exc is not None:
        if isinstance(exc, LiveCallBlocked):
            return True
        exc = exc.__cause__ or exc.__context__
    return False


def test_anthropic_client_request_is_blocked():
    client = anthropic.Anthropic(api_key="sk-test-dummy-not-real", max_retries=0)

    with pytest.raises(anthropic.APIConnectionError) as exc_info:
        client.messages.create(model="claude-sonnet-5-5", max_tokens=1, messages=[{"role": "user", "content": "hi"}])

    assert _blocked_by_guard(exc_info.value)


def test_live_mode_chat_model_is_blocked_even_with_the_ci_toggle_bypassed(monkeypatch):
    """The guard is independent of the SCRUM-55 toggle: with CI unset (so
    get_chat_model's own live-in-CI refusal can't fire) and live mode forced
    on inside this one test, the real ChatAnthropic still can't reach the API."""
    monkeypatch.delenv("CI", raising=False)
    monkeypatch.setenv("INVESTIGATION_AGENT_LLM_MODE", "live")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test-dummy-not-real")
    monkeypatch.setenv("INVESTIGATION_AGENT_LLM_MAX_RETRIES", "0")

    with pytest.raises(anthropic.APIConnectionError) as exc_info:
        get_chat_model().invoke("hi")

    assert _blocked_by_guard(exc_info.value)


def test_langsmith_client_request_is_blocked():
    client = langsmith.Client(api_key="lsv2-test-dummy-not-real", api_url="https://api.smith.langchain.com")

    with pytest.raises(langsmith.utils.LangSmithError) as exc_info:
        client.read_project(project_name="clearflag-scrum-54-canary")

    assert _blocked_by_guard(exc_info.value)


def test_langsmith_tracing_is_disabled_under_the_guard():
    assert langsmith.utils.tracing_is_enabled() is False


def test_llm_mode_resolves_to_mock_under_the_guard():
    """The suite's real environment (not a cleared one, unlike
    test_investigation_agent_llm.py) resolves to mock."""
    assert isinstance(get_chat_model(), GenericFakeChatModel)


def test_no_api_key_is_present_under_the_guard():
    import os

    assert [var for var in API_KEY_VARS if os.environ.get(var, "").strip()] == []


def test_postgres_still_works_under_the_guard():
    with engine.connect() as conn:
        assert conn.execute(text("SELECT 1")).scalar() == 1


@pytest.mark.parametrize(
    ("environ", "ci", "expect_errors", "expect_removed"),
    [
        ({"INVESTIGATION_AGENT_LLM_MODE": "mock"}, True, [], []),
        ({"INVESTIGATION_AGENT_LLM_MODE": " Live "}, False, ["INVESTIGATION_AGENT_LLM_MODE=live"], []),
        ({"LANGSMITH_TRACING": "true"}, False, ["LANGSMITH_TRACING is truthy"], []),
        ({"LANGCHAIN_TRACING_V2": "1"}, True, ["LANGCHAIN_TRACING_V2 is truthy"], []),
        ({"ANTHROPIC_API_KEY": "sk-x", "LANGSMITH_API_KEY": "ls-x"}, True,
         ["ANTHROPIC_API_KEY is set", "LANGSMITH_API_KEY is set"], []),
        ({"ANTHROPIC_API_KEY": "sk-x", "LANGCHAIN_API_KEY": "lc-x"}, False, [], ["ANTHROPIC_API_KEY", "LANGCHAIN_API_KEY"]),
        ({"ANTHROPIC_API_KEY": "", "LANGSMITH_TRACING": "false"}, True, [], []),
    ],
    ids=["ci-clean", "local-live", "local-tracing", "ci-legacy-tracing", "ci-keys-fail", "local-keys-removed", "empty-is-unset"],
)
def test_check_environment(environ, ci, expect_errors, expect_removed):
    assert check_environment(environ, ci=ci) == (expect_errors, expect_removed)
