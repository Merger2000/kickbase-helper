from fastapi.testclient import TestClient

from app.main import create_app


def test_health_ok_without_login(client):
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok", "db": "ok"}


def test_health_reports_unreachable_database(settings):
    settings.database_url = "sqlite+pysqlite:////nonexistent-dir/db.sqlite"
    with TestClient(create_app(settings), base_url="https://testserver") as client:
        response = client.get("/health")
    assert response.status_code == 503
    assert response.json()["db"] == "unreachable"
