---
name: db-migration
description: Safely create and apply an Alembic migration against the correct Neon Postgres branch. Use whenever backend/app/models.py changes or a schema migration is otherwise needed.
---

Migrations touch a shared, mostly-irreversible resource (a real Neon database), so this is the one workflow in this repo worth slowing down for.

## Before touching anything

1. Confirm `backend/.env`'s `DATABASE_URL` points at the Neon **dev** branch, not production. Never print `.env` or the connection string — print the **host only**, e.g. `python -c "from sqlalchemy.engine import make_url; from dotenv import dotenv_values; print(make_url(dotenv_values('.env')['DATABASE_URL']).host)"` from `backend/`. If you can't tell the branch from the host alone, ask the user to confirm via the Neon dashboard's Connection Details panel (branch selector set to **dev**) rather than assuming.
2. Confirm it's the **unpooled** connection string (the host has no `-pooler` suffix) — Neon's pooled connections are flaky with Alembic DDL. Pooled is for deployed runtime traffic only.
3. Never run any Alembic command with `DATABASE_URL` pointed at production.

## Steps

1. Make the model change in `backend/app/models.py` first.
2. From `backend/`, generate the migration: `alembic revision --autogenerate -m "<description>"`.
3. **Open and hand-review** the generated file in `backend/alembic/versions/`. Autogenerate is a starting point, not a guarantee — check specifically for:
   - Money columns as `Numeric(8, 2)`, never `Float`.
   - `nullable=False` matches intent (autogenerate sometimes gets this wrong on existing tables).
   - New/changed indexes match an actual query pattern the code uses — don't let autogenerate add or drop indexes speculatively.
   - No unintended `DROP` on unrelated columns/tables (a common autogenerate false positive after model reordering).
4. Verify against the local test database first: `docker compose up -d --wait && pytest` from `backend/`. The suite builds its schema from `alembic upgrade head`, so this exercises the new migration, and `tests/test_schema_drift.py` fails if the models and migrations disagree. If the migration needs its own test (like `test_agent_rationales_migration.py`), use the `scratch_database_url` / `migrated_scratch_database_url` fixtures.
5. Show the reviewed migration and the test result to the user. Applying it to Neon is a destructive operation on shared data: get a **separate, explicit approval** for the apply itself — approval of the ticket, the plan or the migration's contents doesn't count.
6. Apply: `alembic upgrade head`.
7. Verify: `python -m scripts.smoke_test_db`.

## If something's wrong after generating

- If the migration hasn't been applied or committed yet, edit the generated file directly and regenerate/re-review.
- If it's already been applied (even just locally), write a **new** follow-up migration instead of hand-editing the applied one — don't rewrite migration history.

This is separate from `scripts/seed_transactions.py`, which has its own independent confirmation prompt for data (not schema) changes — and a reseed needs its own separate approval too.
