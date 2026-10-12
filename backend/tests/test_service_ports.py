"""Port choice for the services (--port / --mcp-port)."""

import socket

import pytest

from rootmind.service import API_FALLBACK_PORTS, MCP_FALLBACK_PORTS, ExitCode, ServiceCommandError
from rootmind.service.ports import AUTO, can_bind, choose_port, parse_port_option


def bindable_except(*busy: int):
    def bindable(port: int) -> bool:
        return port not in busy

    return bindable


def choose(option, *, busy=(), taken=(), default=8000, fallback=API_FALLBACK_PORTS):
    return choose_port(
        option,
        default,
        fallback,
        what="REST API",
        flag="--port",
        taken=taken,
        bindable=bindable_except(*busy),
    )


@pytest.mark.parametrize(
    ("text", "expected"),
    [("auto", AUTO), ("AUTO", AUTO), (" auto ", AUTO), ("8000", 8000), ("1", 1), ("65535", 65535)],
)
def test_parse_port_option(text, expected):
    assert parse_port_option(text) == expected


@pytest.mark.parametrize("text", ["0", "65536", "-1", "x", "", "80.5"])
def test_parse_port_option_refuses(text):
    with pytest.raises(ValueError):
        parse_port_option(text)


def test_no_option_is_the_strict_default():
    assert choose(None) == 8000
    with pytest.raises(ServiceCommandError) as raised:
        choose(None, busy=[8000])
    assert raised.value.code == ExitCode.PORT_BUSY
    assert "--port auto" in raised.value.message


def test_explicit_port_is_strict():
    assert choose(18123) == 18123
    with pytest.raises(ServiceCommandError) as raised:
        choose(18123, busy=[18123])
    assert raised.value.code == ExitCode.PORT_BUSY


def test_auto_prefers_the_default():
    assert choose(AUTO) == 8000


def test_auto_falls_back_to_the_first_free_port_of_the_range():
    assert choose(AUTO, busy=[8000]) == 18300
    assert choose(AUTO, busy=[8000, 18300, 18301]) == 18302


def test_auto_mcp_range():
    assert choose(AUTO, default=3000, fallback=MCP_FALLBACK_PORTS, busy=[3000]) == 18400


def test_auto_fails_when_everything_is_busy():
    with pytest.raises(ServiceCommandError) as raised:
        choose(AUTO, busy=[8000, *API_FALLBACK_PORTS])
    assert raised.value.code == ExitCode.PORT_BUSY
    assert "18300-18399" in raised.value.message


def test_ports_taken_by_the_other_service_are_skipped():
    assert choose(AUTO, default=3000, fallback=MCP_FALLBACK_PORTS, taken=[3000]) == 18400
    with pytest.raises(ServiceCommandError):
        choose(3000, default=3000, taken=[3000])


def test_fallback_ranges_lie_outside_the_dynamic_range():
    for ports in (API_FALLBACK_PORTS, MCP_FALLBACK_PORTS):
        assert ports[0] >= 18300 and ports[-1] < 49152
        assert len(ports) == 100
    assert not set(API_FALLBACK_PORTS) & set(MCP_FALLBACK_PORTS)


def test_can_bind_sees_a_listening_socket():
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    sock.listen(1)
    port = sock.getsockname()[1]
    try:
        assert not can_bind(port)
    finally:
        sock.close()
    assert can_bind(port)
