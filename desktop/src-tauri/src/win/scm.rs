// Service queries that standard users may make: the service's ImagePath from
// HKLM (readable by Users) and its state and process ID from the Service
// Control Manager (SERVICE_QUERY_STATUS is granted to interactive users).

use std::ffi::c_void;
use std::io;
use std::mem;
use std::ptr;

use windows_sys::Win32::Foundation::{
    GetLastError, ERROR_FILE_NOT_FOUND, ERROR_MORE_DATA, ERROR_PATH_NOT_FOUND,
    ERROR_SERVICE_DOES_NOT_EXIST, ERROR_SUCCESS,
};
use windows_sys::Win32::System::Registry::{
    RegGetValueW, HKEY_LOCAL_MACHINE, RRF_NOEXPAND, RRF_RT_REG_EXPAND_SZ, RRF_RT_REG_SZ,
};
use windows_sys::Win32::System::Services::{
    CloseServiceHandle, OpenSCManagerW, OpenServiceW, QueryServiceStatusEx, SC_HANDLE,
    SC_MANAGER_CONNECT, SC_STATUS_PROCESS_INFO, SERVICE_QUERY_STATUS, SERVICE_STATUS_PROCESS,
};

use super::wide;
use crate::service::ScmStatus;

/// Closes a service or SCM handle on drop.
struct ScHandle(SC_HANDLE);

impl Drop for ScHandle {
    fn drop(&mut self) {
        // SAFETY: the handle is valid and owned by this guard.
        unsafe { CloseServiceHandle(self.0) };
    }
}

/// The state and process ID of service `name`, or `None` if it does not exist.
pub fn query(name: &str) -> io::Result<Option<ScmStatus>> {
    // SAFETY: null machine and database names select the local SCM.
    let scm = unsafe { OpenSCManagerW(ptr::null(), ptr::null(), SC_MANAGER_CONNECT) };
    if scm.is_null() {
        return Err(io::Error::last_os_error());
    }
    let scm = ScHandle(scm);
    let wname = wide(name)?;
    // SAFETY: `scm` is open and `wname` is NUL-terminated.
    let service = unsafe { OpenServiceW(scm.0, wname.as_ptr(), SERVICE_QUERY_STATUS) };
    if service.is_null() {
        // SAFETY: reads this thread's last error, set by OpenServiceW.
        let err = unsafe { GetLastError() };
        if err == ERROR_SERVICE_DOES_NOT_EXIST {
            return Ok(None);
        }
        return Err(io::Error::from_raw_os_error(err as i32));
    }
    let service = ScHandle(service);

    // SAFETY: SERVICE_STATUS_PROCESS is plain data; all zeros is valid.
    let mut status: SERVICE_STATUS_PROCESS = unsafe { mem::zeroed() };
    let mut needed = 0u32;
    // SAFETY: the buffer is a SERVICE_STATUS_PROCESS of the size passed.
    let ok = unsafe {
        QueryServiceStatusEx(
            service.0,
            SC_STATUS_PROCESS_INFO,
            &mut status as *mut SERVICE_STATUS_PROCESS as *mut u8,
            mem::size_of::<SERVICE_STATUS_PROCESS>() as u32,
            &mut needed,
        )
    };
    if ok == 0 {
        return Err(io::Error::last_os_error());
    }
    Ok(Some(ScmStatus {
        state: status.dwCurrentState,
        pid: status.dwProcessId,
    }))
}

/// The raw ImagePath of service `name` (environment strings not expanded), or
/// `None` if the service key or value does not exist.
pub fn image_path(name: &str) -> io::Result<Option<String>> {
    let subkey = wide(format!(r"SYSTEM\CurrentControlSet\Services\{name}"))?;
    let value = wide("ImagePath")?;
    let flags = RRF_RT_REG_SZ | RRF_RT_REG_EXPAND_SZ | RRF_NOEXPAND;
    let mut buf: Vec<u16> = vec![0; 512];
    for _ in 0..4 {
        let mut bytes = (buf.len() * 2) as u32;
        // SAFETY: the key and value names are NUL-terminated, and `bytes` is
        // the size of `buf` in bytes.
        let status = unsafe {
            RegGetValueW(
                HKEY_LOCAL_MACHINE,
                subkey.as_ptr(),
                value.as_ptr(),
                flags,
                ptr::null_mut(),
                buf.as_mut_ptr() as *mut c_void,
                &mut bytes,
            )
        };
        match status {
            ERROR_SUCCESS => {
                let len = (bytes as usize / 2).min(buf.len());
                let text = &buf[..len];
                let end = text.iter().position(|&c| c == 0).unwrap_or(text.len());
                return Ok(Some(String::from_utf16_lossy(&text[..end])));
            }
            ERROR_FILE_NOT_FOUND | ERROR_PATH_NOT_FOUND => return Ok(None),
            ERROR_MORE_DATA => buf = vec![0; bytes as usize / 2 + 1],
            other => return Err(io::Error::from_raw_os_error(other as i32)),
        }
    }
    Err(io::Error::other("ImagePath kept growing while it was read"))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn missing_service_is_none() {
        let name = "RootMind-Test-Service-That-Does-Not-Exist";
        assert!(query(name).unwrap().is_none());
        assert!(image_path(name).unwrap().is_none());
    }

    #[test]
    fn reads_a_builtin_service() {
        // The Windows Event Log service exists on every Windows installation.
        let status = query("EventLog").unwrap().expect("EventLog service");
        assert!(status.state >= 1);
        let image = image_path("EventLog").unwrap().expect("EventLog ImagePath");
        assert!(image.to_lowercase().contains("svchost"), "{image}");
    }
}
