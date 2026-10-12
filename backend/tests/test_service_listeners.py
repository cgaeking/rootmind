"""The IPv4 listener table: parsing (every platform) and the live table (Windows)."""

from __future__ import annotations

import os
import socket
import struct
import sys

import pytest

from rootmind.service import listeners
from rootmind.service.listeners import STATE_LISTEN, Listener


def table(rows: list[tuple[int, bytes, int, int]]) -> bytes:
    """A MIB_TCPTABLE_OWNER_PID buffer laid out as the OS lays it out in memory."""
    buffer = struct.pack("=I", len(rows))
    for state, address, port, pid in rows:
        buffer += struct.pack("=I", state)
        buffer += address
        buffer += struct.pack(">H", port) + b"\0\0"
        buffer += b"\0" * 8  # remote address and port
        buffer += struct.pack("=I", pid)
    return buffer


LOOPBACK = bytes([127, 0, 0, 1])
ANY = bytes(4)


def test_parses_listening_rows_only():
    parsed = listeners.parse(
        table(
            [
                (STATE_LISTEN, LOOPBACK, 8000, 4242),
                (STATE_LISTEN, ANY, 445, 4),
                (5, LOOPBACK, 50000, 99),  # ESTABLISHED
            ]
        )
    )
    assert parsed == [Listener("127.0.0.1", 8000, 4242), Listener("0.0.0.0", 445, 4)]  # noqa: S104


def test_port_bytes_are_network_order():
    buffer = table([(STATE_LISTEN, LOOPBACK, 18300, 1)])
    assert buffer[4 + 8 : 4 + 10] == b"\x47\x7c"
    assert listeners.parse(buffer)[0].port == 18300


def test_short_buffers_are_rejected():
    assert listeners.parse(b"") is None
    assert listeners.parse(b"\x01\x00") is None
    buffer = table([(STATE_LISTEN, LOOPBACK, 8000, 1)])
    assert listeners.parse(buffer[:-1]) is None
    assert listeners.parse(struct.pack("=I", 2) + buffer[4:]) is None
    assert listeners.parse(struct.pack("=I", 0)) == []


def test_owner_check():
    parsed = listeners.parse(
        table(
            [
                (STATE_LISTEN, LOOPBACK, 8000, 4242),
                (STATE_LISTEN, bytes([192, 168, 1, 5]), 9000, 7),
                (STATE_LISTEN, LOOPBACK, 3000, 4242),
            ]
        )
    )
    assert listeners.port_owned_by(parsed, 8000, 4242)
    assert not listeners.port_owned_by(parsed, 8000, 1111)  # another program owns it
    assert not listeners.port_owned_by(parsed, 8001, 4242)  # nobody listens
    assert not listeners.port_owned_by(parsed, 9000, 7)  # only loopback and wildcard count
    zero = listeners.parse(table([(STATE_LISTEN, LOOPBACK, 8000, 0)]))
    assert not listeners.port_owned_by(zero, 8000, 0)


def test_a_second_listener_with_another_owner_fails_the_check():
    parsed = listeners.parse(
        table([(STATE_LISTEN, LOOPBACK, 8000, 4242), (STATE_LISTEN, ANY, 8000, 666)])
    )
    assert not listeners.port_owned_by(parsed, 8000, 4242)


@pytest.mark.skipif(sys.platform != "win32", reason="GetExtendedTcpTable is Windows only")
def test_finds_our_own_listener():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        sock.listen()
        port = sock.getsockname()[1]
        assert listeners.port_owner_is(port, os.getpid())
        assert not listeners.port_owner_is(port, os.getpid() + 1)


def test_an_unreadable_table_means_not_owned(monkeypatch):
    def broken():
        raise OSError("no table")

    monkeypatch.setattr(listeners, "listeners", broken)
    assert listeners.port_owner_is(8000, 4242) is False
