"""Integration checks for real FrameX registration and Mindforge resources."""

from pathlib import Path

from fastapi.testclient import TestClient
from framex.consts import OPENAPI_URL
from framex.plugin import get_http_plugin_apis

from guikerong_finance.application import create_app
from guikerong_finance.config import Settings
from guikerong_finance.prompts.loader import prompt_text, templates
from guikerong_finance.service.runtime import get_mindforge
from tests.test_harness import request_body, sample_catalog


def test_framex_registers_all_business_routes_and_keeps_raw_http_contract(tmp_path):
    app = create_app(Settings(harness_db_path=tmp_path / "events.sqlite3"), sample_catalog())
    with TestClient(app) as client:
        apis = {api.api: api for api in get_http_plugin_apis()}
        assert len(apis) == 17
        assert set(apis["/api/v1/sessions"].methods) == {"GET", "POST"}
        assert set(apis["/api/v1/sessions/{identifier}"].methods) == {"GET", "DELETE"}
        assert all(api.raw_response for api in apis.values())
        paths = client.get(OPENAPI_URL).json()["paths"]
        assert set(apis) <= paths.keys()
        schema = paths["/api/v1/sessions"]["post"]["requestBody"]["content"]["application/json"]["schema"]
        assert "company" in schema["properties"]
        assert "documents" in schema["required"]
        created = client.post("/api/v1/sessions", json=request_body())
        assert created.status_code == 201
        assert "data" not in created.json()
        assert client.post("/api/v1/sessions", json={}).status_code == 400
        assert client.get("/api/v1/sessions").status_code == 200


def test_app_instances_do_not_share_session_state(tmp_path):
    first = create_app(Settings(harness_db_path=tmp_path / "first.sqlite3"), sample_catalog())
    second = create_app(Settings(harness_db_path=tmp_path / "second.sqlite3"), sample_catalog())
    with TestClient(first) as left, TestClient(second) as right:
        left.post("/api/v1/sessions", json=request_body())
        assert len(left.get("/api/v1/sessions").json()["sessions"]) == 1
        assert right.get("/api/v1/sessions").json()["sessions"] == []


def test_mindforge_reuses_provider_across_application_settings_and_renders_prompts():
    from llama_index.core.llms import MessageRole

    first = get_mindforge(Settings(ark_api_key="test-provider-key", ark_model="test-model"))
    second = get_mindforge(
        Settings(ark_api_key="test-provider-key", ark_model="test-model", harness_db_path=Path("other"))
    )
    assert first is second
    for tag in templates():
        template = first.get_prompt(tag)
        assert template.binded_source == "finance"
        assert template.binded_model == "matching"
        rendered = template.load_prompt_message(role=MessageRole.SYSTEM, context_length=2_000_000)
        assert rendered.content == prompt_text(tag)


def test_yaml_settings_and_provider_switch_do_not_mix_credentials(tmp_path, monkeypatch):
    path = tmp_path / "config.yaml"
    path.write_text(
        "finance_provider: ark\nsources:\n  finance:\n    api_key: yaml-test-key\n"
        "    models: {matching: yaml-model}\n    timeout: 15\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("GUIKERONG_MINDFORGE_CONFIG", str(path))
    for name in (
        "GUIKERONG_MATCHING_MODE",
        "ARK_API_KEY",
        "ARK_MODEL",
        "LLM_DEEPSEEK_FLASH_API_KEY",
        "LLM_DEEPSEEK_FLASH",
        "DEEPSEEK_API_KEY",
    ):
        monkeypatch.delenv(name, raising=False)
    config = Settings.from_env()
    assert config.ark_api_key == "yaml-test-key"
    assert config.ark_model == "yaml-model"
    monkeypatch.setenv("GUIKERONG_MATCHING_MODE", "deepseek")
    assert Settings.from_env().ark_api_key is None
    monkeypatch.setenv("GUIKERONG_MATCHING_MODE", "ark")
    monkeypatch.setenv("ARK_API_KEY", "")
    assert Settings.from_env().ark_api_key is None
