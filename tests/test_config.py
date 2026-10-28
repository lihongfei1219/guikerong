from collections.abc import Callable
from dataclasses import replace

import pytest

from guikerong_finance.config import Settings
from guikerong_finance.errors import DomainError

ENVIRONMENT_KEYS = (
    "GUIKERONG_CSV_PATH",
    "GUIKERONG_RULES_PATH",
    "GUIKERONG_API_KEY",
    "GUIKERONG_ENV",
    "GUIKERONG_MATCHING_MODE",
    "GUIKERONG_MATCH_TIMEOUT_SECONDS",
    "ARK_API_KEY",
    "ARK_MODEL",
    "ARK_BASE_URL",
    "ARK_TIMEOUT_SECONDS",
    "ARK_MAX_RETRIES",
    "ARK_CONCURRENCY",
    "ARK_MAX_COMPLETION_TOKENS",
    "ARK_MAX_PROMPT_CHARACTERS",
    "ARK_THINKING",
    "ARK_MAX_PRODUCTS",
    "LLM_DEEPSEEK_FLASH_API_KEY",
    "LLM_DEEPSEEK_FLASH_BASE_URL",
    "LLM_DEEPSEEK_FLASH",
    "DEEPSEEK_API_KEY",
    "DEEPSEEK_MODEL",
    "DEEPSEEK_BASE_URL",
    "DEEPSEEK_TIMEOUT_SECONDS",
    "DEEPSEEK_MAX_RETRIES",
    "DEEPSEEK_CONCURRENCY",
    "DEEPSEEK_MAX_TOKENS",
    "DEEPSEEK_MAX_PROMPT_CHARACTERS",
    "DEEPSEEK_THINKING",
    "DEEPSEEK_MAX_PRODUCTS",
)


@pytest.fixture(autouse=True)
def isolated_environment(monkeypatch):
    for name in ENVIRONMENT_KEYS:
        monkeypatch.delenv(name, raising=False)


def test_defaults_allow_startup_without_model_credentials():
    settings = Settings.from_env()
    assert settings.matching_mode == "ark"
    assert settings.timeout_seconds == 300
    assert settings.ark_concurrency == 8
    assert settings.ark_api_key is None
    assert settings.ark_model == ""
    assert settings.ark_base_url == "https://ark.cn-beijing.volces.com/api/v3"
    assert settings.ark_max_prompt_characters == 200_000
    settings.validate()
    assert settings == Settings()


def test_ark_environment_loads_all_options_and_default_total_budget(monkeypatch):
    values = {
        "GUIKERONG_MATCHING_MODE": "ark",
        "ARK_API_KEY": "not-a-real-key",
        "ARK_MODEL": "ep-test",
        "ARK_BASE_URL": "https://gateway.example.test/api/v3",
        "ARK_TIMEOUT_SECONDS": "42.5",
        "ARK_MAX_RETRIES": "2",
        "ARK_CONCURRENCY": "4",
        "ARK_MAX_COMPLETION_TOKENS": "8192",
        "ARK_MAX_PROMPT_CHARACTERS": "200000",
        "ARK_THINKING": "omit",
        "ARK_MAX_PRODUCTS": "50",
    }
    for name, value in values.items():
        monkeypatch.setenv(name, value)
    settings = Settings.from_env()
    assert settings.timeout_seconds == 300
    assert settings.ark_api_key == "not-a-real-key"
    assert settings.ark_model == "ep-test"
    assert settings.ark_timeout_seconds == 42.5
    assert settings.ark_max_retries == 2
    assert settings.ark_concurrency == 4
    assert settings.ark_max_completion_tokens == 8192
    assert settings.ark_max_prompt_characters == 200000
    assert settings.ark_thinking == "omit"
    assert settings.ark_max_products == 50
    monkeypatch.setenv("GUIKERONG_MATCH_TIMEOUT_SECONDS", "123")
    assert Settings.from_env().timeout_seconds == 123


