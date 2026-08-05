from fastapi import FastAPI

app = FastAPI(title="ClearFlag API")


@app.get("/health")
def health_check():
    """Basic health check endpoint — confirms the API is running."""
    return {"status": "ok"}
