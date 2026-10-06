"""SCRUM-54: the test suite's "no live calls, ever" guard (Investigation Agent
Design Doc SS7). "Live" means anything outside the local test Postgres: a real
model API call, a LangSmith trace, or any other outbound network request.

This is enforced here, independent of the app's own SCRUM-55 mock/live toggle
and SCRUM-53 tracing switch, so a misconfigured environment fails the run
loudly instead of relying on every code path honoring those switches. Two
layers, both wired up by tests/conftest.py:

1. check_environment(), run at conftest import time (right after
   load_dotenv(), before anything imports app.*): live mode or a truthy
   tracing switch always fails the session; an API key fails it in CI and is
   blanked in os.environ (with one warning) locally, where a developer's
   backend/.env legitimately holds keys for a demo.
2. install_network_block(), applied by an autouse session fixture: every
   socket connect/resolve to a non-loopback address raises LiveCallBlocked.
   The test Postgres connection is unaffected -- psycopg2 connects through
   libpq in C, not through Python's socket module -- and so is FastAPI's
   TestClient, which never opens a socket.

Lives in its own module (not conftest.py) so test_no_live_calls.py can import
and exercise check_environment's CI branch directly.
"""

import ipaddress
import os
import socket
from collections.abc import Mapping

import pytest

LLM_MODE_VAR = "INVESTIGATION_AGENT_LLM_MODE"
TRACING_VARS = ("LANGSMITH_TRACING", "LANGCHAIN_TRACING_V2")
API_KEY_VARS = ("ANTHROPIC_API_KEY", "LANGSMITH_API_KEY", "LANGCHAIN_API_KEY")


class LiveCallBlocked(RuntimeError):
    """Raised by the network block. A RuntimeError rather than an OSError, so
    an HTTP client's own connection-error handling can't mistake it for a
    transient network failure -- both the anthropic and langsmith clients
    still wrap it, but keep it as the __cause__ (see test_no_live_calls.py)."""


def _is_truthy(value: str) -> bool:
    # Same truthy definition as app.investigation_agent.llm/tracing, which
    # this module deliberately doesn't import (it runs before any app import).
    return value.strip().lower() not in ("", "false", "0", "no")


def check_environment(environ: Mapping[str, str], *, ci: bool) -> tuple[list[str], list[str]]:
    """Returns (errors, keys_to_remove). Empty-string keys (e.g. .env.example's
    `ANTHROPIC_API_KEY=`) count as unset."""
    errors = []
    if environ.get(LLM_MODE_VAR, "").strip().lower() == "live":
        errors.append(f"{LLM_MODE_VAR}=live")
    errors.extend(f"{var} is truthy" for var in TRACING_VARS if _is_truthy(environ.get(var, "")))

    keys_set = [var for var in API_KEY_VARS if environ.get(var, "").strip()]
    if ci:
        errors.extend(f"{var} is set" for var in keys_set)
        return errors, []
    return errors, keys_set


def enforce_environment() -> None:
    """Applies check_environment to the real process environment: raises
    pytest.UsageError on any error, otherwise blanks local API keys (one
    warning naming them, never their values) and clears langsmith's cached
    env reads so nothing read earlier in the process survives."""
    errors, keys_to_remove = check_environment(os.environ, ci=_is_truthy(os.environ.get("CI", "")))
    if errors:
        raise pytest.UsageError(
            "Refusing to run the test suite with live-call configuration present "
            f"({'; '.join(errors)}). Tests never make live LLM, LangSmith or network calls "
            "(Investigation Agent Design Doc SS7) -- unset these for the test run."
        )
    # Blanked rather than deleted: app.database and alembic/env.py each call
    # load_dotenv() again on import, which would re-add a deleted key from
    # backend/.env -- but load_dotenv never overrides a variable that's
    # already set, even to "". Every reader (get_chat_model's `if not
    # api_key`, check_environment above) treats "" as unset.
    for var in keys_to_remove:
        os.environ[var] = ""
    if keys_to_remove:
        import warnings

        warnings.warn(
            f"Blanked {', '.join(keys_to_remove)} in the test process environment "
            "(no live calls in tests).",
            stacklevel=2,
        )

    import langsmith.utils

    langsmith.utils.get_env_var.cache_clear()


def _is_loopback(host) -> bool:
    if host is None:
        return True
    if isinstance(host, bytes):
        host = host.decode()
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host.split("%", 1)[0]).is_loopback
    except ValueError:
        return False  # any other hostname -- resolving it is already outbound


def _check_address(address) -> None:
    # AF_UNIX addresses are a path (str/bytes), never outbound.
    if isinstance(address, tuple) and not _is_loopback(address[0]):
        raise LiveCallBlocked(f"Outbound network access is blocked in tests (attempted {address[0]!r}).")


def install_network_block(monkeypatch: pytest.MonkeyPatch) -> None:
    real_connect = socket.socket.connect
    real_connect_ex = socket.socket.connect_ex
    real_create_connection = socket.create_connection
    real_getaddrinfo = socket.getaddrinfo

    def connect(self, address):
        _check_address(address)
        return real_connect(self, address)

    def connect_ex(self, address):
        _check_address(address)
        return real_connect_ex(self, address)

    def create_connection(address, *args, **kwargs):
        _check_address(address)
        return real_create_connection(address, *args, **kwargs)

    def getaddrinfo(host, *args, **kwargs):
        _check_address((host,))
        return real_getaddrinfo(host, *args, **kwargs)

    monkeypatch.setattr(socket.socket, "connect", connect)
    monkeypatch.setattr(socket.socket, "connect_ex", connect_ex)
    monkeypatch.setattr(socket, "create_connection", create_connection)
    monkeypatch.setattr(socket, "getaddrinfo", getaddrinfo)