@pytest.mark.parametrize("mode", ["ark", "deepseek"])
def test_yaml_alone_configures_model_and_tuning(tmp_path, monkeypatch, mode):
    path = tmp_path / "config.yaml"
    path.write_text(
        f"finance_provider: {mode}\n"
        "finance: {environment: production, match_timeout_seconds: 120}\n"
        "sources:\n"
        "  finance:\n"
        "    api_key: yaml-test-key\n"
        "    base_url: https://api.deepseek.com\n"
        "    models: {matching: test-model}\n"
        "    timeout: 42.5\n"
        "    max_retries: 2\n"
        "    concurrency: 3\n"
        "    max_completion_tokens: 8192\n"
        "    max_prompt_characters: 123456\n"
        "    thinking: omit\n"
        "    max_products: 40\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("GUIKERONG_MINDFORGE_CONFIG", str(path))
    settings = Settings.from_env()
    settings.validate_model()
    assert settings.environment == "production"
    assert settings.timeout_seconds == 120
    assert settings.matching_mode == mode
    assert settings.ark_api_key == "yaml-test-key"
    assert settings.ark_model == "test-model"
    assert settings.ark_base_url == "https://api.deepseek.com"
    assert settings.ark_timeout_seconds == 42.5
    assert settings.ark_max_retries == 2
    assert settings.ark_concurrency == 3
    assert settings.ark_max_completion_tokens == 8192
    assert settings.ark_max_prompt_characters == 123456
    assert settings.ark_thinking == "omit"
    assert settings.ark_max_products == 40
    monkeypatch.setenv(f"{mode.upper()}_CONCURRENCY", "5")
    assert Settings.from_env().ark_concurrency == 5
    monkeypatch.delenv(f"{mode.upper()}_CONCURRENCY")
    monkeypatch.setenv("GUIKERONG_MATCHING_MODE", "deepseek" if mode == "ark" else "ark")
    switched = Settings.from_env()
    assert switched.ark_api_key is None
    assert switched.ark_concurrency == 8
    assert switched.ark_max_retries == 1


def test_yaml_tuning_uses_existing_validation(tmp_path, monkeypatch):
    path = tmp_path / "config.yaml"
    path.write_text("sources: {finance: {concurrency: 0}}\n", encoding="utf-8")
    monkeypatch.setenv("GUIKERONG_MINDFORGE_CONFIG", str(path))
    with pytest.raises(ValueError, match="ARK_CONCURRENCY"):
        Settings.from_env()


@pytest.mark.parametrize("missing", ["ARK_API_KEY", "ARK_MODEL"])
def test_model_call_requires_credentials_but_environment_can_load(monkeypatch, missing):
    monkeypatch.setenv("GUIKERONG_MATCHING_MODE", "ark")
    monkeypatch.setenv("ARK_API_KEY", "secret-value")
    monkeypatch.setenv("ARK_MODEL", "ep-test")
    monkeypatch.delenv(missing)
    settings = Settings.from_env()
    settings.validate()
    with pytest.raises(DomainError) as caught:
        settings.validate_model()
    assert caught.value.code == "MODEL_NOT_CONFIGURED"
    assert caught.value.status_code == 503
    assert missing in str(caught.value)
    assert "secret-value" not in str(caught.value)


@pytest.mark.parametrize("from_environment", [False, True])
def test_removed_local_mode_reports_migration_instruction(monkeypatch, from_environment):
    validate: Callable[[], object]
    if from_environment:
        monkeypatch.setenv("GUIKERONG_MATCHING_MODE", "local")
        validate = Settings.from_env
    else:
        validate = Settings(matching_mode="local").validate
    with pytest.raises(ValueError, match="本地匹配模式已删除.*GUIKERONG_MATCHING_MODE=ark"):
        validate()


@pytest.mark.parametrize(("api_key", "model"), [(None, ""), ("", "ep-test"), ("valid-key", "")])
def test_missing_direct_model_configuration_is_deferred_until_call(api_key, model):
    settings = Settings(ark_api_key=api_key, ark_model=model)
    settings.validate()
    with pytest.raises(DomainError) as caught:
        settings.validate_model()
    assert caught.value.code == "MODEL_NOT_CONFIGURED"
    assert caught.value.status_code == 503
    assert "valid-key" not in str(caught.value)


def test_model_validation_checks_other_settings_before_missing_credentials():
    with pytest.raises(ValueError, match="ARK_CONCURRENCY"):
        Settings(ark_concurrency=33).validate_model()


@pytest.mark.parametrize(
    "url",
    [
        "http://example.test/api/v3",
        "https://user:secret@example.test/api/v3",
        "https://example.test/api/v3?key=secret",
        "https://example.test/api/v3#secret",
        "https://example.test/api/../v3",
        "https://example.test/api//v3",
        "https://example.test/api/%2e%2e/v3",
        "https://example.test:bad/api/v3",
        "https://example.test:0/api/v3",
        "https://example.test\\evil/api/v3",
        "https:///api/v3",
        "https://example.test/api/v3\nsecret",
    ],
)
def test_explicit_settings_reject_unsafe_base_urls_without_leaking_values(url):
    with pytest.raises(ValueError, match=".+") as caught:
        Settings(ark_base_url=url).validate()
    assert "secret" not in str(caught.value)


@pytest.mark.parametrize(
    "changes",
    [
        {"matching_mode": "other"},
        {"ark_thinking": "automatic"},
        {"ark_max_retries": -1},
        {"ark_max_retries": 6},
        {"ark_concurrency": 0},
        {"ark_concurrency": 33},
        {"ark_concurrency": True},
        {"ark_max_completion_tokens": 0},
        {"ark_max_prompt_characters": 0},
        {"ark_max_products": 0},
        {"ark_timeout_seconds": float("nan")},
        {"ark_timeout_seconds": float("inf")},
        {"ark_timeout_seconds": 0},
        {"timeout_seconds": -1},
        {"timeout_seconds": True},
        {"ark_api_key": "key\r\nheader"},
        {"ark_model": "ep test"},
    ],
)
def test_validate_checks_direct_configuration(changes):
    settings = replace(Settings(), **changes)
    with pytest.raises(ValueError, match=".+"):
        settings.validate()


def test_constructor_stays_side_effect_free_and_does_not_auto_validate():
    assert Settings(timeout_seconds=-1).timeout_seconds == -1


def test_settings_repr_hides_model_key():
    settings = Settings(ark_api_key="cloud-secret")
    assert "cloud-secret" not in repr(settings)


def test_invalid_env_numeric_value_is_not_echoed(monkeypatch):
    monkeypatch.setenv("ARK_MAX_RETRIES", "sensitive-not-a-number")
    with pytest.raises(ValueError, match=".+") as caught:
        Settings.from_env()
    assert "ARK_MAX_RETRIES" in str(caught.value)
    assert "sensitive-not-a-number" not in str(caught.value)


def test_production_ignores_retired_access_token_configuration(monkeypatch):
    monkeypatch.setenv("GUIKERONG_ENV", "production")
    monkeypatch.setenv("GUIKERONG_API_KEY", "retired-access-key")
    monkeypatch.setenv("GUIKERONG_REVIEWER_API_KEY", "retired-reviewer-key")
    settings = Settings.from_env()
    assert settings.environment == "production"
    assert not hasattr(settings, "api_key")
    assert not hasattr(settings, "reviewer_api_key")


def set_aliases(monkeypatch):
    monkeypatch.setenv("LLM_DEEPSEEK_FLASH_API_KEY", "fake-alias-key")
    monkeypatch.setenv("LLM_DEEPSEEK_FLASH_BASE_URL", "https://alias.example.test/api/v3")
    monkeypatch.setenv("LLM_DEEPSEEK_FLASH", "fake-alias-model")


def test_ark_aliases_populate_provider_configuration(monkeypatch):
    set_aliases(monkeypatch)
    monkeypatch.setenv("GUIKERONG_MATCHING_MODE", "ark")
    settings = Settings.from_env()
    assert settings.ark_api_key == "fake-alias-key"
    assert settings.ark_base_url == "https://alias.example.test/api/v3"
    assert settings.ark_model == "fake-alias-model"
    assert settings.matching_mode == "ark"
    assert settings.timeout_seconds == 300


def test_primary_provider_variables_take_precedence_over_aliases(monkeypatch):
    set_aliases(monkeypatch)
    monkeypatch.setenv("GUIKERONG_MATCHING_MODE", "ark")
    monkeypatch.setenv("ARK_API_KEY", "fake-primary-key")
    monkeypatch.setenv("ARK_BASE_URL", "https://primary.example.test/api/v3")
    monkeypatch.setenv("ARK_MODEL", "fake-primary-model")
    settings = Settings.from_env()
    assert settings.ark_api_key == "fake-primary-key"
    assert settings.ark_base_url == "https://primary.example.test/api/v3"
    assert settings.ark_model == "fake-primary-model"


@pytest.mark.parametrize("name", ["ARK_API_KEY", "ARK_BASE_URL", "ARK_MODEL"])
@pytest.mark.parametrize("blank", ["", "  \t "])
def test_explicit_blank_primary_never_falls_back_to_alias(monkeypatch, name, blank):
    caught: pytest.ExceptionInfo
    set_aliases(monkeypatch)
    monkeypatch.setenv("GUIKERONG_MATCHING_MODE", "ark")
    monkeypatch.setenv(name, blank)
    if name == "ARK_BASE_URL":
        with pytest.raises(ValueError, match=".+") as caught:
            Settings.from_env()
    else:
        settings = Settings.from_env()
        assert getattr(settings, name.lower()) in (None, "")
        with pytest.raises(DomainError) as caught:
            settings.validate_model()
        assert caught.value.code == "MODEL_NOT_CONFIGURED"
    assert name in str(caught.value)
    assert "fake-alias-key" not in str(caught.value)


@pytest.mark.parametrize("use_primary", [False, True])
def test_provider_environment_values_trim_only_outer_whitespace(monkeypatch, use_primary):
    monkeypatch.setenv("GUIKERONG_MATCHING_MODE", "ark")
    keys = (
        ("ARK_API_KEY", "ARK_BASE_URL", "ARK_MODEL")
        if use_primary
        else ("LLM_DEEPSEEK_FLASH_API_KEY", "LLM_DEEPSEEK_FLASH_BASE_URL", "LLM_DEEPSEEK_FLASH")
    )
    for name, value in zip(
        keys, ("fake-trim-key", "https://trim.example.test/api/v3", "fake-trim-model"), strict=False
    ):
        monkeypatch.setenv(name, " \t" + value + "  \n")
    settings = Settings.from_env()
    assert settings.ark_api_key == "fake-trim-key"
    assert settings.ark_base_url == "https://trim.example.test/api/v3"
    assert settings.ark_model == "fake-trim-model"


@pytest.mark.parametrize(
    ("alias", "value"),
    [
        ("LLM_DEEPSEEK_FLASH_API_KEY", "fake key"),
        ("LLM_DEEPSEEK_FLASH_BASE_URL", "https://invalid .example.test/api/v3"),
        ("LLM_DEEPSEEK_FLASH", "fake model"),
    ],
)
def test_alias_internal_whitespace_still_fails_validation(monkeypatch, alias, value):
    set_aliases(monkeypatch)
    monkeypatch.setenv("GUIKERONG_MATCHING_MODE", "ark")
    monkeypatch.setenv(alias, value)
    with pytest.raises(ValueError, match=".+"):
        Settings.from_env()


@pytest.mark.parametrize(
    ("missing", "canonical"),
    [
        ("LLM_DEEPSEEK_FLASH_API_KEY", "ARK_API_KEY"),
        ("LLM_DEEPSEEK_FLASH", "ARK_MODEL"),
    ],
)
def test_missing_required_alias_is_reported_before_model_call(monkeypatch, missing, canonical):
    set_aliases(monkeypatch)
    monkeypatch.setenv("GUIKERONG_MATCHING_MODE", "ark")
    monkeypatch.delenv(missing)
    settings = Settings.from_env()
    with pytest.raises(DomainError, match=canonical) as caught:
        settings.validate_model()
    assert caught.value.code == "MODEL_NOT_CONFIGURED"


def test_alias_credentials_work_with_default_ark_mode(monkeypatch):
    set_aliases(monkeypatch)
    settings = Settings.from_env()
    assert settings.matching_mode == "ark"
    assert settings.timeout_seconds == 300
    settings.validate_model()


def test_deepseek_defaults_do_not_read_ark_or_legacy_aliases(monkeypatch):
    set_aliases(monkeypatch)
    for name, value in {
        "GUIKERONG_MATCHING_MODE": "deepseek",
        "ARK_API_KEY": "fake-ark-key",
        "ARK_MODEL": "fake-ark-model",
        "ARK_BASE_URL": "https://ark.example.test/v3",
        "ARK_TIMEOUT_SECONDS": "not-a-number",
        "ARK_MAX_RETRIES": "not-a-number",
        "ARK_CONCURRENCY": "not-a-number",
        "ARK_MAX_COMPLETION_TOKENS": "not-a-number",
        "ARK_MAX_PROMPT_CHARACTERS": "not-a-number",
        "ARK_THINKING": "invalid",
        "ARK_MAX_PRODUCTS": "not-a-number",
    }.items():
        monkeypatch.setenv(name, value)
    settings = Settings.from_env()
    assert settings.matching_mode == "deepseek"
    assert settings.model_provider == "deepseek"
    assert settings.ark_api_key is None
    assert settings.ark_model == "deepseek-flash"
    assert settings.ark_base_url == "https://api.deepseek.com"
    assert settings.ark_timeout_seconds == 60
    assert settings.ark_max_retries == 1
    assert settings.ark_concurrency == 8
    assert settings.ark_max_completion_tokens == 4096
    assert settings.ark_max_prompt_characters == 200_000
    assert settings.ark_thinking == "disabled"
    assert settings.ark_max_products == 100
    with pytest.raises(DomainError, match="DEEPSEEK_API_KEY") as caught:
        settings.validate_model()
    assert caught.value.code == "MODEL_NOT_CONFIGURED"
    assert "ARK_" not in str(caught.value)
    assert "fake-ark-key" not in str(caught.value)
    assert "fake-alias-key" not in str(caught.value)


def test_deepseek_environment_loads_every_native_option(monkeypatch):
    values = {
        "GUIKERONG_MATCHING_MODE": "deepseek",
        "DEEPSEEK_API_KEY": "fake-native-key",
        "DEEPSEEK_MODEL": "deepseek-flash",
        "DEEPSEEK_BASE_URL": "https://api.deepseek.com/v1",
        "DEEPSEEK_TIMEOUT_SECONDS": "45.5",
        "DEEPSEEK_MAX_RETRIES": "2",
        "DEEPSEEK_CONCURRENCY": "5",
        "DEEPSEEK_MAX_TOKENS": "8192",
        "DEEPSEEK_MAX_PROMPT_CHARACTERS": "300000",
        "DEEPSEEK_THINKING": "enabled",
        "DEEPSEEK_MAX_PRODUCTS": "60",
    }
    for name, value in values.items():
        monkeypatch.setenv(name, value)
    settings = Settings.from_env()
    settings.validate_model()
    assert settings.ark_api_key == "fake-native-key"
    assert settings.ark_model == "deepseek-flash"
    assert settings.ark_base_url == "https://api.deepseek.com/v1"
    assert settings.ark_timeout_seconds == 45.5
    assert settings.ark_max_retries == 2
    assert settings.ark_concurrency == 5
    assert settings.ark_max_completion_tokens == 8192
    assert settings.ark_max_prompt_characters == 300000
    assert settings.ark_thinking == "enabled"
    assert settings.ark_max_products == 60
    assert "fake-native-key" not in repr(settings)


def test_ark_mode_ignores_native_deepseek_namespace(monkeypatch):
    monkeypatch.setenv("GUIKERONG_MATCHING_MODE", "ark")
    for name in ENVIRONMENT_KEYS:
        if name.startswith("DEEPSEEK_"):
            monkeypatch.setenv(name, "invalid-native-secret")
    settings = Settings.from_env()
    assert settings == Settings()
    assert settings.model_provider == "volcengine_ark"


@pytest.mark.parametrize("name", ["DEEPSEEK_API_KEY", "DEEPSEEK_MODEL", "DEEPSEEK_BASE_URL"])
@pytest.mark.parametrize("blank", ["", " \t "])
def test_deepseek_explicit_blank_never_uses_defaults_or_ark(monkeypatch, name, blank):
    caught: pytest.ExceptionInfo
    set_aliases(monkeypatch)
    monkeypatch.setenv("GUIKERONG_MATCHING_MODE", "deepseek")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "fake-native-key")
    monkeypatch.setenv(name, blank)
    if name == "DEEPSEEK_BASE_URL":
        with pytest.raises(ValueError, match=".+") as caught:
            Settings.from_env()
    else:
        settings = Settings.from_env()
        with pytest.raises(DomainError) as caught:
            settings.validate_model()
        assert caught.value.code == "MODEL_NOT_CONFIGURED"
    assert name in str(caught.value)
    assert "fake-native-key" not in str(caught.value)
    assert "fake-alias-key" not in str(caught.value)


