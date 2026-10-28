"""Exercise startup in fresh interpreters, including IDE script execution."""

import os
import subprocess
import sys
from pathlib import Path

import pytest


@pytest.mark.parametrize("mode", ["script", "module"])
def test_entrypoint_builds_application_without_shadowing_stdlib(mode):
    root = Path(__file__).resolve().parents[1]
    code = """
import runpy
import sys
from pathlib import Path

root = Path.cwd()
mode = sys.argv[1]
if mode == "script":
    # Direct execution puts the script directory first on the import path.
    sys.path.insert(0, str(root / "src" / "guikerong_finance"))

import uvicorn
import http.client

calls = []
def capture_run(app, **kwargs):
    assert any(route.path == "/api/v1/sessions" for route in app.routes)
    assert kwargs["loop"] == "asyncio"
    assert isinstance(kwargs["port"], int)
    calls.append(app)

uvicorn.run = capture_run
if mode == "script":
    runpy.run_path(str(root / "src" / "guikerong_finance" / "__main__.py"), run_name="__main__")
else:
    runpy.run_module("guikerong_finance", run_name="__main__")
assert len(calls) == 1
"""
    result = subprocess.run(  # noqa: S603 -- fixed local interpreter and test code; no shell
        [sys.executable, "-c", code, mode],
        cwd=root,
        env={**os.environ, "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"},
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=90,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
