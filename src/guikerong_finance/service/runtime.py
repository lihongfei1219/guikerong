"""Shared Mindforge registration and provider mapping for this package."""

import logging
import math
from functools import lru_cache

from guikerong_finance.config import Settings
from guikerong_finance.consts import PACKAGE_NAME


class _NoPrivateTrace(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        # Mindforge TRACE includes complete configuration and prompt contents.
        return record.levelno >= logging.INFO


_private_trace_filter = _NoPrivateTrace()


def get_mindforge(settings: Settings):
    return _registered_provider(
        settings.ark_api_key or "", settings.ark_model, settings.ark_base_url, math.ceil(settings.ark_timeout_seconds)
    )


@lru_cache(maxsize=8)
def _registered_provider(api_key: str, model: str, base_url: str, timeout: int):
    from mindforge import register_mindforge
    from mindforge.utils.logger import logger

    logger.addFilter(_private_trace_filter)

    config = {
        "lazy_loading": True,
        "sources": {
            "finance": {
                "api_key": api_key,
                "base_url": base_url,
                "models": {"matching": model},
                "timeout": timeout,
                "active": bool(api_key and model),
                # Application-level retry policy is shared with the existing API.
                "max_retries": 0,
            }
        },
        "prompts": {"prompt_root": "prompts/templates", "prompt_suffix_file": "prompts/suffix.yaml"},
        "session": {"memory_cache_num": 0, "cache_policy": "LRU"},
    }
    return register_mindforge(config=config, package_name=PACKAGE_NAME)