def test_deepseek_strings_trim_only_outer_whitespace(monkeypatch):
    monkeypatch.setenv("GUIKERONG_MATCHING_MODE", "deepseek")
    for name, value in {
        "DEEPSEEK_API_KEY": "fake-native-key",
        "DEEPSEEK_MODEL": "deepseek-flash",
        "DEEPSEEK_BASE_URL": "https://api.deepseek.com",
    }.items():
        monkeypatch.setenv(name, " \t" + value + " \n")
    settings = Settings.from_env()
    assert settings.ark_api_key == "fake-native-key"
    assert settings.ark_model == "deepseek-flash"
    assert settings.ark_base_url == "https://api.deepseek.com"
    settings.validate_model()


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("DEEPSEEK_API_KEY", "fake key"),
        ("DEEPSEEK_MODEL", "fake model"),
        ("DEEPSEEK_BASE_URL", "https://user:secret@api.deepseek.com"),
        ("DEEPSEEK_THINKING", "invalid"),
        ("DEEPSEEK_CONCURRENCY", "33"),
        ("DEEPSEEK_MAX_RETRIES", "6"),
        ("DEEPSEEK_MAX_TOKENS", "0"),
        ("DEEPSEEK_MAX_PROMPT_CHARACTERS", "0"),
        ("DEEPSEEK_MAX_PRODUCTS", "0"),
        ("DEEPSEEK_TIMEOUT_SECONDS", "nan"),
        ("DEEPSEEK_CONCURRENCY", "sensitive-not-a-number"),
        ("DEEPSEEK_TIMEOUT_SECONDS", "sensitive-not-a-number"),
    ],
)
def test_invalid_deepseek_configuration_uses_native_names_without_values(monkeypatch, name, value):
    monkeypatch.setenv("GUIKERONG_MATCHING_MODE", "deepseek")
    monkeypatch.setenv(name, value)
    with pytest.raises(ValueError, match=".+") as caught:
        Settings.from_env()
    assert name in str(caught.value)
    assert "ARK_" not in str(caught.value)
    assert "secret" not in str(caught.value)
    assert "sensitive-not-a-number" not in str(caught.value)


