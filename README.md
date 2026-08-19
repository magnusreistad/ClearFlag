# ClearFlag

A fraud transparency dashboard that pairs every flagged transaction with a plain-language, auditable rationale — bringing the same "show your work" discipline used in financial control-testing into a consumer-facing product experience.

Built as a solo portfolio project demonstrating end-to-end product delivery: PRD, sprint execution, and a working MVP.

## Project structure

```
clearflag/
├── .github/
│   └── workflows/
│       └── ci.yml              # CI (lint + test on every push/PR)
├── backend/
│   ├── app/
│   │   ├── __init__.py
│   │   ├── database.py         # SQLAlchemy engine, session, Base
│   │   ├── main.py             # FastAPI app entrypoint
│   │   └── models.py           # User, Transaction models
│   ├── alembic/
│   │   ├── env.py               # Loads DATABASE_URL from .env, points at Base.metadata
│   │   ├── README
│   │   ├── script.py.mako       # Template for new migration files
│   │   └── versions/            # Migration scripts
│   ├── scripts/
│   │   ├── smoke_test_db.py     # Manual insert/read-back check against the DB
│   │   └── seed_transactions.py # Generates synthetic transaction data w/ planted fraud
│   ├── tests/
│   │   └── test_health.py
│   ├── .env                     # not committed — see Local development
│   ├── alembic.ini
│   ├── conftest.py              # Empty on purpose — see explanation within .py file
│   ├── requirements-dev.txt
│   └── requirements.txt
├── frontend/
│   ├── src/
│   │   ├── api/                 # Fetch wrapper + typed GET /transactions client
│   │   ├── components/
│   │   │   └── TransactionFeed/ # Fetches & renders the transaction list (SCRUM-60)
│   │   ├── lib/                 # Currency/date/category formatting helpers
│   │   ├── types/                # Shared TS types mirroring backend/app/schemas.py
│   │   ├── App.tsx
│   │   └── main.tsx
│   ├── .env.example              # VITE_API_BASE_URL, VITE_SEEDED_USER_ID
│   ├── package.json
│   └── vite.config.ts
├── .gitignore
└── README.md
```

## Status

🚧 In active development — see the [Confluence project space](#) for the full PRD, sprint plan, and decision log.

Done so far:
- Backend: `users`/`transactions` schema, synthetic 95-day dataset with planted fraud scenarios (SCRUM-12), paginated `GET /transactions` endpoint (SCRUM-13).
- Frontend: React app scaffolded (Vite + TypeScript + CSS Modules); transaction feed component renders merchant, amount, category, location, and date for the seeded user, with day grouping, pagination, and loading/empty/error states (SCRUM-60).

Not yet built: fraud-flag badges, rationale cards, the rules engine, and auth (auth is explicitly out of scope for this project — see `backend/app/models.py`).

## Local development (backend)

```bash
cd backend
python -m venv venv
source venv/bin/activate   # Windows: venv\Scripts\activate
pip install -r requirements.txt

# Create .env with DATABASE_URL set to your Neon connection string
alembic upgrade head

uvicorn app.main:app --reload
```

Visit `http://localhost:8000/health` to confirm the API is running.

## Local development (frontend)

```bash
cd frontend
npm install

# Create .env (copy .env.example) — VITE_SEEDED_USER_ID must match the
# seeded user's actual id in your target DB, not just any number
npm run dev
```

Visit `http://localhost:5173`. The backend must also be running (see above) — the dev server's origin is allow-listed in `backend/app/main.py`'s CORS config.

Run the test suite with `npm test`.

## API endpoints

`GET /transactions` — paginated list of a user's transactions, sorted newest-first.

| Query param | Required | Notes |
|---|---|---|
| `user_id` | yes | No auth yet, so the caller must pass this explicitly — see `backend/app/models.py` for why. |
| `limit` | no | Default 50, max 200. |
| `offset` | no | Default 0. |

Returns `{ items, total, limit, offset }`, where each item is `id, user_id, timestamp, merchant, category, amount, latitude, longitude, location_label` (see `backend/app/schemas.py`). `amount` is serialized as a string, not a number, to preserve `Decimal` precision.

## Environment variables

The backend requires a `.env` file in `backend/` (copy `.env.example` and fill in real values) with:

```
DATABASE_URL=postgresql://<user>:<password>@<host>/<dbname>
```

This project uses Neon Postgres with separate branches for dev and production:

- Active development, migrations, and synthetic data generation run against the **dev** branch.
- The **production** branch stays empty until deployment (Sprint 6).

Use the unpooled connection string for local dev and migrations — Neon's pooled connections can be flaky with Alembic DDL operations. Pooling is intended for the deployed app's runtime traffic, not migration-time usage.

Grab the correct connection string from the Neon dashboard's Connection Details panel — make sure the branch selector is set to **dev**, not production, before copying it for local work.

## Database setup & migrations

This project uses Alembic for schema migrations.

Apply existing migrations to a fresh environment:

```bash
cd backend
alembic upgrade head
```

After changing `app/models.py`, generate a new migration:

```bash
alembic revision --autogenerate -m "describe the change"
```

Always review the generated migration file in `alembic/versions/` before applying it — autogenerate is a starting point, not a guarantee.

Apply it:

```bash
alembic upgrade head
```

Verify the schema is working:

```bash
python -m scripts.smoke_test_db
```

This inserts a seeded user and transaction, reads them back, confirms types round-trip correctly (notably that `amount` deserializes as `Decimal`, not `float`), and cleans up after itself.

## Synthetic transaction dataset

`scripts/seed_transactions.py` generates ~95 days of realistic transaction history for the single seeded user, with a handful of deliberately planted fraud scenarios mixed in (velocity, geographic anomaly, amount deviation, new-merchant risk, and one multi-rule case) for the Sprint 2 rules engine to eventually detect. Each planted row is called out in a code comment explaining why it's there.

Run it with:

```bash
cd backend
python -m scripts.seed_transactions
```

It prompts you to confirm the target database host before touching any data — always confirm it's the **dev** Neon branch, never production. Pass `--yes` to skip the prompt (e.g. scripting/automation).

The script is idempotent: it clears the seeded user's existing transactions before regenerating, and uses a fixed random seed, so re-running it locally produces the same dataset shape instead of piling up duplicates.

## Schema overview

Two tables currently exist:

- **`users`** — minimal, single-seeded-row table for MVP. No authentication (explicitly out of scope for this project) — exists purely as a scoping anchor so queries can be written per-user from the start.
- **`transactions`** — the core table. Three composite indexes — `(user_id, timestamp)`, `(user_id, merchant)`, `(user_id, category)` — map directly to the lookup patterns the Sprint 2 rules engine will run (velocity, new-merchant detection, and amount-deviation checks, respectively).
