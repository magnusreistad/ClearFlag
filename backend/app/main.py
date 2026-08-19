from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.routers import transactions

app = FastAPI(title="ClearFlag API")

# Local frontend dev server (Vite). No deployed frontend origin yet — revisit
# once the frontend has a real deployment target.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173"],
    allow_methods=["GET"],
    allow_headers=["*"],
)

app.include_router(transactions.router)


@app.get("/health")
def health_check():
    """Basic health check endpoint — confirms the API is running."""
    return {"status": "ok"}
