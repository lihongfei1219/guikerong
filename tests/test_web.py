import pytest
from fastapi.testclient import TestClient
from test_harness import sample_catalog as catalog

from app.config import Settings
from app.main import create_app
from app.models import Catalog


@pytest.fixture
def static_site(tmp_path, monkeypatch):
    directory = tmp_path / "static"
    directory.mkdir()
    (directory / "chat.html").write_text(
        '<!doctype html><html><head><link rel="stylesheet" href="/assets/chat.css"></head>'
        '<body>匹配测试页<script src="/assets/chat.js"></script></body></html>', encoding="utf-8",
    )
    (directory / "chat.css").write_text("body { color: #123; }", encoding="utf-8")
    (directory / "chat.js").write_text("document.title = '测试';", encoding="utf-8")
    # A harmless file outside the mounted directory proves traversal is rejected.
    (tmp_path / "config.py").write_text("OUTSIDE_STATIC_SENTINEL", encoding="utf-8")
    monkeypatch.setattr("app.main.STATIC_DIR", directory)
    return directory


def test_homepage_and_assets_are_public(static_site):
    with TestClient(create_app(Settings(), catalog())) as client:
        homepage = client.get("/")
        assert homepage.status_code == 200
        assert homepage.headers["content-type"].startswith("text/html")
        assert homepage.headers["cache-control"] == "no-store"
        assert "匹配测试页" in homepage.text
        assert "web-test-key" not in homepage.text
        css = client.get("/assets/chat.css")
        assert css.status_code == 200
        assert css.headers["content-type"].startswith("text/css")
        javascript = client.get("/assets/chat.js")
        assert javascript.status_code == 200
        assert "document.title" in javascript.text
        assert homepage.headers["x-request-id"]


@pytest.mark.parametrize("path", [
    "/.env", "/config.py", "/app/main.py", "/AGENT.md",
    "/assets/.env", "/assets/config.py", "/assets/",
    "/assets/%2e%2e/config.py", "/assets/..%2fconfig.py",
    "/assets/%2e%2e%5cconfig.py", "/assets/%2e%2e/%2e%2e/.env",
])
def test_assets_cannot_expose_repository_files_or_escape_the_static_directory(static_site, path):
    with TestClient(create_app(Settings(), catalog())) as client:
        response = client.get(path)
        assert response.status_code == 404
        assert "OUTSIDE_STATIC_SENTINEL" not in response.text
        assert str(static_site.parent) not in response.text


def test_health_reports_exact_upload_limits_without_secrets_or_paths(static_site):
    settings = Settings(
        ark_api_key="private-cloud-test-key",
        ark_model="private-test-model", ark_base_url="https://private-gateway.example.test/api/v3",
        csv_path=static_site.parent / "private-catalog.csv",
        max_documents=17, max_document_bytes=2048, max_total_document_bytes=16384,
        max_characters=12345, max_body_bytes=32768,
    )
    with TestClient(create_app(settings, catalog())) as client:
        response = client.get("/health")
        assert response.status_code == 200
        assert response.json()["upload_limits"] == {
            "max_documents": 17, "max_document_bytes": 2048,
            "max_total_document_bytes": 16384, "max_characters": 12345, "max_body_bytes": 32768,
        }
        from app.cloud_matching import MATCHING_PROMPT_VERSION

        assert response.json()["matching_prompt_version"] == MATCHING_PROMPT_VERSION
        for private in (
            "private-local-test-key", "private-cloud-test-key", "private-test-model",
            "private-gateway", "private-catalog.csv", "private-rules.json", str(static_site.parent),
        ):
            assert private not in response.text


def test_page_and_limits_remain_available_if_catalog_is_degraded(static_site):
    empty = Catalog(version="empty-test", raw_record_count=0, products=[])
    with TestClient(create_app(Settings(), empty)) as client:
        assert client.get("/").status_code == 200
        health = client.get("/health")
        assert health.status_code == 503
        assert health.json()["status"] == "degraded"
        assert health.json()["upload_limits"]["max_documents"] == 100
