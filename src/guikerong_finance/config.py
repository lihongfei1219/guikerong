"""Explicit configuration; no network calls and no secrets in source control."""

import math
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

import yaml

from guikerong_finance.errors import DomainError

ROOT = Path(os.getenv("GUIKERONG_DATA_ROOT", str(Path.cwd()))).resolve()


@dataclass(frozen=True)
class Settings:
    csv_path: Path = ROOT / "科技金融产品.csv"
    catalog_db_path: Path = ROOT / "data" / "products.sqlite3"
    environment: str = "development"
    harness_db_path: Path = ROOT / "data" / "harness.sqlite3"
    max_documents: int = 100
    max_document_bytes: int = 1024 * 1024
    max_total_document_bytes: int = 10 * 1024 * 1024
    max_body_bytes: int = 12 * 1024 * 1024
    max_characters: int = 500_000
    timeout_seconds: float = 300.0
    matching_mode: str = "ark"
    ark_api_key: str | None = field(default=None, repr=False)
    ark_model: str = ""
    ark_base_url: str = "https://ark.cn-beijing.volces.com/api/v3"
    ark_timeout_seconds: float = 60.0
    ark_max_retries: int = 1
    ark_concurrency: int = 8
    ark_max_completion_tokens: int = 4096
    ark_max_prompt_characters: int = 200_000
    ark_thinking: str = "disabled"
    ark_max_products: int = 100

    @property
    def model_provider(self) -> str:
        return "deepseek" if self.matching_mode == "deepseek" else "volcengine_ark"

    @property
    def _provider_env_prefix(self) -> str:
        return "DEEPSEEK" if self.matching_mode == "deepseek" else "ARK"

    def validate(self) -> None:
        """Validate explicit settings without reading environment or exposing secrets.

        Startup and catalog browsing do not need model credentials. Model calls
        additionally use validate_model() to require provider credentials.
        """
        if not isinstance(self.environment, str) or self.environment not in {"development", "production"}:
            raise ValueError("GUIKERONG_ENV 必须为 development 或 production")
        if self.matching_mode == "local":
            raise ValueError("旧本地匹配模式已删除，请使用 GUIKERONG_MATCHING_MODE=ark")
        if not isinstance(self.matching_mode, str) or self.matching_mode not in {"ark", "deepseek"}:
            raise ValueError("GUIKERONG_MATCHING_MODE 必须为 ark 或 deepseek")
        prefix = self._provider_env_prefix
        if not isinstance(self.ark_thinking, str) or self.ark_thinking not in {"disabled", "enabled", "omit"}:
            raise ValueError(f"{prefix}_THINKING 必须为 disabled、enabled 或 omit")
        if self.ark_api_key is not None and (
            not isinstance(self.ark_api_key, str)
            or any(ord(char) < 33 or ord(char) > 126 for char in self.ark_api_key)
        ):
            raise ValueError(f"{prefix}_API_KEY 格式无效")
        if not isinstance(self.ark_model, str) or any(char.isspace() or ord(char) < 32 for char in self.ark_model):
            raise ValueError(f"{prefix}_MODEL 格式无效")
        _validate_base_url(self.ark_base_url, name=f"{prefix}_BASE_URL")
        if self.matching_mode == "deepseek":
            parsed_url = urlsplit(self.ark_base_url)
            if parsed_url.hostname != "api.deepseek.com" or parsed_url.port not in (None, 443):
                raise ValueError("DEEPSEEK_BASE_URL 必须使用 DeepSeek 官方地址 https://api.deepseek.com")
        max_tokens_name = "DEEPSEEK_MAX_TOKENS" if self.matching_mode == "deepseek" else "ARK_MAX_COMPLETION_TOKENS"
        for name, value, minimum, maximum in (
            (f"{prefix}_MAX_RETRIES", self.ark_max_retries, 0, 5),
            (f"{prefix}_CONCURRENCY", self.ark_concurrency, 1, 32),
            (max_tokens_name, self.ark_max_completion_tokens, 1, 65_536),
            (f"{prefix}_MAX_PROMPT_CHARACTERS", self.ark_max_prompt_characters, 1, 2_000_000),
            (f"{prefix}_MAX_PRODUCTS", self.ark_max_products, 1, 1_000),
        ):
            if type(value) is not int or not minimum <= value <= maximum:
                raise ValueError(f"{name} 必须为 {minimum} 至 {maximum} 的整数")
        timeout_value: float
        for name, timeout_value, maximum in (
            (f"{prefix}_TIMEOUT_SECONDS", self.ark_timeout_seconds, 600),
            ("GUIKERONG_MATCH_TIMEOUT_SECONDS", self.timeout_seconds, 3600),
        ):
            if (
                isinstance(timeout_value, bool)
                or not isinstance(timeout_value, int | float)
                or not math.isfinite(timeout_value)
                or not 0 < timeout_value <= maximum
            ):
                raise ValueError(f"{name} 必须为大于 0 且不超过 {maximum} 的有限数值")
        for name in (
            "max_documents",
            "max_document_bytes",
            "max_total_document_bytes",
            "max_body_bytes",
            "max_characters",
        ):
            value = getattr(self, name)
            if type(value) is not int or value <= 0:
                raise ValueError(f"{name} 必须为正整数")

    def validate_model(self) -> None:
        """Require model configuration at the point of calling the provider."""
        self.validate()
        prefix = self._provider_env_prefix
        missing = [
            name
            for name, value in ((f"{prefix}_API_KEY", self.ark_api_key), (f"{prefix}_MODEL", self.ark_model))
            if not _nonempty(value)
        ]
        if missing:
            raise DomainError("MODEL_NOT_CONFIGURED", "请先在服务端配置 " + "、".join(missing) + "。", 503)

    @classmethod
    def from_env(cls) -> "Settings":
        deployment = _deployment_config()
        application = deployment.get("finance", {})
        configured_mode = deployment.get("finance_provider", "ark")
        matching_mode = os.getenv("GUIKERONG_MATCHING_MODE", configured_mode)
        # Switching providers must never reuse the other provider's credentials.
        provider = deployment.get("sources", {}).get("finance", {}) if matching_mode == configured_mode else {}
        model_mapping = provider.get("models", {})
        default_model = model_mapping.get("matching", "") if isinstance(model_mapping, dict) else ""
        # Provider namespaces never share credentials, addresses or tuning values.
        # An explicitly blank native setting stays blank instead of selecting Ark.
        if matching_mode == "deepseek":
            prefix = "DEEPSEEK"
            provider_key = os.getenv("DEEPSEEK_API_KEY", provider.get("api_key", "")).strip() or None
            provider_model = os.getenv("DEEPSEEK_MODEL", default_model or "deepseek-flash").strip()
            provider_base_url = os.getenv(
                "DEEPSEEK_BASE_URL", provider.get("base_url", "https://api.deepseek.com")
            ).strip()
            max_tokens_name = "DEEPSEEK_MAX_TOKENS"
        else:
            prefix = "ARK"
            provider_key = _env_alias("ARK_API_KEY", "LLM_DEEPSEEK_FLASH_API_KEY", provider.get("api_key", "")) or None
            provider_model = _env_alias("ARK_MODEL", "LLM_DEEPSEEK_FLASH", default_model)
            provider_base_url = _env_alias(
                "ARK_BASE_URL",
                "LLM_DEEPSEEK_FLASH_BASE_URL",
                provider.get("base_url", "https://ark.cn-beijing.volces.com/api/v3"),
            )
            max_tokens_name = "ARK_MAX_COMPLETION_TOKENS"
        settings = cls(
            csv_path=Path(os.getenv("GUIKERONG_CSV_PATH", str(ROOT / "科技金融产品.csv"))),
            catalog_db_path=Path(os.getenv("GUIKERONG_CATALOG_DB", str(ROOT / "data/products.sqlite3"))),
            environment=os.getenv("GUIKERONG_ENV", application.get("environment", "development")),
            harness_db_path=Path(os.getenv("GUIKERONG_HARNESS_DB", str(ROOT / "data/harness.sqlite3"))),
            matching_mode=matching_mode,
            timeout_seconds=_env_float(
                "GUIKERONG_MATCH_TIMEOUT_SECONDS", application.get("match_timeout_seconds", 300)
            ),
            ark_api_key=provider_key,
            ark_model=provider_model,
            ark_base_url=provider_base_url,
            ark_timeout_seconds=_env_float(f"{prefix}_TIMEOUT_SECONDS", provider.get("timeout", 60)),
            ark_max_retries=_env_int(f"{prefix}_MAX_RETRIES", provider.get("max_retries", 1)),
            ark_concurrency=_env_int(f"{prefix}_CONCURRENCY", provider.get("concurrency", 8)),
            ark_max_completion_tokens=_env_int(max_tokens_name, provider.get("max_completion_tokens", 4096)),
            ark_max_prompt_characters=_env_int(
                f"{prefix}_MAX_PROMPT_CHARACTERS", provider.get("max_prompt_characters", 200_000)
            ),
            ark_thinking=os.getenv(f"{prefix}_THINKING", provider.get("thinking", "disabled")),
            ark_max_products=_env_int(f"{prefix}_MAX_PRODUCTS", provider.get("max_products", 100)),
        )
        settings.validate()
        return settings


