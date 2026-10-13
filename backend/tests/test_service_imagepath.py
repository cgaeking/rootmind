"""The ImagePath contract of the services (shared with the desktop app's imagepath.rs)."""

import pytest

from rootmind.service import imagepath
from rootmind.service.imagepath import ImagePath

# The golden strings of the interface contract; the Rust parser uses the same.
GOLDEN_BACKEND = (
    r'"C:\Program Files\RootMind Service\1.2.0-0a1b2c3d\rootmind-backend.exe"'
    r" service run backend --port 8000 --user-sid S-1-5-21-1-2-3-1001"
)
GOLDEN_MCP = (
    r'"C:\Program Files\RootMind Service\1.2.0-0a1b2c3d\rootmind-backend.exe"'
    r" service run mcp-http --port 3000"
)
EXE = r"C:\Program Files\RootMind Service\1.2.0-0a1b2c3d\rootmind-backend.exe"
PF = r"C:\Program Files"


def test_builds_the_golden_strings():
    assert imagepath.build(EXE, "backend", 8000, "S-1-5-21-1-2-3-1001") == GOLDEN_BACKEND
    assert imagepath.build(EXE, "mcp-http", 3000) == GOLDEN_MCP


def test_parses_the_golden_strings():
    assert imagepath.parse(GOLDEN_BACKEND) == ImagePath(EXE, "backend", 8000, "S-1-5-21-1-2-3-1001")
    assert imagepath.parse(GOLDEN_MCP) == ImagePath(EXE, "mcp-http", 3000, None)
    assert imagepath.exe_is_ours(EXE, PF)


def test_round_trip():
    for text in (GOLDEN_BACKEND, GOLDEN_MCP):
        parsed = imagepath.parse(text)
        assert imagepath.build(parsed.exe, parsed.role, parsed.port, parsed.user_sid) == text


def test_tolerates_surrounding_and_repeated_whitespace():
    raw = "  " + GOLDEN_MCP.replace(" --port", "   --port") + "  "
    assert imagepath.parse(raw).port == 3000


@pytest.mark.parametrize(
    "raw",
    [
        None,
        "",
        # unquoted path
        r"C:\Program Files\RootMind Service\1.2.0-0a1b2c3d\rootmind-backend.exe service run "
        r"backend --port 8000 --user-sid S-1-5-21-1",
        # another program, without arguments
        r'"C:\Program Files\RootMind Service\other.exe"',
        r"C:\tools\other-service.exe",
        # no arguments, no closing quote, no space after the quote
        r'"C:\x\rootmind-backend.exe"',
        r'"C:\x\rootmind-backend.exe service run mcp-http --port 3000',
        r'"C:\x\rootmind-backend.exe"service run mcp-http --port 3000',
        # unknown role, missing or invalid port
        r'"C:\x\rootmind-backend.exe" service run indexer --port 3000',
        r'"C:\x\rootmind-backend.exe" service run mcp-http',
        r'"C:\x\rootmind-backend.exe" service run mcp-http --port',
        r'"C:\x\rootmind-backend.exe" service run mcp-http --port 0',
        r'"C:\x\rootmind-backend.exe" service run mcp-http --port 70000',
        r'"C:\x\rootmind-backend.exe" service run mcp-http --port +80',
        # backend without the account, account on MCP, malformed SID
        r'"C:\x\rootmind-backend.exe" service run backend --port 8000',
        r'"C:\x\rootmind-backend.exe" service run mcp-http --port 3000 --user-sid S-1-5-21-1',
        r'"C:\x\rootmind-backend.exe" service run backend --port 8000 --user-sid S-1-5-x',
        # unknown or repeated options, another subcommand
        r'"C:\x\rootmind-backend.exe" service run mcp-http --port 3000 --debug',
        r'"C:\x\rootmind-backend.exe" service run mcp-http --port 3000 --port 3001',
        r'"C:\x\rootmind-backend.exe" api --port 8000',
    ],
)
def test_rejects_other_shapes(raw):
    assert imagepath.parse(raw) is None


@pytest.mark.parametrize(
    ("exe", "ours"),
    [
        (EXE, True),
        (EXE.upper(), True),
        (r"C:\Program Files\RootMind Service\x\rootmind-backend.exe", True),
        # other folders (a similar name, the user profile), other names and depths
        (r"C:\Program Files\Other\rootmind-backend.exe", False),
        (r"C:\Program Files\RootMind Service2\x\rootmind-backend.exe", False),
        (r"C:\Users\me\AppData\Local\RootMind\backend\rootmind-backend.exe", False),
        (r"C:\Program Files\RootMind Service\1.2.0-0a1b2c3d\other.exe", False),
        (r"C:\Program Files\RootMind Service\rootmind-backend.exe", False),
        (r"C:\Program Files\RootMind Service\a\b\rootmind-backend.exe", False),
        (r"C:\Program Files\RootMind Service\..\rootmind-backend.exe", False),
        (r"C:\Program Files\RootMind Service\\rootmind-backend.exe", False),
        ("C:/Program Files/RootMind Service/x/rootmind-backend.exe", False),
        (r"D:\Program Files\RootMind Service\x\rootmind-backend.exe", False),
    ],
)
def test_exe_is_ours(exe, ours):
    assert imagepath.exe_is_ours(exe, PF) is ours


def test_exe_is_ours_needs_the_program_files_folder():
    assert not imagepath.exe_is_ours(EXE, "")
    assert imagepath.exe_is_ours(EXE, PF + "\\")


@pytest.mark.parametrize(
    "args",
    [
        ('C:\\x"y\\rootmind-backend.exe', "mcp-http", 3000, None),
        ("", "mcp-http", 3000, None),
        (EXE, "backend", 8000, None),
        (EXE, "backend", 8000, "not-a-sid"),
        (EXE, "mcp-http", 3000, "S-1-5-21-1"),
        (EXE, "mcp-http", 0, None),
        (EXE, "indexer", 3000, None),
    ],
)
def test_build_refuses_invalid_input(args):
    with pytest.raises(ValueError):
        imagepath.build(*args)


@pytest.mark.parametrize(
    ("value", "valid"),
    [
        ("S-1-5-21-1-2-3-1001", True),
        ("S-1-5-18", True),
        ("S-1-5-80-298245856-1475029704-2427701759-2504033446-509022668", True),
        ("S-1-5", False),
        ("S-1-5-", False),
        ("s-1-5-18", False),
        ("S-1-5-x", False),
        ("S-1-5-18 ", False),
    ],
)
def test_is_sid(value, valid):
    assert imagepath.is_sid(value) is valid
