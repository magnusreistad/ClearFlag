---
name: new-endpoint
description: Scaffold a new ClearFlag API endpoint end-to-end (backend router, schema, test, frontend API wrapper, TS type) following this repo's existing conventions. Use when adding a new backend API endpoint.
---

Follow the patterns already established in `backend/app/routers/transactions.py`: `GET /transactions` (paginated list), `GET /transactions/summary` (aggregate), and `POST /transactions/{transaction_id}/disposition` (append-only write). Their schemas are in `backend/app/schemas.py`, tests in `backend/tests/test_transactions.py`, `test_summary.py` and `test_dispositions.py`, and frontend wrappers in `frontend/src/api/transactions.ts` and `api/summary.ts`. Don't improvise a different shape — match the closest one.

## Steps

1. **Schema** (`backend/app/schemas.py`): add a Pydantic response model with `model_config = ConfigDict(from_attributes=True)` so it can validate straight from the SQLAlchemy model. Any money field must be `Decimal` (never `float`) — it will serialize as a string automatically, which the frontend must also treat as a string.

2. **Router** (`backend/app/routers/<resource>.py`, or the existing `transactions.py` for a `/transactions/...` route): required/optional query params use `Query(...)` with the same bounds style as the existing endpoints (e.g. `limit: int = Query(50, ge=1, le=200)`). Get a DB session via `Depends(get_db)` from `app.database` — never instantiate a session directly in a route. If the resource is user-scoped, require an explicit `user_id` query param — there is no auth in this project, so scoping is always explicit, never inferred from a session/token. Another user's row is a 404, same as a nonexistent one.

   **Route order matters**: Starlette matches in declaration order, so a fixed path like `/transactions/summary` must be declared before any `/transactions/{transaction_id}` GET, or `summary` is matched as an id (and 422s as a non-integer).

3. **Register the router** in `backend/app/main.py` via `app.include_router(...)` if it's a new module.

4. **Indexes**: if the new endpoint introduces a new filter/sort pattern, add a composite index in `backend/app/models.py` that matches the actual query — mirror how the existing indexes (e.g. the three on `transactions`, `ix_transaction_dispositions_transaction_latest`) each map 1:1 to a specific lookup, not generic "index everything." A model change means a migration: hand off to the `db-migration` skill.

5. **Test** (`backend/tests/test_<resource>.py`): use the shared Postgres 18 harness in `backend/tests/conftest.py` as-is — it's already wired, so add nothing to it:
   - Use `TestClient(app)` and the app's own `SessionLocal` for setup rows. Don't create a test engine, don't override `get_db`, and don't build or drop the schema: it comes from `alembic upgrade head` once per session, and every table is truncated at the start of each test.
   - Run locally with `docker compose up -d --wait && pytest` from `backend/` (needs `TEST_DATABASE_URL` in `.env`). Never point a test at Neon.
   - If the test itself changes the schema, use the `scratch_database_url` / `migrated_scratch_database_url` fixtures.
   - Cover: required-param validation (422 on missing), scoping (one user's data doesn't leak into another's results), and any bounds validation on query params.
   - Mutation-check any new gate (validation rule, constraint, 404/409 branch): break it, confirm a test fails, restore.

6. **Frontend type** (`frontend/src/types/`): mirror the new Pydantic schema's field names and types exactly.

7. **Frontend API wrapper** (`frontend/src/api/<resource>.ts`): call the existing `apiGet<T>` / `apiPost<T>` helpers from `api/client.ts` and read `VITE_SEEDED_USER_ID` the way `api/transactions.ts` does — components must never call `fetch` directly.

8. **Verify** before considering it done: `ruff check .`, `ruff format --check .` and `pytest` from `backend/`; `npm run lint`, `npm test` and `npm run build` (the only typecheck) from `frontend/`.

If the endpoint requires a schema migration, hand off to the `db-migration` skill rather than running Alembic ad hoc.
