from fastapi.testclient import TestClient

from app.main import app


def test_health_reports_graph_contents_for_the_startup_banner(
    graph_available: str,
) -> None:
    # `make up` polls this endpoint and prints its fields as the "is it
    # ready?" banner. If the shape drifts, the banner reports an unloaded
    # graph and AI features as off while the app is actually fine — the
    # reviewer can't tell a broken stack from a working one.
    with TestClient(app) as client:
        response = client.get("/api/health")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["neo4j"] == "connected"
    assert body["exercises"] == 50
    assert body["members"] == 1
    assert isinstance(body["ai_enabled"], bool)
    assert body["model"]
