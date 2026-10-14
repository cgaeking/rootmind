"""The ``service`` command line (``rootmind-backend.exe service ...``, ``rootmind-backend service ...``).

::

    service install [--port N|auto] [--mcp-http | --no-mcp-http] [--mcp-port N|auto]
                    [--mcp-token-from FILE] [--user-sid SID] [--no-start]
    service uninstall [--purge]
    service status [--json]

The RootMind installer runs ``install`` (and its uninstaller ``uninstall``);
the desktop app runs ``install`` again to repair the service or to switch MCP
over HTTP. ``install`` and ``uninstall`` need an elevated prompt; ``install``
only runs from the release build. MCP over HTTP is on for a new install. ``status`` works for every account and from pip/uv
installs. ``service run backend|mcp-http`` is started by the Service Control
Manager and not meant to be run by hand.

On Linux the same commands manage the account's systemd user units
(:mod:`.linux`): no elevation, no ``--user-sid``; ``service run`` is started by
systemd.

Exit codes: 0 ok, 1 unexpected error, 2 usage (or not Windows or Linux, or
not the release build), 3 a service with our name that RootMind did not install,
4 port busy, 5 not elevated, 6 data folder refused, 7 health check failed,
8 account mismatch (also: run as LocalSystem without a service to update),
9 the access check for the account cannot be built,
10 the program folder to copy from can be changed by non-administrators
(Linux: by accounts other than root and this one), 11 (Linux) the account's
systemd user manager cannot be reached. On Linux, 8 means run as root.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from . import ROLE_BACKEND, ROLE_MCP, ExitCode, ServiceCommandError
from .imagepath import is_sid
from .ports import parse_port_option


def _prog() -> str:
    return "rootmind-backend service" if getattr(sys, "frozen", False) else "rootmind service"


def _port_option(value: str) -> int | str:
    try:
        return parse_port_option(value)
    except ValueError as e:
        raise argparse.ArgumentTypeError(str(e)) from None


def _run_port(value: str) -> int:
    try:
        port = int(value)
    except ValueError:
        port = 0
    if not 1 <= port <= 65535:
        raise argparse.ArgumentTypeError(f"expected a port number from 1 to 65535, not {value!r}")
    return port


def _sid(value: str) -> str:
    if not is_sid(value):
        raise argparse.ArgumentTypeError(f"not a SID: {value!r}")
    return value


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog=_prog(),
        description=(
            "Run RootMind in the background: as Windows services, or on Linux as systemd user "
            "units of this account."
        ),
    )
    commands = parser.add_subparsers(
        dest="command", metavar="{install,uninstall,status}", required=True
    )

    install = commands.add_parser(
        "install",
        help="Install or update the service (Windows: elevated prompt)",
        description=(
            "Install the background service, or update and repair it. A new install also "
            "serves MCP over HTTP (127.0.0.1, bearer token) unless --no-mcp-http is given. An "
            "update keeps the ports, the MCP setting, the tokens and the data."
        ),
    )
    install.add_argument(
        "--port",
        type=_port_option,
        default=None,
        help="REST API port, or 'auto' (8000 if free, else 18300-18399). Default: 8000",
    )
    mcp = install.add_mutually_exclusive_group()
    mcp.add_argument(
        "--mcp-http",
        dest="mcp",
        action="store_const",
        const=True,
        default=None,
        help=(
            "Serve MCP over HTTP (service RootMind-MCP-HTTP). Default: on for a new install, "
            "unchanged for an update"
        ),
    )
    mcp.add_argument(
        "--no-mcp-http",
        dest="mcp",
        action="store_const",
        const=False,
        help="Do not serve MCP over HTTP; removes the MCP service if it exists",
    )
    install.add_argument(
        "--mcp-port",
        type=_port_option,
        default=None,
        help="MCP port, or 'auto' (3000 if free, else 18400-18499). Default: 3000",
    )
    install.add_argument(
        "--mcp-token-from",
        metavar="FILE",
        type=Path,
        default=None,
        help="Use the MCP token in this file (for example an existing data\\api_token.txt)",
    )
    install.add_argument(
        "--user-sid",
        type=_sid,
        default=None,
        help=(
            "The account whose folders the service may read (default: the approving account; "
            "run as LocalSystem, which only updates an existing service: its account)"
        ),
    )
    install.add_argument(
        "--no-start", dest="start", action="store_false", help="Do not start the services"
    )

    uninstall = commands.add_parser(
        "uninstall",
        help="Remove the service (Windows: elevated prompt)",
        description=(
            "Stop and remove the services and the program folder. The data folder is kept "
            "(without the REST token) unless --purge is given."
        ),
    )
    uninstall.add_argument(
        "--purge",
        action="store_true",
        help=(
            "Also delete the data folder (%%ProgramData%%\\RootMind; Linux: "
            "~/.local/share/rootmind): index, settings, MCP token"
        ),
    )

    status = commands.add_parser("status", help="Show the service's state")
    status.add_argument("--json", action="store_true", help="Print JSON")

    # Started by the Service Control Manager (the ImagePath) or by systemd (the
    # unit's ExecStart); not listed.
    run = commands.add_parser("run")
    run.add_argument("role", choices=(ROLE_BACKEND, ROLE_MCP))
    run.add_argument("--port", type=_run_port, required=True)
    run.add_argument("--user-sid", type=_sid, default=None)
    return parser


def _reconfigure_output() -> None:
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            try:
                reconfigure(errors="replace")
            except (OSError, ValueError):
                pass


def _default_env():
    """The real machine: Windows or Linux; None elsewhere."""
    if sys.platform == "win32":
        from .system import WindowsSystem

        return WindowsSystem()
    if sys.platform.startswith("linux"):
        from .linux_system import LinuxSystem

        return LinuxSystem()
    return None


def main(argv: list[str] | None = None, env=None) -> int:
    """Entry point of ``service``; returns the exit code.

    Args:
        argv: The arguments after ``service``.
        env: The machine (:class:`~.system.WindowsSystem` or
            :class:`~.linux_system.LinuxSystem`); tests pass a fake.
    """
    args = build_parser().parse_args(sys.argv[1:] if argv is None else argv)
    if args.command == "run":
        if args.role == ROLE_BACKEND and args.user_sid is None and sys.platform == "win32":
            print("error: service run backend needs --user-sid", file=sys.stderr)
            return ExitCode.USAGE
        from .host import run_service

        return int(run_service(args.role, args.port, args.user_sid))

    _reconfigure_output()
    if env is None:
        env = _default_env()
    linux = env is not None and getattr(env, "is_linux", lambda: False)()
    if env is None or not (linux or env.is_windows()):
        print("error: the background service runs on Windows and Linux only", file=sys.stderr)
        return ExitCode.USAGE

    if linux:
        from . import linux as commands
    else:
        from . import install as commands  # type: ignore[no-redef]

    def err(message: str) -> None:
        print(message, file=sys.stderr)

    if args.command == "install":
        options = commands.InstallOptions(
            port=args.port,
            mcp=args.mcp,
            mcp_port=args.mcp_port,
            mcp_token_from=args.mcp_token_from,
            user_sid=args.user_sid,
            start=args.start,
        )
        return commands.install(env, options, out=print, err=err)
    if args.command == "uninstall":
        return commands.uninstall(env, purge=args.purge, out=print, err=err)

    try:
        info = commands.status(env)
    except ServiceCommandError as e:
        err(f"error: {e.message}")
        return int(e.code)
    except OSError as e:  # the service manager or a known folder (ScmError too)
        err(f"error: cannot read the state of the background service: {e.strerror or e}")
        return ExitCode.UNEXPECTED
    from .common import status_json

    print(status_json(info) if args.json else commands.format_status(info))
    return ExitCode.OK
