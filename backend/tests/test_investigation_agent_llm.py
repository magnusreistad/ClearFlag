import pytest
from langchain_anthropic import ChatAnthropic
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage

from app.investigation_agent.llm import MOCK_RESPONSE_PREFIX, get_chat_model


@pytest.fixture(autouse=True)
def _clean_llm_env(monkeypatch):
    """SCRUM-55: every test sets its own env vars from a blank slate, so
    a value leaking from the real process environment (or a previous test)
    can never change the outcome here.
    """
    for var in (
        "INVESTIGATION_AGENT_LLM_MODE",
        "INVESTIGATION_AGENT_LLM_MODEL",
        "INVESTIGATION_AGENT_LLM_TIMEOUT_SECONDS",
        "INVESTIGATION_AGENT_LLM_MAX_RETRIES",
        "INVESTIGATION_AGENT_LLM_TEMPERATURE",
        "ANTHROPIC_API_KEY",
        "CI",
    ):
        monkeypatch.delenv(var, raising=False)


def test_unset_mode_defaults_to_mock():
    model = get_chat_model()
    assert isinstance(model, GenericFakeChatModel)


def test_explicit_mock_mode_returns_labeled_canned_response():
    model = get_chat_model()
    result = model.invoke("is this transaction fraudulent?")
    assert result.content.startswith(MOCK_RESPONSE_PREFIX)


def test_mock_mode_accepts_injected_scripted_responses(monkeypatch):
    monkeypatch.setenv("INVESTIGATION_AGENT_LLM_MODE", "mock")
    scripted = iter([AIMessage(content=f"{MOCK_RESPONSE_PREFIX} velocity fixture response")])

    model = get_chat_model(responses=scripted)

    result = model.invoke("anything")
    assert result.content == f"{MOCK_RESPONSE_PREFIX} velocity fixture response"


@pytest.mark.parametrize("bad_mode", ["live-ish", "LIVEMODE", "production", "on"])
def test_unrecognized_mode_raises_config_error(monkeypatch, bad_mode):
    monkeypatch.setenv("INVESTIGATION_AGENT_LLM_MODE", bad_mode)

    with pytest.raises(ValueError, match="INVESTIGATION_AGENT_LLM_MODE"):
        get_chat_model()


def test_live_mode_without_api_key_raises_instead_of_falling_back_to_mock(monkeypatch):
    monkeypatch.setenv("INVESTIGATION_AGENT_LLM_MODE", "live")

    with pytest.raises(RuntimeError, match="ANTHROPIC_API_KEY"):
        get_chat_model()


def test_live_mode_in_ci_raises_even_with_a_key_set(monkeypatch):
    monkeypatch.setenv("INVESTIGATION_AGENT_LLM_MODE", "live")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test-dummy-not-real")
    monkeypatch.setenv("CI", "true")

    with pytest.raises(RuntimeError, match="CI"):
        get_chat_model()


def test_live_mode_with_key_and_no_ci_constructs_chat_anthropic(monkeypatch):
    monkeypatch.setenv("INVESTIGATION_AGENT_LLM_MODE", "live")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test-dummy-not-real")
    monkeypatch.setenv("INVESTIGATION_AGENT_LLM_MODEL", "claude-sonnet-5-5")
    monkeypatch.setenv("INVESTIGATION_AGENT_LLM_TIMEOUT_SECONDS", "45")
    monkeypatch.setenv("INVESTIGATION_AGENT_LLM_MAX_RETRIES", "3")

    model = get_chat_model()

    assert isinstance(model, ChatAnthropic)
    assert model.model == "claude-sonnet-5-5"
    # Not set by default: newer model generations (like the default above)
    # reject `temperature` outright as deprecated, so it must not be passed
    # unless INVESTIGATION_AGENT_LLM_TEMPERATURE is explicitly set.
    assert model.temperature is None
    assert model.default_request_timeout == 45
    assert model.max_retries == 3


def test_live_mode_passes_temperature_through_only_when_explicitly_set(monkeypatch):
    monkeypatch.setenv("INVESTIGATION_AGENT_LLM_MODE", "live")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test-dummy-not-real")
    monkeypatch.setenv("INVESTIGATION_AGENT_LLM_TEMPERATURE", "0")

    model = get_chat_model()

    assert isinstance(model, ChatAnthropic)
    assert model.temperature == 0