def _nonempty(value: object) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _deployment_config() -> dict:
    configured = os.getenv("GUIKERONG_MINDFORGE_CONFIG", "config.yaml")
    if not configured:
        return {}
    path = Path(configured)
    if not path.exists() and "GUIKERONG_MINDFORGE_CONFIG" not in os.environ:
        return {}
    try:
        value = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError):
        raise ValueError("无法读取 Mindforge 配置，请检查 GUIKERONG_MINDFORGE_CONFIG") from None
    if not isinstance(value, dict):
        raise ValueError("Mindforge 配置必须为 YAML 映射")
    return value


def _env_alias(primary: str, alias: str, default: str = "") -> str:
    """Only an undefined primary falls back; an explicit blank remains blank.

    Normalize surrounding whitespace before validation, without changing internal
    characters. Read process environment only; loading .env stays with the caller.
    """
    value = os.getenv(primary)
    if value is None:
        value = os.getenv(alias, default)
    return value.strip()


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except ValueError:
        raise ValueError(f"{name} 必须为整数") from None


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)))
    except ValueError:
        raise ValueError(f"{name} 必须为有限数值") from None


def _validate_base_url(value: str, *, name: str = "ARK_BASE_URL") -> None:
    message = f"{name} 必须为无凭据、查询参数及片段的 HTTPS 基础 URL"
    if not isinstance(value, str) or any(char.isspace() or ord(char) < 32 for char in value):
        raise ValueError(message)
    try:
        parsed = urlsplit(value)
        valid = (
            parsed.scheme == "https"
            and bool(parsed.hostname)
            and parsed.username is None
            and parsed.password is None
            and "?" not in value
            and "#" not in value
            and (parsed.port is None or 1 <= parsed.port <= 65535)
            and re.fullmatch(r"(?:/[A-Za-z0-9._~-]+)*/?", parsed.path) is not None
            and all(part not in {".", ".."} for part in parsed.path.split("/"))
            and "\\" not in parsed.netloc
        )
    except ValueError:
        valid = False
    if not valid:
        raise ValueError(message)
