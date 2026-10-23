# CLAUDE.md

Guidance for Claude Code sessions in this repository. Decisions and their rationale live in the [Decision Log] (Confluence); this file links to them rather than restating them.

## Session rules (non-negotiable)

- **Secrets**: never print `.env`, any secret value, or any file that may contain one (including `.claude/settings.local.json`). To show which database is targeted, print the host only — never the connection string.
- **Destructive operations on shared data need their own explicit approval** each time, even mid-ticket: applying a migration to Neon, reseeding (`scripts.seed_transactions`), and live composition (`scripts.compose_rationales` with `INVESTIGATION_AGENT_LLM_MODE=live`). Approval for one does not cover another.
- **Don't run `scripts.compose_rationales` in mock mode against the dev database.** Mock attempts no longer consume the live retry budget (SCRUM-91), but each one appends a permanently failing `[MOCK]` row to append-only `agent_rationales`. Mock is the default, so a bare run against dev counts; use `--dry-run` to preview.

## Working conventions

- **Phase A, then a hard stop**: tickets start with a read-only investigation (no file edits), a written report, then a stop. Phase B begins only on explicit approval.
- **Mutation checks**: every new gate (validator, guard, constraint, CI step) and every new test fixture is proven by breaking what it protects, watching the test fail, then restoring.
- **File, don't fix**: issues discovered outside the ticket's scope are reported for the user to file, not fixed in place.
- Branch per ticket (`SCRUM-<n>: ...` commit messages), PRs are squash-merged, and the user merges.

## Project

ClearFlag is a fraud transparency dashboard: every flagged transaction gets a plain-language, auditable rationale. Solo portfolio project, built sprint-by-sprint against Jira `SCRUM-*` tickets. Auth is out of scope by design. The backend (`backend/`, FastAPI) and frontend (`frontend/`, Vite/React/TS) are independent apps that only meet over HTTP.

## Commands

Backend (from `backend/`; needs a `.env` — copy `.env.example`):

```bash
python -m venv venv && source venv/bin/activate
pip install -r requirements.txt -r requirements-dev.txt
uvicorn app.main:app --reload            # http://localhost:8000
ruff check . && ruff format --check .    # CI enforces both
docker compose up -d --wait && pytest    # local Postgres 18 on :5433 (TEST_DATABASE_URL)
pytest tests/test_transactions.py::test_list_transactions_pagination
alembic upgrade head                     # Neon: use the db-migration skill
python -m scripts.seed_transactions      # destructive: regenerates a user's dataset
python -m scripts.compose_rationales --dry-run   # see Session rules before a real run
```

Frontend (from `frontend/`; copy `.env.example` to `.env`): `npm run dev` (:5173), `npm run lint` (oxlint), `npm test` (vitest), `npm run build` (`tsc -b && vite build` — the only typecheck).

Once per clone: `git config core.hooksPath .githooks` (blocks committing any `.env` except `.env.example`) and `git config blame.ignoreRevsFile .git-blame-ignore-revs`.

## Architecture

The request path, in order:

1. **Rules engine** (`app/rules/`, [Rules Engine Design Doc]): four rules — `velocity`, `geographic_anomaly`, `amount_deviation`, `new_merchant_risk` — run in `engine.RULES` order over a user's full history. Velocity windows are backward-looking ([SCRUM-76]): each transaction's window ends at its own timestamp (inclusive, so same-timestamp transactions count) and never sees later ones. Each hit carries structured `values` the agent can cite.
2. **`transaction_flags`** (SCRUM-61): one row per rule hit, deleted and rebuilt wholesale by `_persist_flags` on every `GET /transactions` and `GET /transactions/summary` (a GET with a write side effect — an accepted MVP tradeoff). Flag ids don't survive a request; no timestamp column ([SCRUM-80]). The rebuild is serialized by a per-process lock.
3. **Rationales**: the feed serves the latest passing `agent_rationales` row matching the transaction's current `fact_fingerprint` + `PROMPT_VERSION`. On a miss, a failed-only row, or a stale prompt version, `_refresh_flags` falls back to the **interim formatter** (the rules' own sentences joined in rule order). The request path never calls the model and never writes `agent_rationales` ([SCRUM-53]).
4. **Dispositions** (`POST /transactions/{id}/disposition`, [SCRUM-29]): keyed to `transactions.id` (never a flag id), with a `rule_names` snapshot of the persisted flags at POST time. The current disposition is the latest row.
5. **Summary** (`GET /transactions/summary`, [SCRUM-32]): refreshes flags through the same `_persist_flags`, then aggregates in SQL; feeds the frontend `SummaryDashboard`. It must stay declared before any future `GET /transactions/{transaction_id}`, or `summary` is matched as an id.

**Append-only is a convention, not enforced by the database**: application code never updates or deletes `agent_rationales` or `transaction_dispositions` rows. The one documented exception is `scripts.seed_transactions`, which clears a user's whole dataset (dispositions included) before regenerating it.

