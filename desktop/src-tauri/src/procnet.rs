// The listening TCP sockets on Linux, from /proc/net/tcp and /proc/net/tcp6.
//
// The app hands out the service's REST token only when every socket that
// receives connections to 127.0.0.1:<port> belongs to the unit's main
// process (service.rs), as on Windows with the TCP table (tcptable.rs). Linux
// lists the sockets of every account with their inode; a process's own
// sockets are the `socket:[<inode>]` links in /proc/<pid>/fd, which this
// account can read for its own processes. The backend does the same check
// (backend/src/rootmind/service/procnet.py).
//
// Each line: `sl local_address rem_address st ... uid timeout inode ...`;
// an address is hex words in native byte order, the port big-endian hex, the
// state 0A for LISTEN. Parsing is platform-neutral so it is tested
// everywhere; only the Linux build reads /proc (linux/).
#![cfg_attr(not(target_os = "linux"), allow(dead_code))]

use std::collections::HashSet;
use std::net::{IpAddr, Ipv4Addr, Ipv6Addr};

use crate::tcptable::Listener;

const TCP_LISTEN: u32 = 0x0A;

/// One listening socket.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct Socket {
    pub addr: IpAddr,
    pub port: u16,
    pub inode: u64,
}

fn address(hex: &str) -> Option<IpAddr> {
    let word = |i: usize| -> Option<[u8; 4]> {
        let value = u32::from_str_radix(hex.get(i..i + 8)?, 16).ok()?;
        Some(value.to_ne_bytes())
    };
    match hex.len() {
        8 => Some(IpAddr::V4(Ipv4Addr::from(word(0)?))),
        32 => {
            let mut bytes = [0u8; 16];
            for (n, chunk) in bytes.chunks_exact_mut(4).enumerate() {
                chunk.copy_from_slice(&word(n * 8)?);
            }
            Some(IpAddr::V6(Ipv6Addr::from(bytes)))
        }
        _ => None,
    }
}

/// The listening sockets in the text of /proc/net/tcp or tcp6 (the header
/// line and malformed lines are skipped).
pub fn parse(text: &str) -> Vec<Socket> {
    let mut out = Vec::new();
    for line in text.lines().skip(1) {
        let fields: Vec<&str> = line.split_whitespace().collect();
        if fields.len() < 10 {
            continue;
        }
        if u32::from_str_radix(fields[3], 16).ok() != Some(TCP_LISTEN) {
            continue;
        }
        let Some((host, port)) = fields[1].rsplit_once(':') else {
            continue;
        };
        let (Some(addr), Ok(port), Ok(inode)) = (
            address(host),
            u16::from_str_radix(port, 16),
            fields[9].parse::<u64>(),
        ) else {
            continue;
        };
        out.push(Socket { addr, port, inode });
    }
    out
}

/// How a socket's address receives 127.0.0.1, in the terms of the Windows
/// listener table: loopback, wildcard, or not at all (`None`).
fn as_ipv4(addr: IpAddr) -> Option<Ipv4Addr> {
    match addr {
        IpAddr::V4(v4) if v4 == Ipv4Addr::LOCALHOST || v4 == Ipv4Addr::UNSPECIFIED => Some(v4),
        IpAddr::V6(v6) if v6 == Ipv6Addr::UNSPECIFIED => Some(Ipv4Addr::UNSPECIFIED),
        IpAddr::V6(v6) => match v6.to_ipv4_mapped() {
            Some(v4) if v4 == Ipv4Addr::LOCALHOST || v4 == Ipv4Addr::UNSPECIFIED => Some(v4),
            _ => None,
        },
        _ => None,
    }
}

/// The sockets as a listener table for tcptable::port_owned_by: a socket in
/// `inodes` (the process's own) gets `pid`, any other socket PID 0, which
/// never matches. Sockets that cannot receive 127.0.0.1 are left out.
pub fn listeners(sockets: &[Socket], inodes: &HashSet<u64>, pid: u32) -> Vec<Listener> {
    sockets
        .iter()
        .filter_map(|s| {
            as_ipv4(s.addr).map(|addr| Listener {
                addr,
                port: s.port,
                pid: if inodes.contains(&s.inode) { pid } else { 0 },
            })
        })
        .collect()
}

