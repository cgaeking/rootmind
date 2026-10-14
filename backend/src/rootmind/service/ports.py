"""Choosing the services' ports (``--port`` and ``--mcp-port``).

- No option: the default port (8000, MCP 3000), strictly: busy means exit 4.
- A number: that port, strictly.
- ``auto``: the default port when a test bind on 127.0.0.1 succeeds, otherwise
  the first bindable port of the fallback range (18300-18399, MCP
  18400-18499). Ports Windows has excluded fail the test bind and are skipped.

An update keeps the ports of the installed services and never tests them.
"""

from __future__ import annotations

import socket
import sys
from collections.abc import Callable, Iterable

from . import ExitCode, ServiceCommandError

AUTO = "auto"

PortOption = int | str | None  # a number, "auto", or None for the strict default


def parse_port_option(value: str) -> int | str:
    """Parse ``--port`` / ``--mcp-port``: ``auto`` or a port number from 1 to 65535."""
    if value.strip().lower() == AUTO:
        return AUTO
    try:
        port = int(value)
    except ValueError:
        port = 0
    if not 1 <= port <= 65535:
        raise ValueError(f"expected 'auto' or a port number from 1 to 65535, not {value!r}")
    return port


def can_bind(port: int, host: str = "127.0.0.1") -> bool:
    """Whether a listening socket could be bound to ``host:port`` right now.

    On Windows the test socket is exclusive (``SO_EXCLUSIVEADDRUSE``), like the
    service's own socket, so a port that another process has bound fails.
    Elsewhere it uses ``SO_REUSEADDR`` like the service's socket: a port with
    only closed connections waiting (TIME_WAIT, after a restart) is free, a
    port another socket listens on is not.
    """
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        if sys.platform == "win32":
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        else:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind((host, port))
    except OSError:
        return False
    finally:
        sock.close()
    return True


def choose_port(
    option: PortOption,
    default: int,
    fallback: Iterable[int],
    *,
    what: str,
    flag: str,
    taken: Iterable[int] = (),
    bindable: Callable[[int], bool] = can_bind,
) -> int:
    """The port for a new service.

    Args:
        option: The command-line value (a number, ``"auto"``) or None.
        default: The default port (8000 or 3000).
        fallback: The ports ``auto`` tries when the default is busy.
        what: The service's name for messages ("REST API", "MCP server").
        flag: The option that sets this port ("--port", "--mcp-port").
        taken: Ports already chosen for the other service in this run.
        bindable: The bind test (injected in tests).

    Raises:
        ServiceCommandError: exit 4 when no port is free.
    """
    taken = set(taken)
    if option == AUTO:
        for port in (default, *fallback):
            if port not in taken and bindable(port):
                return port
        first, last = _bounds(fallback)
        raise ServiceCommandError(
            ExitCode.PORT_BUSY,
            f"No free port for the {what}: {default} and {first}-{last} are in use.",
        )
    port = default if option is None else int(option)
    if port in taken or not bindable(port):
        raise ServiceCommandError(
            ExitCode.PORT_BUSY,
            f"Port {port} for the {what} is in use. Pass {flag} auto or another port.",
        )
    return port


def _bounds(ports: Iterable[int]) -> tuple[int, int]:
    values = list(ports)
    return (values[0], values[-1]) if values else (0, 0)
