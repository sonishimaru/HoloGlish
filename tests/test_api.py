"""検索 API（FastAPI）のテスト。"""

import os

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def client(built_db, monkeypatch):
    monkeypatch.setenv("HOLOGLISH_DB", built_db)
    from server.app import app  # HOLOGLISH_DB はリクエスト時に参照される

    return TestClient(app)


def test_search_endpoint(client):
    res = client.get("/api/search", params={"q": "おはよう"})
    assert res.status_code == 200
    data = res.json()
    assert data["total"] == 3
    assert data["results"][0]["snippet"]


def test_search_with_filters(client):
    res = client.get("/api/search", params={"q": "hello", "branch": "en"})
    assert res.json()["total"] == 2
    res = client.get("/api/search", params={"q": "hello", "branch": "jp"})
    assert res.json()["total"] == 0


def test_facets_endpoint(client):
    data = client.get("/api/facets").json()
    assert "Sakura Miko" in data["members"]
    assert set(data["branches"]) == {"jp", "en"}


def test_index_html_served(client):
    res = client.get("/")
    assert res.status_code == 200
    assert "HoloGlish" in res.text


def test_static_assets_served(client):
    assert client.get("/static/app.js").status_code == 200
    assert client.get("/static/style.css").status_code == 200


def test_empty_query_ok(client):
    res = client.get("/api/search", params={"q": ""})
    assert res.status_code == 200
    assert res.json()["total"] == 0


def test_search_sort_relevance(client):
    res = client.get("/api/search", params={"q": "おはよう", "sort": "relevance"})
    assert res.status_code == 200
    data = res.json()
    assert data["sort"] == "relevance"
    assert data["total"] == 3


def test_context_endpoint(client):
    hit = client.get("/api/search", params={"q": "おはよう", "member": "Sakura Miko"}).json()["results"][0]
    res = client.get("/api/context", params={"video_id": hit["video_id"], "start": hit["start"], "window": 2})
    assert res.status_code == 200
    data = res.json()
    assert data["video"]["member"] == "Sakura Miko"
    assert any(s["is_current"] for s in data["segments"])


def test_context_missing_video_id(client):
    # video_id は必須 → 422
    assert client.get("/api/context").status_code == 422
