from fastapi.testclient import TestClient
from test_harness import request_body, sample_catalog

from app.config import Settings
from app.main import create_app


def test_workspace_asset_urls_change_when_asset_content_changes(tmp_path, monkeypatch):
    import re
    from pathlib import Path

    import app.main

    static = tmp_path / "static"
    static.mkdir()
    for name in ("chat.html", "chat.js", "chat.css"):
        (static / name).write_bytes((Path(app.main.STATIC_DIR) / name).read_bytes())
    monkeypatch.setattr(app.main, "STATIC_DIR", static)
    with TestClient(create_app(Settings(harness_db_path=tmp_path / "cache.sqlite3"), sample_catalog())) as client:
        before = client.get("/")
        assert before.headers["cache-control"] == "no-store"
        script = re.search(r'src="(/assets/chat.js\?v=[a-f0-9]+)"', before.text).group(1)
        assert client.get(script).status_code == 200
        (static / "chat.js").write_text("// new release", encoding="utf-8")
        after = client.get("/")
        updated = re.search(r'src="(/assets/chat.js\?v=[a-f0-9]+)"', after.text).group(1)
        assert updated != script
        assert client.get(updated).text == "// new release"


def test_only_chat_routes_and_assets_remain(tmp_path):
    with TestClient(create_app(Settings(harness_db_path=tmp_path / "chat.sqlite3"), sample_catalog())) as client:
        assert client.get("/").text == client.get("/chat").text
        assert "金融匹配助手" in client.get("/").text
        paths = client.get("/openapi.json").json()["paths"]
        assert "/api/v1/sessions/{identifier}/match/stream" in paths
        for path in ("/api/v1/financial-products/match", "/api/v1/financial-products/match/stream"):
            assert path not in paths
            assert client.post(path, json=request_body()).status_code == 404
        for path in ("/assets/app.js", "/assets/app.css", "/assets/index.html"):
            assert client.get(path).status_code == 404
        assert client.get("/api/v1/financial-products").status_code == 200
        created = client.post("/api/v1/sessions", json=request_body())
        assert created.status_code == 201
        session = created.json()
        failure = client.post(f"/api/v1/sessions/{session['id']}/match", json={"expected_version": 1})
        assert failure.status_code == 503
        assert failure.json()["error"]["code"] == "MODEL_NOT_CONFIGURED"


def test_api_body_limits_and_validation_do_not_leak_documents(tmp_path):
    settings = Settings(harness_db_path=tmp_path / "secure.sqlite3", max_body_bytes=1000)
    with TestClient(create_app(settings, sample_catalog())) as client:
        assert client.post("/api/v1/sessions", json=request_body()).status_code == 201
        assert client.post("/api/v1/sessions", content=" " * 1001).status_code == 413
        body = request_body()
        body["documents"].append(body["documents"][0])
        response = client.post("/api/v1/sessions", json=body)
        assert response.status_code == 400
        assert body["documents"][0]["content"] not in response.text
        assert response.headers["x-request-id"] == response.json()["request_id"]
