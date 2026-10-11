"""Smoke tests: every entry module imports and the apps build without keys or network."""

import importlib
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest
from helpers import rest_client

MODULES = [
    "rootmind",
    "rootmind.main",
    "rootmind.config",
    "rootmind.tools",
    "rootmind.api.app",
    "rootmind.api.routes",
    "rootmind.rlm_engine.engine",
    "rootmind.mcp_stdio",
    "rootmind.mcp_http",
]


@pytest.mark.parametrize("name", MODULES)
def test_module_imports(name):
    importlib.import_module(name)


def test_version_matches_pyproject():
    import rootmind

    pyproject = tomllib.loads(
        (Path(__file__).resolve().parents[1] / "pyproject.toml").read_text(encoding="utf-8")
    )
    assert rootmind.__version__ == pyproject["project"]["version"]


def test_create_app_with_config_object(tmp_path):
    from rootmind.api.app import create_app
    from rootmind.config import AppConfig

    config = AppConfig()
    config.database.path = str(tmp_path / "index.db")
    app = create_app(config)
    with rest_client(app) as client:
        assert client.get("/health").json() == {"status": "healthy"}


def test_mcp_http_app_requires_token():
    from rootmind.mcp_http import TokenError, create_app

    with pytest.raises(TokenError):
        create_app("   ")


def test_no_deprecation_warnings_from_rootmind():
    """Import every module in a fresh interpreter, so import-time warnings are seen too."""
    code = (
        "import importlib, sys\n"
        f"for name in {MODULES!r}:\n"
        "    importlib.import_module(name)\n"
        "from rootmind.config import AppConfig, Settings\n"
        "Settings(); AppConfig()\n"
    )
    result = subprocess.run(  # noqa: S603 (fixed arguments, this interpreter)
        [sys.executable, "-W", "error::DeprecationWarning", "-c", code],
        capture_output=True,
        text=True,
        timeout=120,
        cwd=Path(__file__).resolve().parents[1],
    )
    assert result.returncode == 0, result.stderr
