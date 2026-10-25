from tests.web.conftest import Web


def test_health_ok(web: Web) -> None:
    response = web.client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_interactive_docs_disabled(web: Web) -> None:
    for path in ("/docs", "/redoc", "/openapi.json"):
        assert web.client.get(path).status_code in {303, 404}