/// The inode of a `socket:[<inode>]` link target.
pub fn socket_inode(target: &str) -> Option<u64> {
    target
        .strip_prefix("socket:[")?
        .strip_suffix(']')?
        .parse()
        .ok()
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::tcptable::port_owned_by;

    const HEADER: &str = "  sl  local_address rem_address   st tx_queue rx_queue tr tm->when retrnsmt   uid  timeout inode\n";

    fn v4(addr: [u8; 4]) -> String {
        format!("{:08X}", u32::from_ne_bytes(addr))
    }

    fn v6(addr: Ipv6Addr) -> String {
        addr.octets()
            .chunks_exact(4)
            .map(|c| format!("{:08X}", u32::from_ne_bytes([c[0], c[1], c[2], c[3]])))
            .collect()
    }

    fn row(local: &str, port: u16, state: u32, inode: u64) -> String {
        format!(
            "   0: {local}:{port:04X} 00000000:0000 {state:02X} 00000000:00000000 00:00000000 00000000  1000        0 {inode} 1 0000000000000000 100 0 0 10 0\n"
        )
    }

    #[test]
    fn parses_ipv4_and_ipv6() {
        let text = format!(
            "{HEADER}{}{}",
            row(&v4([127, 0, 0, 1]), 8000, 0x0A, 111),
            row(&v4([10, 0, 0, 1]), 22, 0x01, 5)
        );
        assert_eq!(
            parse(&text),
            vec![Socket {
                addr: IpAddr::V4(Ipv4Addr::LOCALHOST),
                port: 8000,
                inode: 111
            }]
        );
        let text6 = format!(
            "{HEADER}{}",
            row(&v6(Ipv6Addr::UNSPECIFIED), 3000, 0x0A, 222)
        );
        assert_eq!(parse(&text6)[0].addr, IpAddr::V6(Ipv6Addr::UNSPECIFIED));
        assert_eq!(
            parse(&format!(
                "{HEADER}garbage\n   1: ZZ:1F40 0:0 0A 0 0 0 1 0 7\n"
            )),
            vec![]
        );
    }

    #[test]
    fn only_the_main_process_may_listen() {
        let own: HashSet<u64> = [1].into_iter().collect();
        let sock = |addr: IpAddr, inode| Socket {
            addr,
            port: 8000,
            inode,
        };
        let lo = IpAddr::V4(Ipv4Addr::LOCALHOST);
        let cases = [
            (vec![sock(lo, 1)], true),
            (vec![sock(lo, 2)], false),
            (vec![], false),
            (
                vec![sock(lo, 1), sock(IpAddr::V4(Ipv4Addr::UNSPECIFIED), 9)],
                false,
            ),
            (
                vec![sock(lo, 1), sock(IpAddr::V6(Ipv6Addr::UNSPECIFIED), 9)],
                false,
            ),
            (
                vec![
                    sock(lo, 1),
                    sock(IpAddr::V6(Ipv4Addr::LOCALHOST.to_ipv6_mapped()), 9),
                ],
                false,
            ),
            // Sockets that cannot receive 127.0.0.1 do not count.
            (
                vec![sock(lo, 1), sock(IpAddr::V6(Ipv6Addr::LOCALHOST), 9)],
                true,
            ),
            (
                vec![
                    sock(lo, 1),
                    sock(IpAddr::V4(Ipv4Addr::new(192, 168, 1, 5)), 9),
                ],
                true,
            ),
        ];
        for (sockets, owned) in cases {
            let table = listeners(&sockets, &own, 4242);
            assert_eq!(port_owned_by(&table, 8000, 4242), owned, "{sockets:?}");
        }
    }

    #[test]
    fn socket_links() {
        assert_eq!(socket_inode("socket:[12345]"), Some(12345));
        assert_eq!(socket_inode("pipe:[12345]"), None);
        assert_eq!(socket_inode("socket:[x]"), None);
    }
}
