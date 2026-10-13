from fastapi.testclient import TestClient

from app.main import app


def test_health_endpoint():
    with TestClient(app) as client:
        response = client.get("/api/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_index_serves_web_interface():
    with TestClient(app) as client:
        response = client.get("/")
    assert response.status_code == 200
    assert "DSM Storage Inspector" in response.text
    assert "连接群晖" in response.text
