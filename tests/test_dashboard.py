from fastapi.testclient import TestClient

from studizba.dashboard import app


def test_public_health_and_openapi_surface():
    client = TestClient(app)
    assert client.get("/api/health").json() == {"ok": True}
    dashboard = client.get("/").text.lower()
    assert "поиск по тексту" in dashboard
    assert "семантическ" not in dashboard
    paths = client.get("/openapi.json").json()["paths"]
    assert "/api/reviews/search" in paths
    assert "/api/progress" in paths
    assert "/api/teachers/{teacher_id}/explain/{metric}" in paths
    assert "/api/departments/rank" in paths
