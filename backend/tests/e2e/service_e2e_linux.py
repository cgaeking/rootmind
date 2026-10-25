"""End-to-end test of the RootMind background service on Linux (systemd user units).

Installs, checks, updates and removes the real user units with the packaged
``rootmind-backend`` (the Linux onedir build). It changes the account it runs
as: it writes ``~/.config/systemd/user/rootmind-*.service`` and
``~/.local/share/rootmind`` and starts and stops the units.

It therefore refuses to run unless ``CI=true`` (GitHub's runners) or
``--force`` is given, and unless none of those files exist yet. It needs a
running systemd user manager for the account (on a runner: ``loginctl
enable-linger`` and ``XDG_RUNTIME_DIR``). It always cleans up, also after a
failure. Python standard library only (any Python 3.10+)::

    python3 backend/tests/e2e/service_e2e_linux.py --exe-dir backend/dist/rootmind-backend \\
        --api-port 18080 --mcp-port 18081

1. Refusals: a foreign unit (3), a busy port (4), an unprotected data folder
   (6), a program folder others may change (10), --user-sid (2).
2. Install (MCP over HTTP on by default): units, modes of the data folder and
   the secret files, the program copy, service.json without a secret.
3. Tokens: 401/200 on both services; MCP lists the five tools.
4. Remote access: the authtoken file is 0600 and in no log (never turned on).
5. Indexing a folder through the REST API; MCP finds it without a restart.
5b. The command line (status, folders, index --wait, search, mcp tokens):
   a named token scoped to another folder does not see the first one, a
   revoked token is refused at once; the network listener (plain HTTP on
   127.0.0.2 with insecure_http) takes the named token, refuses the default
   token and a foreign Host.
6. status --json; systemd restarts a killed service; a restart keeps it.
7. An update from another copy (ports and tokens kept, the old copy gone),
   MCP over HTTP off and on again (as the app switches it).
8. Uninstall, again, and with --purge.
"""

# ruff: noqa: S103, S607 (modes under test; systemctl and journalctl from PATH)
from __future__ import annotations

import argparse
import json
import os
import shutil
import signal
import socket
import stat
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

UNITS = ("rootmind-backend.service", "rootmind-mcp-http.service")
TOOLS = {"search_documents", "search_fuzzy", "list_documents", "read_document", "get_statistics"}
FAKE_AUTHTOKEN = "e2e_fake_ngrok_authtoken_000000000000"  # pragma: allowlist secret


class E2EError(Exception):
    pass


def fail(message: str) -> None:
    raise E2EError(f"E2E FAILED: {message}")


def ok(message: str) -> None:
    print(f"ok   {message}", flush=True)


def check(condition: bool, message: str) -> None:
    if not condition:
        fail(message)
    ok(message)


def xdg(name: str, default: str) -> Path:
    value = os.environ.get(name, "")
    return Path(value) if value.startswith("/") else Path.home() / default


DATA = xdg("XDG_DATA_HOME", ".local/share") / "rootmind"
UNIT_DIR = xdg("XDG_CONFIG_HOME", ".config") / "systemd" / "user"


def run(args: list[str], *, timeout: float = 600) -> tuple[int, str]:
    """Run a command; returns (exit code, stdout+stderr), and echoes both."""
    result = subprocess.run(  # noqa: S603 (fixed programs built here)
        args, capture_output=True, text=True, timeout=timeout, check=False
    )
    output = (result.stdout + result.stderr).rstrip()
    print(
        f"> {' '.join(Path(a).name if i == 0 else a for i, a in enumerate(args))}  "
        f"[exit {result.returncode}]"
    )
    if output:
        print("\n".join(f"    {line}" for line in output.splitlines()))
    return result.returncode, output


def expect_exit(args: list[str], code: int, what: str) -> str:
    actual, output = run(args)
    if actual != code:
        fail(f"{what} exited with {actual}, expected {code}")
    ok(f"{what} -> exit {code}")
    return output


def systemctl(*args: str) -> tuple[int, str]:
    result = subprocess.run(  # noqa: S603
        ["systemctl", "--user", *args], capture_output=True, text=True, timeout=120, check=False
    )
    return result.returncode, (result.stdout + result.stderr).strip()


