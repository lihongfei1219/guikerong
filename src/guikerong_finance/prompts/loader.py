"""Versioned prompt assets, loaded without changing their original text."""

from functools import lru_cache
from importlib.resources import files

import yaml

from guikerong_finance.consts import PACKAGE_NAME


@lru_cache(maxsize=1)
def templates() -> dict:
    resource = files(PACKAGE_NAME).joinpath("prompts/templates/finance.yaml")
    data = yaml.safe_load(resource.read_text(encoding="utf-8"))["templates"]
    if not isinstance(data, dict):
        raise ValueError("Invalid prompt template mapping")
    return data


def prompt_text(tag: str) -> str:
    template = templates()[tag]
    # Raw instruction assets contain JSON braces. They are not format variables;
    # dynamic company/product data is assembled separately by context builders.
    text = template["prompt_versions"][template["active_version"]]
    if not isinstance(text, str):
        raise ValueError("Invalid prompt text")
    return text.format()
