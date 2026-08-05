# ClearFlag

A fraud transparency dashboard that pairs every flagged transaction with a plain-language, auditable rationale — bringing the same "show your work" discipline used in financial control-testing into a consumer-facing product experience.

Built as a solo portfolio project demonstrating end-to-end product delivery: PRD, sprint execution, and a working MVP.

## Project structure

```
clearflag/
├── backend/          FastAPI service — transaction API + rules engine
│   ├── main.py
│   ├── requirements.txt
│   └── tests/
├── frontend/         React app (added in Sprint 3)
├── .github/
│   └── workflows/
│       └── ci.yml    Lint + test on every push/PR
└── README.md
```

## Status

🚧 In active development — see the [Confluence project space](#) for the full PRD, sprint plan, and decision log.

## Local development (backend)

```bash
cd backend
python -m venv venv
source venv/bin/activate   # Windows: venv\Scripts\activate
pip install -r requirements.txt
uvicorn main:app --reload
```

Visit `http://localhost:8000/health` to confirm the API is running.
