"""Which process listens on a local TCP port, on Linux (``/proc/net/tcp``, ``tcp6``).

``service install`` sends the tokens to a freshly started service only after
checking that the sockets listening on its port belong to the service's
process (as :mod:`.listeners` does on Windows). Until the service has bound
its port, a program of another account could have bound it first and would
receive the tokens.

``/proc/net/tcp`` lists every socket of the network namespace with its inode,
whoever owns it; ``/proc/<pid>/fd`` names the socket inodes a process holds
(``socket:[<inode>]``), readable for the account's own processes. A port is
owned by a process when every listener that can receive a connection to
``127.0.0.1:<port>`` (``127.0.0.1``, ``0.0.0.0``, ``::``,
``::ffff:127.0.0.1``, ``::ffff:0.0.0.0``) is one of its sockets. Parsing is
kept apart from reading ``/proc`` so it can be tested on every platform. The
desktop app does the same check (``src-tauri/src/linux/procnet.rs``).
"""

from __future__ import annotations

import os
import socket
import struct
from dataclasses import dataclass
from pathlib import Path

TCP_LISTEN = 0x0A
_RELEVANT = frozenset(
    {"127.0.0.1", "0.0.0.0", "::", "::ffff:127.0.0.1", "::ffff:0.0.0.0"}  # noqa: S104 (compared)
)


@dataclass(frozen=True)
class Listener:
    """One listening TCP socket."""

    address: str
    port: int
    inode: int
    uid: int


def _address(field: str) -> str | None:
    """A ``/proc/net/tcp*`` address (native-endian 32-bit words in hex) as text."""
    try:
        if len(field) == 8:
            return socket.inet_ntop(socket.AF_INET, struct.pack("=I", int(field, 16)))
        if len(field) == 32:
            words = [int(field[i : i + 8], 16) for i in range(0, 32, 8)]
            return socket.inet_ntop(socket.AF_INET6, struct.pack("=4I", *words))
    except (ValueError, OSError, struct.error):
        return None
    return None


def parse(text: str) -> list[Listener]:
    """The listening sockets in the text of ``/proc/net/tcp`` or ``/proc/net/tcp6``.

    Malformed lines are skipped; the header line is skipped as well.
    """
    found = []
    for line in text.splitlines()[1:]:
        fields = line.split()
        if len(fields) < 10:
            continue
        local, state, uid, inode = fields[1], fields[3], fields[7], fields[9]
        try:
            if int(state, 16) != TCP_LISTEN:
                continue
            host, _, port = local.rpartition(":")
            address = _address(host)
            if address is None:
                continue
            found.append(Listener(address, int(port, 16), int(inode), int(uid)))
        except ValueError:
            continue
    return found


def port_owned_by(listeners: list[Listener], port: int, inodes: set[int]) -> bool:
    """Whether every listener that receives ``127.0.0.1:port`` is one of ``inodes``.

    There must be at least one such listener.
    """
    found = False
    for listener in listeners:
        if listener.port != port or listener.address not in _RELEVANT:
            continue
        if listener.inode not in inodes:
            return False
        found = True
    return found


def listeners(proc: Path = Path("/proc")) -> list[Listener]:
    """All listening TCP sockets of this network namespace.

    Raises:
        OSError: ``/proc/net/tcp`` cannot be read.
    """
    found = parse((proc / "net" / "tcp").read_text(encoding="ascii", errors="replace"))
    try:
        found += parse((proc / "net" / "tcp6").read_text(encoding="ascii", errors="replace"))
    except FileNotFoundError:
        pass  # IPv6 is turned off
    return found


def socket_inodes(pid: int, proc: Path = Path("/proc")) -> set[int]:
    """The socket inodes the process ``pid`` holds open.

    Raises:
        OSError: its file descriptors cannot be listed (gone, or another account's).
    """
    inodes = set()
    folder = proc / str(pid) / "fd"
    for name in os.listdir(folder):
        try:
            target = os.readlink(folder / name)
        except OSError:
            continue  # closed meanwhile
        if target.startswith("socket:[") and target.endswith("]"):
            try:
                inodes.add(int(target[8:-1]))
            except ValueError:
                continue
    return inodes


def port_owner_is(port: int, pid: int, proc: Path = Path("/proc")) -> bool:
    """Whether the listeners on ``127.0.0.1:port`` belong to ``pid``; False if unknown."""
    if pid <= 0:
        return False
    try:
        return port_owned_by(listeners(proc), port, socket_inodes(pid, proc))
    except OSError:
        return False
