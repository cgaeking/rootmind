"""Which process listens on a local TCP port (the IPv4 listener table).

``service install`` sends the REST and MCP tokens to a freshly started
service only after checking that the socket listening on its port belongs to
the service's process. Until the service has bound its port, another program
(for example of another account, or WSL port forwarding) could have bound it
first and would receive the tokens.

The table comes from ``GetExtendedTcpTable(AF_INET,
TCP_TABLE_OWNER_PID_LISTENER)``, which needs no process handle. Parsing is
kept apart from the Win32 call so it can be tested on every platform. Layout
(``MIB_TCPTABLE_OWNER_PID``): a u32 entry count, then one
``MIB_TCPROW_OWNER_PID`` per entry with six u32 fields: state, local address,
local port, remote address, remote port, owning PID. Addresses are in network
byte order; a port is the first two bytes of its field, also in network byte
order. The desktop app reads the same table (``src-tauri/src/tcptable.rs``).
"""

from __future__ import annotations

import ctypes
import socket
import struct
from dataclasses import dataclass

STATE_LISTEN = 2  # MIB_TCP_STATE_LISTEN
_ROW_BYTES = 24
_LOOPBACK = "127.0.0.1"
_ANY = "0.0.0.0"  # noqa: S104 (compared with, never bound)

_AF_INET = 2
_TCP_TABLE_OWNER_PID_LISTENER = 3
_NO_ERROR = 0
_ERROR_INSUFFICIENT_BUFFER = 122


@dataclass(frozen=True)
class Listener:
    """One listening IPv4 socket."""

    address: str
    port: int
    pid: int


def parse(buffer: bytes) -> list[Listener] | None:
    """Parse a ``MIB_TCPTABLE_OWNER_PID`` buffer; rows that are not listening are skipped.

    Returns None when the buffer is shorter than its entry count says.
    """
    if len(buffer) < 4:
        return None
    (count,) = struct.unpack_from("=I", buffer, 0)
    if (len(buffer) - 4) // _ROW_BYTES < count:
        return None
    listeners = []
    for index in range(count):
        offset = 4 + index * _ROW_BYTES
        (state,) = struct.unpack_from("=I", buffer, offset)
        if state != STATE_LISTEN:
            continue
        address = socket.inet_ntoa(bytes(buffer[offset + 4 : offset + 8]))
        (port,) = struct.unpack_from(">H", buffer, offset + 8)
        (pid,) = struct.unpack_from("=I", buffer, offset + 20)
        listeners.append(Listener(address, port, pid))
    return listeners


def port_owned_by(listeners: list[Listener], port: int, pid: int) -> bool:
    """Whether every listener on 127.0.0.1:``port`` or 0.0.0.0:``port`` belongs to ``pid``.

    There must be at least one, and PID 0 never matches.
    """
    found = False
    for listener in listeners:
        if listener.port != port or listener.address not in (_LOOPBACK, _ANY):
            continue
        if listener.pid != pid:
            return False
        found = True
    return found and pid != 0


def listeners() -> list[Listener]:
    """All IPv4 listening sockets and their owning processes (Windows only).

    Raises:
        OSError: the table cannot be read.
    """
    iphlpapi = ctypes.WinDLL("iphlpapi")  # type: ignore[attr-defined]
    get_table = iphlpapi.GetExtendedTcpTable
    get_table.restype = ctypes.c_uint32
    get_table.argtypes = [
        ctypes.c_void_p,
        ctypes.POINTER(ctypes.c_uint32),
        ctypes.c_int,
        ctypes.c_uint32,
        ctypes.c_int,
        ctypes.c_uint32,
    ]
    size = ctypes.c_uint32(0)
    buffer = None
    # The table can grow between the size query and the read; retry a few times.
    for _ in range(5):
        status = get_table(
            buffer, ctypes.byref(size), 0, _AF_INET, _TCP_TABLE_OWNER_PID_LISTENER, 0
        )
        if status == _NO_ERROR and buffer is not None:
            parsed = parse(ctypes.string_at(buffer, min(size.value, ctypes.sizeof(buffer))))
            if parsed is None:
                raise OSError("malformed TCP table")
            return parsed
        if status in (_NO_ERROR, _ERROR_INSUFFICIENT_BUFFER):
            # A u32 array keeps the buffer aligned for the table's u32 fields.
            buffer = (ctypes.c_uint32 * (size.value // 4 + 16))()
            size.value = ctypes.sizeof(buffer)
            continue
        raise ctypes.WinError(status)  # type: ignore[attr-defined]
    raise OSError("the TCP table kept growing while it was read")


def port_owner_is(port: int, pid: int) -> bool:
    """Whether the listener on 127.0.0.1:``port`` belongs to ``pid``; False if unknown."""
    try:
        return port_owned_by(listeners(), port, pid)
    except OSError:
        return False