def show(unit: str, prop: str) -> str:
    return systemctl("show", unit, f"--property={prop}", "--value")[1]


def http(method: str, url: str, token: str | None = None, body=None) -> tuple[int, object]:
    data = json.dumps(body).encode() if body is not None else None
    headers = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(url, data=data, method=method, headers=headers)  # noqa: S310
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(request, timeout=30) as response:
            status, raw = response.status, response.read()
    except urllib.error.HTTPError as e:
        status, raw = e.code, e.read()
    except (urllib.error.URLError, OSError):
        return 0, None
    try:
        return status, json.loads(raw) if raw else None
    except ValueError:
        return status, raw.decode("utf-8", "replace")


def mcp(port: int, token: str | None, method: str, params=None, request_id: int = 1):
    body = {"jsonrpc": "2.0", "id": request_id, "method": method, "params": params or {}}
    if method == "initialize":
        body["params"] = {
            "protocolVersion": "2025-06-18",
            "capabilities": {},
            "clientInfo": {"name": "service-e2e", "version": "1"},
        }
    return http("POST", f"http://127.0.0.1:{port}/mcp", token, body)


def wait_healthy(port: int, seconds: float = 90) -> None:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if http("GET", f"http://127.0.0.1:{port}/health")[0] == 200:
            return
        time.sleep(0.5)
    fail(f"127.0.0.1:{port}/health did not answer within {seconds} s")


def mode(path: Path) -> int:
    return stat.S_IMODE(os.lstat(path).st_mode)


def copy_build(source: Path, target: Path) -> Path:
    shutil.copytree(source, target)
    for folder, dirs, files in os.walk(target):
        for name in dirs + files:
            os.chmod(
                Path(folder) / name, 0o755 if name in dirs or name == "rootmind-backend" else 0o644
            )
    os.chmod(target, 0o755)
    return target / "rootmind-backend"


def status_json(exe: Path) -> dict:
    code, output = run([str(exe), "service", "status", "--json"])
    if code != 0:
        fail(f"service status --json exited with {code}")
    return json.loads(output[output.index("{") :])


def no_secret_in(paths: list[Path], secrets: list[str], what: str) -> None:
    for path in paths:
        for file in [path] if path.is_file() else [p for p in path.rglob("*") if p.is_file()]:
            data = file.read_bytes()
            for secret in secrets:
                if secret.encode() in data:
                    fail(f"a secret is in {file}")
    ok(what)


