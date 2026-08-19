from fastapi import FastAPI

from app.routers import transactions

app = FastAPI(title="ClearFlag API")

app.include_router(transactions.router)


@app.get("/health")
def health_check():
    """Basic health check endpoint — confirms the API is running."""
    return {"status": "ok"}
