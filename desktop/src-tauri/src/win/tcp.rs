// The IPv4 TCP listener table with owning process IDs. Reading it needs no
// process handle, so it works for a service running as SYSTEM.

use std::ffi::c_void;
use std::io;

use windows_sys::Win32::Foundation::{ERROR_INSUFFICIENT_BUFFER, NO_ERROR};
use windows_sys::Win32::NetworkManagement::IpHelper::{
    GetExtendedTcpTable, TCP_TABLE_OWNER_PID_LISTENER,
};
use windows_sys::Win32::Networking::WinSock::AF_INET;

use crate::tcptable::{self, Listener};

/// All IPv4 listening sockets and their owners.
pub fn listeners() -> io::Result<Vec<Listener>> {
    let mut size = 0u32;
    // u32 elements keep the buffer aligned for the table's u32 fields.
    let mut buf: Vec<u32> = Vec::new();
    // The table can grow between the size query and the read; retry a few times.
    for _ in 0..5 {
        // SAFETY: `buf` holds at least `size` bytes (or is empty with size 0,
        // in which case the call only reports the size needed).
        let status = unsafe {
            GetExtendedTcpTable(
                buf.as_mut_ptr() as *mut c_void,
                &mut size,
                0,
                AF_INET as u32,
                TCP_TABLE_OWNER_PID_LISTENER,
                0,
            )
        };
        match status {
            NO_ERROR => {
                let len = (size as usize).min(buf.len() * 4);
                // SAFETY: the first `len` bytes of `buf` were written by the call
                // and lie inside the allocation.
                let bytes = unsafe { std::slice::from_raw_parts(buf.as_ptr() as *const u8, len) };
                return tcptable::parse(bytes)
                    .ok_or_else(|| io::Error::other("malformed TCP table"));
            }
            ERROR_INSUFFICIENT_BUFFER => {
                buf = vec![0; (size as usize).div_ceil(4) + 16];
                size = (buf.len() * 4) as u32;
            }
            other => return Err(io::Error::from_raw_os_error(other as i32)),
        }
    }
    Err(io::Error::other(
        "the TCP table kept growing while it was read",
    ))
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::net::TcpListener;

    #[test]
    fn finds_our_own_listener() {
        let socket = TcpListener::bind("127.0.0.1:0").unwrap();
        let port = socket.local_addr().unwrap().port();
        let table = listeners().unwrap();
        let pid = std::process::id();
        assert!(tcptable::port_owned_by(&table, port, pid));
        assert!(!tcptable::port_owned_by(&table, port, pid + 1));
    }
}
