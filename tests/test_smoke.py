from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)


def test_index_renders():
    r = client.get("/")
    assert r.status_code == 200
    assert "rssRadar" in r.text


def test_healthz():
    r = client.get("/healthz")
    assert r.status_code == 200
    assert r.json() == {"ok": True}