**Investigation Agent** (`app/investigation_agent/`), run only offline by `scripts.compose_rationales`:
- Tool dispatch is deterministic ([SCRUM-51]): `plan_tool_calls` builds the `AIMessage` tool calls in code from the rules that fired; LangGraph's `ToolNode` + `tools_condition` execute them. **Not** `.bind_tools()` — the model never chooses tools. Graph: `plan_tool_calls → ToolNode → collect_evidence → compose_rationale → validate`.
- Every attempt (pass or fail) appends one `agent_rationales` row; `rationale` stays NULL on failure, while `composed_text` keeps what the model wrote for audit.
- On agent, tool or validation (guardrail) failure, the graph marks `rationale_source="interim"` and writes no servable rationale, so the feed keeps serving the interim formatter ([SCRUM-56]).
- Retries ([SCRUM-56]): `MAX_VALIDATION_ATTEMPTS` caps **validation failures only**, per (transaction, fingerprint, `PROMPT_VERSION`, `VALIDATOR_VERSION`), and counts only non-mock attempts: rows with `model_id = "mock"` never count (SCRUM-91). Composition errors (tool/model failures) don't consume that budget. Separately, the SDK retries transport errors inside one model call (`INVESTIGATION_AGENT_LLM_MAX_RETRIES`).
- `INVESTIGATION_AGENT_LLM_MODE` is `mock` (default) or `live`; live refuses to run in CI. Bump `PROMPT_VERSION` / `VALIDATOR_VERSION` on any change that could alter output or pass/fail.

## Standing conventions

- **Database**: Neon Postgres, separate dev/production branches (production stays empty until deployment). `app/database.py` raises at import if `DATABASE_URL` is unset, so Alembic and the scripts need a valid `.env`. Use the **unpooled** connection string for migrations.
- **No-auth scoping**: a single seeded `users` row; every endpoint takes an explicit `user_id`. The frontend reads it from `VITE_SEEDED_USER_ID` in `api/transactions.ts` and `api/summary.ts`.
- **Money is `Decimal`**: `Numeric(8, 2)` in the DB, `Decimal` in Pydantic, a string over JSON; the frontend parses it only at render time (`lib/format.ts`).
- **Frontend API layer**: components never call `fetch`. `api/client.ts` exports `apiGet` and `apiPost` (both throw `ApiError` on non-2xx); per-resource modules wrap them with typed params.
- **Linters**: `ruff` (lint + format) for the backend, `oxlint` (not eslint) for the frontend.

## Testing

Backend tests run against a real Postgres 18 ([SCRUM-67]) — `backend/docker-compose.yml` locally, a `postgres:18` service container in CI — never SQLite, never Neon. `tests/conftest.py` points `DATABASE_URL` at `TEST_DATABASE_URL` before the app is imported, so the app's own engine, `SessionLocal` and `get_db` are the test ones: don't add per-module engines or `dependency_overrides[get_db]`. The schema is built once per session by `alembic upgrade head` (`test_schema_drift.py` checks models match migrations), and every table is truncated at the start of each test. Schema-changing tests use `scratch_database_url` / `migrated_scratch_database_url`. `tests/no_live_guard.py` fails the run on live mode or tracing and blocks all non-loopback network; script model responses with the `scripted_llm` fixture. The empty `backend/conftest.py` only makes `backend/` the pytest rootdir.

## CI

`.github/workflows/ci.yml` runs two parallel jobs, both on `ubuntu-24.04`: backend (`ruff check`, `ruff format --check` ([SCRUM-79]), a standalone `alembic upgrade head`, `pytest` against the service container) and frontend (Node 24: oxlint, `vitest run`, `npm run build`).

## What's tracked in git

`CLAUDE.md`, `.claude/settings.json` (shared permissions plus a hook that blocks `.env` commits) and `.claude/skills/` are tracked so every clone and session gets the same guidance. `.claude/settings.local.json` stays ignored (machine-specific, may hold secrets), via the `.claude/*` + `!` exceptions in `.gitignore`. Skills: `db-migration`, `new-endpoint`, `frontend-design` (also the design notes: palette, chip colours, tokens).

## Decision Log

[Decision Log] parent page. Rationale for the above: [Rules Engine Design Doc] · [SCRUM-29] dispositions · [SCRUM-32] summary dashboard · [SCRUM-51] tool dispatch · [SCRUM-53] rationale table · [SCRUM-56] retry cap and fallback · [SCRUM-67] Postgres harness · [SCRUM-76] velocity windows · [SCRUM-79] ruff format in CI · [SCRUM-80] drop created_at.

[Decision Log]: https://magnusreistad.atlassian.net/wiki/spaces/CF/pages/2129922
[Rules Engine Design Doc]: https://magnusreistad.atlassian.net/wiki/spaces/CF/pages/2031617
[SCRUM-29]: https://magnusreistad.atlassian.net/wiki/spaces/CF/pages/36339713
[SCRUM-32]: https://magnusreistad.atlassian.net/wiki/spaces/CF/pages/38764545
[SCRUM-51]: https://magnusreistad.atlassian.net/wiki/spaces/CF/pages/31293441
[SCRUM-53]: https://magnusreistad.atlassian.net/wiki/spaces/CF/pages/31522817
[SCRUM-56]: https://magnusreistad.atlassian.net/wiki/spaces/CF/pages/31916033
[SCRUM-67]: https://magnusreistad.atlassian.net/wiki/spaces/CF/pages/32571394
[SCRUM-76]: https://magnusreistad.atlassian.net/wiki/spaces/CF/pages/36306945
[SCRUM-79]: https://magnusreistad.atlassian.net/wiki/spaces/CF/pages/36667393
[SCRUM-80]: https://magnusreistad.atlassian.net/wiki/spaces/CF/pages/37191681
