// Win32 calls for the background service helpers (service.rs). Everything
// here runs unelevated in the app's process, except the program started by
// elevate.rs. Paths come from the known-folder API, never from environment
// variables.

pub mod elevate;
pub mod paths;
pub mod scm;
pub mod tcp;
pub mod token;

use std::ffi::OsStr;
use std::io;
use std::os::windows::ffi::OsStrExt;

/// A NUL-terminated UTF-16 copy of `s`. Interior NULs are refused, because
/// Win32 would silently cut the string there.
pub(crate) fn wide(s: impl AsRef<OsStr>) -> io::Result<Vec<u16>> {
    let mut out: Vec<u16> = s.as_ref().encode_wide().collect();
    if out.contains(&0) {
        return Err(io::Error::new(
            io::ErrorKind::InvalidInput,
            "string contains a NUL character",
        ));
    }
    out.push(0);
    Ok(out)
}

/// Read a NUL-terminated UTF-16 string.
///
/// # Safety
/// `ptr` must be null or point to a NUL-terminated UTF-16 string that stays
/// valid for the duration of the call.
pub(crate) unsafe fn from_wide_ptr(ptr: *const u16) -> String {
    if ptr.is_null() {
        return String::new();
    }
    let mut len = 0usize;
    // SAFETY: the caller guarantees a NUL terminator, so the scan stays inside
    // the string.
    while unsafe { *ptr.add(len) } != 0 {
        len += 1;
    }
    // SAFETY: the `len` values before the terminator were just read.
    let slice = unsafe { std::slice::from_raw_parts(ptr, len) };
    String::from_utf16_lossy(slice)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn wide_appends_a_terminator_and_refuses_nul() {
        assert_eq!(wide("ab").unwrap(), vec![97, 98, 0]);
        assert!(wide("a\0b").is_err());
    }

    #[test]
    fn from_wide_ptr_reads_until_the_terminator() {
        let buf = wide("RootMind").unwrap();
        // SAFETY: `buf` is NUL-terminated and alive.
        assert_eq!(unsafe { from_wide_ptr(buf.as_ptr()) }, "RootMind");
        // SAFETY: null is allowed.
        assert_eq!(unsafe { from_wide_ptr(std::ptr::null()) }, "");
    }
}