def test(exe_dir: Path, api: int, mcp_port: int, work: Path) -> None:
    sources = work / "sources"
    sources.mkdir(mode=0o755)
    os.chmod(work, 0o755)
    exe_a = copy_build(exe_dir, sources / "a")
    exe_b = copy_build(exe_dir, sources / "b")
    code, version_line = run([str(exe_a), "--version"])
    version = version_line.split()[-1]
    check(code == 0, f"testing version {version} as uid {os.getuid()}")

    # --- 1. refusals ---------------------------------------------------------------------
    UNIT_DIR.mkdir(parents=True, exist_ok=True)
    foreign = UNIT_DIR / UNITS[0]
    foreign.write_text("[Service]\nExecStart=/bin/true\n")
    systemctl("daemon-reload")
    expect_exit([str(exe_a), "service", "install"], 3, "install with a foreign unit")
    check(
        foreign.read_text() == "[Service]\nExecStart=/bin/true\n", "the foreign unit is unchanged"
    )
    foreign.unlink()
    systemctl("daemon-reload")

    with socket.socket() as busy:
        busy.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        busy.bind(("127.0.0.1", api))
        busy.listen()
        expect_exit(
            [str(exe_a), "service", "install", "--port", str(api)], 4, "install on a busy port"
        )

    unsafe = sources / "unsafe"
    unsafe_exe = copy_build(exe_dir, unsafe)
    os.chmod(unsafe / "_internal", 0o777)
    expect_exit(
        [str(unsafe_exe), "service", "install"], 10, "install from a folder others may change"
    )
    shutil.rmtree(unsafe)

    DATA.mkdir(parents=True)
    os.chmod(DATA, 0o777)
    expect_exit([str(exe_a), "service", "install"], 6, "install into an unprotected data folder")
    DATA.rmdir()

    expect_exit(
        [str(exe_a), "service", "install", "--user-sid", "S-1-5-21-1-2-3-1001"], 2, "--user-sid"
    )
    check(not DATA.exists() and not any(UNIT_DIR.glob("rootmind-*")), "nothing was left behind")

    # --- 2. install ------------------------------------------------------------------------
    expect_exit(
        [str(exe_a), "service", "install", "--port", str(api), "--mcp-port", str(mcp_port)],
        0,
        "install (MCP over HTTP on by default)",
    )
    for unit in UNITS:
        check(show(unit, "ActiveState") == "active", f"{unit} is active")
        check(systemctl("is-enabled", unit)[1] == "enabled", f"{unit} is enabled")
    check(mode(DATA) == 0o700, "the data folder is 0700")
    for sub in ("data", "data/db", "logs", "logs/backend", "logs/mcp-http", "tmp", "runtime"):
        check(mode(DATA / sub) == 0o700, f"{sub}/ is 0700")
    rest_token = (DATA / "data" / "rest_token.txt").read_text().strip()
    mcp_token = (DATA / "data" / "api_token.txt").read_text().strip()
    for name in ("rest_token.txt", "api_token.txt"):
        check(mode(DATA / "data" / name) == 0o600, f"{name} is 0600")
    check(mode(DATA / "config.yaml") == 0o600, "config.yaml is 0600")
    others = [p for p in DATA.rglob("*") if not p.is_symlink() and os.lstat(p).st_mode & 0o077]
    check(
        not others, f"nothing in the data folder grants the group or others anything {others[:3]}"
    )
    copies = list((DATA / "runtime").iterdir())
    check(len(copies) == 1 and copies[0].name.startswith(f"{version}-"), "one program copy")
    copy_exe = copies[0] / "rootmind-backend"
    discovery = json.loads((DATA / "service.json").read_text())
    check(
        discovery == {"executable": str(copy_exe), "version": version},
        "service.json names the copy",
    )
    pid = int(show(UNITS[0], "MainPID"))
    check(
        Path(os.readlink(f"/proc/{pid}/exe")) == copy_exe,
        "the backend runs from the program copy",
    )
    no_secret_in(
        [DATA / "service.json", DATA / "logs", UNIT_DIR / UNITS[0], UNIT_DIR / UNITS[1]],
        [rest_token, mcp_token],
        "no token in the units, service.json or the logs",
    )

    # --- 3. tokens ----------------------------------------------------------------------------
    rest = f"http://127.0.0.1:{api}"
    check(http("GET", f"{rest}/status")[0] == 401, "REST without the token -> 401")
    check(http("GET", f"{rest}/status", rest_token)[0] == 200, "REST with the token -> 200")
    check(http("GET", f"{rest}/status", mcp_token)[0] == 401, "REST with the MCP token -> 401")
    check(mcp(mcp_port, None, "initialize")[0] == 401, "MCP without the token -> 401")
    check(mcp(mcp_port, rest_token, "initialize")[0] == 401, "MCP with the REST token -> 401")
    check(mcp(mcp_port, mcp_token, "initialize")[0] == 200, "MCP with the token -> 200")
    listed = mcp(mcp_port, mcp_token, "tools/list", request_id=2)[1]
    names = {tool["name"] for tool in listed["result"]["tools"]}
    check(TOOLS <= names, "MCP lists the five tools")

    # --- 4. remote access (never turned on: that would contact ngrok) ------------------------
    deadline = time.monotonic() + 30
    reported: dict = {}
    while time.monotonic() < deadline:
        try:
            reported = json.loads((DATA / "data" / "remote_access.json").read_text())
        except (OSError, ValueError):
            reported = {}
        if reported.get("updated"):
            break
        time.sleep(0.5)
    check(reported.get("state") == "off", "the MCP service reports remote access off")
    code, body = http("GET", f"{rest}/remote-access", rest_token)
    check(
        code == 200 and body["sdk"]["available"] and not body["enabled"], "the ngrok SDK is bundled"
    )
    code, body = http("POST", f"{rest}/remote-access", rest_token, {"authtoken": FAKE_AUTHTOKEN})
    check(code == 200 and body["authtoken_saved"] and not body["enabled"], "the authtoken is saved")
    authtoken = DATA / "data" / "ngrok_authtoken.txt"
    check(mode(authtoken) == 0o600, "ngrok_authtoken.txt is 0600")
    no_secret_in(
        [DATA / "config.yaml", DATA / "logs"], [FAKE_AUTHTOKEN], "the authtoken is in no log"
    )
    code, body = http("POST", f"{rest}/remote-access", rest_token, {"authtoken": ""})
    check(code == 200 and not authtoken.exists(), "the authtoken is removed")

    # --- 5. indexing --------------------------------------------------------------------------
    folder = work / "docs"
    folder.mkdir()
    (folder / "a.md").write_text("# A\n\nThe word quartzlanternalpha is in this file.\n")
    code, current = http("GET", f"{rest}/config", rest_token)
    check(code == 200, "GET /config")
    connectors = [{"name": "e2e", "type": "local", "path": str(folder)}]
    code, _ = http(
        "POST", f"{rest}/config", rest_token, {"llm": current["llm"], "connectors": connectors}
    )
    check(code == 200, "POST /config adds a folder")
    http("POST", f"{rest}/index/refresh", rest_token, {"full": True})
    deadline = time.monotonic() + 120
    while True:
        code, found = http("GET", f"{rest}/search?q=quartzlanternalpha", rest_token)
        if code == 200 and found.get("count", 0) >= 1:
            break
        if time.monotonic() > deadline:
            fail(f"the folder was not indexed within 120 s: {found}")
        time.sleep(1)
    ok("the service indexed the folder")
    code, result = mcp(
        mcp_port,
        mcp_token,
        "tools/call",
        {"name": "search_documents", "arguments": {"query": "quartzlanternalpha"}},
        request_id=3,
    )
    check(
        code == 200 and "quartzlanternalpha" in json.dumps(result), "MCP finds it without a restart"
    )

    # --- 5b. the command line, named tokens, the network listener --------------------------
    cli = [str(exe_a)]
    output = expect_exit([*cli, "status", "--json"], 0, "rootmind status --json")
    info = json.loads(output[output.index("{") :])
    check(
        info["service"]["state"] == "running" and info["folders"][0]["name"] == "e2e",
        "status names the service and the folder",
    )
    check(rest_token not in output and mcp_token not in output, "status prints no token")
    other = work / "other"
    other.mkdir()
    (other / "b.md").write_text("# B\n\nThe word nebulaharborbeta is only here.\n")
    expect_exit([*cli, "folders", "add", str(other), "--name", "other"], 0, "folders add")
    expect_exit([*cli, "index", "--wait"], 0, "index --wait")
    output = expect_exit([*cli, "search", "nebulaharborbeta", "--json"], 0, "search")
    check(json.loads(output[output.index("{") :])["count"] >= 1, "search finds the new folder")

    # The network listener: plain HTTP on 127.0.0.2 (no certificate in CI). The
    # config has no mcp_http section yet, so it is appended as text (this script
    # uses the standard library only) and the file restored afterwards.
    config_file = DATA / "config.yaml"
    saved_config = config_file.read_text()
    check("mcp_http" not in saved_config, "no mcp_http section before the test")
    net_port = mcp_port + 2
    config_file.write_text(
        saved_config.rstrip("\n")
        + "\nmcp_http:\n  allowed_hosts: ['127.0.0.2']\n  network:\n"
        + f"    bind: 127.0.0.2\n    port: {net_port}\n    insecure_http: true\n"
    )
    code, _ = systemctl("restart", UNITS[1])
    check(code == 0, "restart MCP over HTTP with the network listener")
    wait_healthy(mcp_port)
    deadline = time.monotonic() + 30
    while http("GET", f"http://127.0.0.2:{net_port}/health")[0] == 0:
        if time.monotonic() > deadline:
            fail("the network listener did not answer")
        time.sleep(0.5)
    ok("the network listener answers on 127.0.0.2")

    token_file = work / "scoped.token"
    expect_exit(
        [*cli, "mcp", "tokens", "create", "scoped", "--folders", "other", "--out", str(token_file)],
        0,
        "mcp tokens create",
    )
    check(mode(token_file) == 0o600, "the new token's file is 0600")
    scoped = token_file.read_text().strip()

    def search(token: str, word: str, port: int = mcp_port) -> tuple[int, str]:
        code, result = mcp(
            port,
            token,
            "tools/call",
            {"name": "search_documents", "arguments": {"query": word}},
            request_id=4,
        )
        return code, json.dumps(result)

    code, found = search(scoped, "nebulaharborbeta")
    check(code == 200 and "b.md" in found, "the scoped token sees its folder")
    code, found = search(scoped, "quartzlanternalpha")
    check(code == 200 and "a.md" not in found, "the scoped token does not see the other folder")
    no_secret_in([DATA], [scoped], "the new token is stored nowhere in the data folder")
    code, found = search(scoped, "nebulaharborbeta", net_port)
    check(
        code == 200 and "b.md" in found,
        f"the network listener takes the named token ({code}: {found[:300]})",
    )
    code, found = search(mcp_token, "nebulaharborbeta", net_port)
    check(code == 401, f"but not the default token ({code})")
    check(search(mcp_token, "quartzlanternalpha")[0] == 200, "loopback still takes it")

    expect_exit([*cli, "mcp", "tokens", "revoke", "scoped"], 0, "mcp tokens revoke")
    check(search(scoped, "nebulaharborbeta")[0] == 401, "a revoked token is refused at once")
    check(search(mcp_token, "nebulaharborbeta")[0] == 200, "the other tokens keep working")

    config_file.write_text(saved_config)
    systemctl("restart", UNITS[1])
    wait_healthy(mcp_port)
    check(
        http("GET", f"http://127.0.0.2:{net_port}/health")[0] == 0,
        "without the setting there is no network listener",
    )

    # --- 6. status, restarts ------------------------------------------------------------------
    info = status_json(copy_exe)
    check(
        info["ours"]
        and info["state"] == "running"
        and info["port"] == api
        and info["version"] == version
        and info["token_readable"],
        "status --json: ours, running, port, version",
    )
    check(
        info["mcp_http"]["state"] == "running" and info["mcp_http"]["port"] == mcp_port,
        "status --json: MCP running on its port",
    )
    old_pid = int(show(UNITS[0], "MainPID"))
    os.kill(old_pid, signal.SIGKILL)
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        new_pid = int(show(UNITS[0], "MainPID") or 0)
        if new_pid and new_pid != old_pid and show(UNITS[0], "ActiveState") == "active":
            break
        time.sleep(0.5)
    else:
        fail("systemd did not restart the killed backend")
    wait_healthy(api)
    ok("systemd restarts a backend that was killed")
    code, _ = systemctl("restart", UNITS[1])
    check(code == 0, "systemctl --user restart of the MCP unit")
    wait_healthy(mcp_port)
    check(mcp(mcp_port, mcp_token, "initialize")[0] == 200, "MCP answers after the restart")

    # --- 7. update, MCP off and on ----------------------------------------------------------
    expect_exit(
        [str(exe_b), "service", "install", "--port", "auto", "--mcp-port", "auto"],
        0,
        "update from another copy",
    )
    copies = list((DATA / "runtime").iterdir())
    check(
        len(copies) == 1 and copies[0] != copy_exe.parent,
        "the update made a new copy and removed the old one",
    )
    copy_exe = copies[0] / "rootmind-backend"
    info = status_json(copy_exe)
    check(
        info["port"] == api and info["mcp_http"]["port"] == mcp_port, "the update keeps the ports"
    )
    check(
        (DATA / "data" / "rest_token.txt").read_text().strip() == rest_token, "and the REST token"
    )
    check((DATA / "data" / "api_token.txt").read_text().strip() == mcp_token, "and the MCP token")
    code, found = http("GET", f"{rest}/search?q=quartzlanternalpha", rest_token)
    check(code == 200 and found.get("count", 0) >= 1, "and the index")

    expect_exit([str(copy_exe), "service", "install", "--no-mcp-http"], 0, "MCP over HTTP off")
    check(not (UNIT_DIR / UNITS[1]).exists(), "the MCP unit is gone")
    check(
        http("GET", f"http://127.0.0.1:{mcp_port}/health")[0] == 0,
        "nothing listens on the MCP port",
    )
    expect_exit(
        [str(copy_exe), "service", "install", "--mcp-http", "--mcp-port", str(mcp_port)],
        0,
        "MCP over HTTP on",
    )
    check(mcp(mcp_port, mcp_token, "initialize")[0] == 200, "MCP is back with its port and token")

    # --- 8. uninstall -------------------------------------------------------------------------
    expect_exit([str(exe_b), "service", "uninstall"], 0, "uninstall")
    check(not any(UNIT_DIR.glob("rootmind-*")), "the units are gone")
    for unit in UNITS:
        check(show(unit, "LoadState") == "not-found", f"systemd no longer knows {unit}")
    check(not (DATA / "runtime").exists(), "the program copies are gone")
    check(not (DATA / "data" / "rest_token.txt").exists(), "the REST token is gone")
    check((DATA / "data" / "api_token.txt").exists(), "the MCP token and the data are kept")
    check(http("GET", f"{rest}/health")[0] == 0, "nothing listens on the REST port")
    output = expect_exit([str(exe_b), "service", "uninstall"], 0, "uninstall again")
    check("nothing to do" in output, "a second uninstall has nothing to do")
    expect_exit([str(exe_b), "service", "uninstall", "--purge"], 0, "uninstall --purge")
    check(not DATA.exists(), "the data folder is deleted")


