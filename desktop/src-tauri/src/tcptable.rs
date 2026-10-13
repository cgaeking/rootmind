// The IPv4 TCP listener table, as returned by
// GetExtendedTcpTable(AF_INET, TCP_TABLE_OWNER_PID_LISTENER).
//
// The app sends the service's REST token only to the process that owns the
// listening socket, and only when that process is the RootMind service
// (service.rs). Parsing lives here, apart from the Win32 call, so it can be
// tested with synthetic buffers.
//
// Layout (MIB_TCPTABLE_OWNER_PID): a u32 entry count, then one
// MIB_TCPROW_OWNER_PID per entry, six u32 fields each:
//   dwState, dwLocalAddr, dwLocalPort, dwRemoteAddr, dwRemotePort, dwOwningPid.
// Addresses are stored in network byte order; a port is the first two bytes
// of its field, also in network byte order.

// Only the Windows build reads the table (win/tcp.rs); the parser is
// platform-neutral so it can be tested anywhere.
#![cfg_attr(not(windows), allow(dead_code))]

use std::net::Ipv4Addr;

/// MIB_TCP_STATE_LISTEN.
pub const STATE_LISTEN: u32 = 2;
const ROW_BYTES: usize = 24;

/// One listening socket.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct Listener {
    pub addr: Ipv4Addr,
    pub port: u16,
    pub pid: u32,
}

/// Parse a MIB_TCPTABLE_OWNER_PID buffer. Rows that are not listening are
/// skipped. `None` if the buffer is shorter than its entry count says.
pub fn parse(buf: &[u8]) -> Option<Vec<Listener>> {
    let count = read_u32(buf, 0)? as usize;
    let rows = buf.get(4..)?;
    if rows.len() / ROW_BYTES < count {
        return None;
    }
    let mut out = Vec::with_capacity(count);
    for row in rows.chunks_exact(ROW_BYTES).take(count) {
        let state = read_u32(row, 0)?;
        if state != STATE_LISTEN {
            continue;
        }
        let addr = Ipv4Addr::new(row[4], row[5], row[6], row[7]);
        let port = u16::from_be_bytes([row[8], row[9]]);
        let pid = read_u32(row, 20)?;
        out.push(Listener { addr, port, pid });
    }
    Some(out)
}

/// Whether every IPv4 listener on 127.0.0.1:`port` or 0.0.0.0:`port` belongs
/// to `pid`, and there is at least one.
pub fn port_owned_by(listeners: &[Listener], port: u16, pid: u32) -> bool {
    let mut found = false;
    for l in listeners {
        let relevant =
            l.port == port && (l.addr == Ipv4Addr::LOCALHOST || l.addr == Ipv4Addr::UNSPECIFIED);
        if relevant {
            if l.pid != pid {
                return false;
            }
            found = true;
        }
    }
    found && pid != 0
}

fn read_u32(buf: &[u8], at: usize) -> Option<u32> {
    let bytes = buf.get(at..at + 4)?;
    Some(u32::from_ne_bytes(bytes.try_into().ok()?))
}

#[cfg(test)]
mod tests {
    use super::*;

    /// Build a table the way the OS lays it out in memory.
    fn table(rows: &[(u32, [u8; 4], u16, u32)]) -> Vec<u8> {
        let mut buf = Vec::new();
        buf.extend_from_slice(&(rows.len() as u32).to_ne_bytes());
        for &(state, addr, port, pid) in rows {
            buf.extend_from_slice(&state.to_ne_bytes());
            buf.extend_from_slice(&addr);
            let mut port_field = [0u8; 4];
            port_field[..2].copy_from_slice(&port.to_be_bytes());
            buf.extend_from_slice(&port_field);
            buf.extend_from_slice(&[0u8; 4]); // remote address
            buf.extend_from_slice(&[0u8; 4]); // remote port
            buf.extend_from_slice(&pid.to_ne_bytes());
        }
        buf
    }

    #[test]
    fn parses_rows() {
        let buf = table(&[
            (STATE_LISTEN, [127, 0, 0, 1], 8000, 4242),
            (STATE_LISTEN, [0, 0, 0, 0], 445, 4),
            (5, [127, 0, 0, 1], 50000, 99), // ESTABLISHED, skipped
        ]);
        let listeners = parse(&buf).unwrap();
        assert_eq!(
            listeners,
            vec![
                Listener {
                    addr: Ipv4Addr::LOCALHOST,
                    port: 8000,
                    pid: 4242
                },
                Listener {
                    addr: Ipv4Addr::UNSPECIFIED,
                    port: 445,
                    pid: 4
                },
            ]
        );
    }

    #[test]
    fn port_bytes_are_network_order() {
        // 18300 = 0x477C: stored as 47 7C.
        let buf = table(&[(STATE_LISTEN, [127, 0, 0, 1], 18300, 1)]);
        assert_eq!(&buf[4 + 8..4 + 10], &[0x47, 0x7C]);
        assert_eq!(parse(&buf).unwrap()[0].port, 18300);
    }

    #[test]
    fn short_buffers_are_rejected() {
        assert_eq!(parse(&[]), None);
        assert_eq!(parse(&[1, 0]), None);
        let mut buf = table(&[(STATE_LISTEN, [127, 0, 0, 1], 8000, 1)]);
        buf.truncate(buf.len() - 1);
        assert_eq!(parse(&buf), None);
        // The count claims two rows, but there is one.
        let mut buf = table(&[(STATE_LISTEN, [127, 0, 0, 1], 8000, 1)]);
        buf[..4].copy_from_slice(&2u32.to_ne_bytes());
        assert_eq!(parse(&buf), None);
        // An empty table is fine.
        assert_eq!(parse(&0u32.to_ne_bytes()), Some(vec![]));
    }

    #[test]
    fn owner_check() {
        let listeners = parse(&table(&[
            (STATE_LISTEN, [127, 0, 0, 1], 8000, 4242),
            (STATE_LISTEN, [192, 168, 1, 5], 9000, 7),
            (STATE_LISTEN, [127, 0, 0, 1], 3000, 4242),
        ]))
        .unwrap();
        assert!(port_owned_by(&listeners, 8000, 4242));
        // Another process owns the port (a squatter, or WSL forwarding).
        assert!(!port_owned_by(&listeners, 8000, 1111));
        // Nobody listens on the port.
        assert!(!port_owned_by(&listeners, 8001, 4242));
        // Only loopback and wildcard listeners count.
        assert!(!port_owned_by(&listeners, 9000, 7));
        // PID 0 (the idle process) never matches.
        let zero = parse(&table(&[(STATE_LISTEN, [127, 0, 0, 1], 8000, 0)])).unwrap();
        assert!(!port_owned_by(&zero, 8000, 0));
    }

    #[test]
    fn a_second_listener_with_another_owner_fails_the_check() {
        let listeners = parse(&table(&[
            (STATE_LISTEN, [127, 0, 0, 1], 8000, 4242),
            (STATE_LISTEN, [0, 0, 0, 0], 8000, 666),
        ]))
        .unwrap();
        assert!(!port_owned_by(&listeners, 8000, 4242));
    }
}