@pytest.mark.parametrize(
    "url",
    [
        "https://ark.cn-beijing.volces.com/api/v3",
        "https://api.deepseek.com.evil.test",
        "https://evil.test/secret",
        "https://api.deepseek.com:8443",
    ],
)
def test_native_deepseek_rejects_other_provider_destinations(url):
    with pytest.raises(ValueError, match="DEEPSEEK_BASE_URL.*DeepSeek 官方地址") as caught:
        Settings(matching_mode="deepseek", ark_base_url=url).validate()
    assert "secret" not in str(caught.value)
    assert "evil.test" not in str(caught.value)


@pytest.mark.parametrize(
    "url",
    [
        "https://api.deepseek.com",
        "https://api.deepseek.com/v1",
        "https://api.deepseek.com/beta",
        "https://api.deepseek.com:443/v1/",
    ],
)
def test_direct_deepseek_settings_support_official_addresses(url):
    settings = Settings(
        matching_mode="deepseek",
        ark_api_key="fake-native-key",
        ark_model="deepseek-flash",
        ark_base_url=url,
    )
    settings.validate_model()
    assert settings.model_provider == "deepseek"


@pytest.mark.parametrize("mode", ["DEEPSEEK", "deepseek ", "", "other", None, []])
def test_matching_mode_must_be_an_explicit_supported_value(mode):
    with pytest.raises(ValueError, match="GUIKERONG_MATCHING_MODE.*ark.*deepseek"):
        Settings(matching_mode=mode).validate()
