"""Every pywin32 attribute the Windows code uses must exist.

The service adapters reach pywin32 through aliases (``ws = self._ws``), and
the unit tests replace those modules with fakes, so a misspelled or misplaced
constant (for example ``win32service.DELETE``, which lives in ``win32con``)
only fails on a real service install. This test resolves each alias to its
pywin32 module and checks the attribute statically.
"""

import ast
import importlib
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="pywin32 is Windows-only")

PYWIN32 = {
    "ntsecuritycon",
    "pywintypes",
    "servicemanager",
    "win32api",
    "win32con",
    "win32event",
    "win32file",
    "win32process",
    "win32profile",
    "win32security",
    "win32service",
    "win32serviceutil",
    "win32ts",
    "winerror",
}

SRC = Path(__file__).resolve().parents[1] / "src" / "rootmind"
FILES = sorted((SRC / "service").glob("*.py")) + [SRC / "winaccess.py"]


def _aliases(tree: ast.AST) -> dict[str, str]:
    """Map local names (and ``self.<attr>`` names) to the pywin32 module they hold."""
    alias: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                if a.name in PYWIN32:
                    alias[a.asname or a.name] = a.name
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign):
            continue
        value = node.value
        if isinstance(value, ast.Name) and value.id in alias:
            module = alias[value.id]
        elif (
            isinstance(value, ast.Attribute)
            and isinstance(value.value, ast.Name)
            and value.value.id == "self"
            and value.attr in alias
        ):
            module = alias[value.attr]
        else:
            continue
        for target in node.targets:
            if isinstance(target, ast.Attribute):
                alias[target.attr] = module
            elif isinstance(target, ast.Name):
                alias[target.id] = module
    return alias


@pytest.mark.parametrize("path", FILES, ids=lambda p: p.name)
def test_pywin32_attributes_exist(path: Path) -> None:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    alias = _aliases(tree)
    missing = []
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Attribute)
            and isinstance(node.value, ast.Name)
            and node.value.id in alias
        ):
            module = importlib.import_module(alias[node.value.id])
            if not hasattr(module, node.attr):
                missing.append(
                    f"line {node.lineno}: {node.value.id}.{node.attr} ({module.__name__})"
                )
    assert not missing, "\n".join(missing)
