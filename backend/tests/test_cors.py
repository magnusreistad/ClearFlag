"""SCRUM-29: the Vite dev server (http://localhost:5173) is a different
origin from the API, so the browser preflights any POST with a JSON body.
A preflight the CORS middleware rejects means the request never leaves
the browser, however correct the endpoint is."""

from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)

FRONTEND_ORIGIN = "http://localhost:5173"


def preflight(method: str, origin: str = FRONTEND_ORIGIN):
    return client.options(
        "/transactions/1/disposition",
        headers={
            "Origin": origin,
            "Access-Control-Request-Method": method,
            "Access-Control-Request-Headers": "content-type",
        },
    )


def test_preflight_for_post_from_the_frontend_succeeds():
    response = preflight("POST")

    assert response.status_code == 200
    assert response.headers["access-control-allow-origin"] == FRONTEND_ORIGIN
    assert "POST" in response.headers["access-control-allow-methods"]


def test_preflight_from_an_unknown_origin_is_rejected():
    response = preflight("POST", origin="http://evil.example")

    assert response.status_code == 400
    assert "access-control-allow-origin" not in response.headers


def test_preflight_for_a_method_the_api_doesnt_allow_is_rejected():
    response = preflight("DELETE")

    assert response.status_code == 400
