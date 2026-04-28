from fastapi.testclient import TestClient
from src.api.main import app


def test_reaches_endpoint():
    client = TestClient(app)
    response = client.get("/reaches")
    assert response.status_code == 200
    assert isinstance(response.json(), list)


def test_reach_not_found():
    client = TestClient(app)
    response = client.get("/reach/nonexistent")
    assert response.status_code == 404
