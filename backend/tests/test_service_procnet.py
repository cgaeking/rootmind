"""Who listens on a port, on Linux: parsing /proc/net/tcp(6) runs everywhere."""

from __future__ import annotations

import socket
import struct
import sys

import pytest

from rootmind.service import procnet

HEADER = (
    "  sl  local_address rem_address   st tx_queue rx_queue tr tm->when retrnsmt   uid  timeout "
    "inode\n"
)


def v4(address: str) -> str:
    return f"{struct.unpack('=I', socket.inet_aton(address))[0]:08X}"


def v6(address: str) -> str:
    words = struct.unpack("=4I", socket.inet_pton(socket.AF_INET6, address))
    return "".join(f"{w:08X}" for w in words)


def row(local: str, port: int, state: int, uid: int, inode: int) -> str:
    return (
        f"   0: {local}:{port:04X} 00000000:0000 {state:02X} 00000000:00000000 00:00000000 "
        f"00000000 {uid:5d}        0 {inode} 1 0000000000000000 100 0 0 10 0\n"
    )


def test_parse_ipv4_and_ipv6():
    text = (
        HEADER + row(v4("127.0.0.1"), 8000, 0x0A, 1000, 111) + row(v4("10.0.0.1"), 22, 0x01, 0, 5)
    )
    assert procnet.parse(text) == [procnet.Listener("127.0.0.1", 8000, 111, 1000)]
    text6 = HEADER + row(v6("::"), 3000, 0x0A, 1001, 222) + row(v6("::1"), 3001, 0x0A, 1, 9)
    assert procnet.parse(text6) == [
        procnet.Listener("::", 3000, 222, 1001),
        procnet.Listener("::1", 3001, 9, 1),
    ]


def test_parse_skips_malformed_lines():
    text = HEADER + "garbage\n" + "   1: ZZ:1F40 0:0 0A 0:0 0:0 0 1 0 7\n"
    assert procnet.parse(text) == []


@pytest.mark.parametrize(
    ("listeners", "owned"),
    [
        ([procnet.Listener("127.0.0.1", 8000, 1, 1000)], True),
        ([procnet.Listener("127.0.0.1", 8000, 2, 1000)], False),  # another socket
        ([], False),  # nobody listens
        ([procnet.Listener("127.0.0.1", 8001, 1, 1000)], False),  # another port
        (
            [procnet.Listener("127.0.0.1", 8000, 1, 1000), procnet.Listener("0.0.0.0", 8000, 9, 0)],  # noqa: S104
            False,
        ),
        (
            [procnet.Listener("127.0.0.1", 8000, 1, 1000), procnet.Listener("::", 8000, 9, 1001)],
            False,
        ),
        (
            [
                procnet.Listener("127.0.0.1", 8000, 1, 1000),
                procnet.Listener("::ffff:127.0.0.1", 8000, 9, 1001),
            ],
            False,
        ),
        # Only listeners that receive 127.0.0.1 count.
        (
            [
                procnet.Listener("127.0.0.1", 8000, 1, 1000),
                procnet.Listener("192.168.1.5", 8000, 9, 1),
            ],
            True,
        ),
        ([procnet.Listener("127.0.0.1", 8000, 1, 1000), procnet.Listener("::1", 8000, 9, 1)], True),
    ],
)
def test_port_owned_by(listeners, owned):
    assert procnet.port_owned_by(listeners, 8000, {1}) is owned


def test_a_fake_proc_tree(tmp_path):
    (tmp_path / "net").mkdir()
    (tmp_path / "net" / "tcp").write_text(HEADER + row(v4("127.0.0.1"), 8000, 0x0A, 1000, 555))
    assert procnet.port_owned_by(procnet.listeners(tmp_path), 8000, {555})
    assert procnet.port_owner_is(8000, 0, tmp_path) is False
    assert procnet.port_owner_is(8000, 42, tmp_path) is False  # no /proc/42/fd


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="reads the real /proc")
def test_this_process_owns_its_listening_socket():
    import os

    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        sock.listen()
        port = sock.getsockname()[1]
        assert procnet.port_owner_is(port, os.getpid())
        assert not procnet.port_owner_is(port, 1)
    assert not procnet.port_owner_is(port, os.getpid())