def collect_diagnostics(target: Path) -> None:
    target.mkdir(parents=True, exist_ok=True)
    for unit in UNITS:
        (target / f"{unit}.status.txt").write_text(systemctl("status", "--no-pager", unit)[1])
        result = subprocess.run(  # noqa: S603
            ["journalctl", "--user", "-u", unit, "--no-pager", "-n", "200"],
            capture_output=True,
            text=True,
            check=False,
        )
        (target / f"{unit}.journal.txt").write_text(result.stdout + result.stderr)
    if (DATA / "logs").is_dir():
        shutil.copytree(DATA / "logs", target / "logs", dirs_exist_ok=True)


def cleanup() -> None:
    for unit in UNITS:
        systemctl("stop", unit)
        systemctl("disable", unit)
        (UNIT_DIR / unit).unlink(missing_ok=True)
    systemctl("daemon-reload")
    for unit in UNITS:
        systemctl("reset-failed", unit)
    if DATA.exists():
        for folder, dirs, _files in os.walk(DATA):
            for name in dirs:
                os.chmod(Path(folder) / name, 0o700)
        os.chmod(DATA, 0o700)
        shutil.rmtree(DATA)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--exe-dir", type=Path, required=True)
    parser.add_argument("--api-port", type=int, default=18080)
    parser.add_argument("--mcp-port", type=int, default=18081)
    parser.add_argument("--diagnostics-dir", type=Path)
    parser.add_argument("--force", action="store_true", help="run outside CI")
    args = parser.parse_args(argv)

    def refuse(message: str) -> int:
        print(f"service_e2e_linux: refusing to run: {message}")
        return 2

    if not sys.platform.startswith("linux"):
        return refuse("this test is for Linux")
    if os.environ.get("CI") != "true" and not args.force:
        return refuse("it installs systemd user units; it runs in CI (CI=true), or pass --force")
    if os.getuid() == 0:
        return refuse("run it as a normal account, not as root")
    for port in (args.api_port, args.mcp_port):
        if port in (3000, 8000, 8010) or not 1024 <= port <= 65535:
            return refuse(f"port {port} is not allowed here")
    if systemctl("show", "--property=Version")[0] != 0:
        return refuse("the systemd user manager is not reachable (XDG_RUNTIME_DIR, linger?)")
    if DATA.exists() or any(UNIT_DIR.glob("rootmind-*")):
        return refuse(f"{DATA} or a rootmind unit in {UNIT_DIR} exists already")
    exe_dir = args.exe_dir.resolve()
    if not (exe_dir / "rootmind-backend").is_file():
        return refuse(f"{exe_dir} has no rootmind-backend")

    work = Path(tempfile.mkdtemp(prefix="rootmind-e2e-"))
    try:
        test(exe_dir, args.api_port, args.mcp_port, work)
    except Exception as e:
        print(str(e) if isinstance(e, E2EError) else f"E2E FAILED: {e!r}", flush=True)
        if args.diagnostics_dir:
            collect_diagnostics(args.diagnostics_dir)
        return 1
    finally:
        cleanup()
        shutil.rmtree(work, ignore_errors=True)
    print("Linux service end-to-end test passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
