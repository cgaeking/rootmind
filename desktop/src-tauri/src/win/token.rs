// The app's own account: its SID (passed to `service install --user-sid`) and
// whether it may approve UAC prompts as an administrator (owner decision: the
// background service is offered to local administrators only).

use std::ffi::c_void;
use std::io;
use std::mem;
use std::os::windows::io::{AsRawHandle, FromRawHandle, OwnedHandle};
use std::ptr;

use windows_sys::core::{BOOL, PWSTR};
use windows_sys::Win32::Foundation::{LocalFree, HANDLE, HLOCAL};
use windows_sys::Win32::Security::Authorization::ConvertSidToStringSidW;
use windows_sys::Win32::Security::{
    CheckTokenMembership, CreateWellKnownSid, GetTokenInformation, TokenElevationType,
    TokenElevationTypeFull, TokenElevationTypeLimited, TokenLinkedToken, TokenUser,
    WinBuiltinAdministratorsSid, SECURITY_MAX_SID_SIZE, TOKEN_ELEVATION_TYPE,
    TOKEN_INFORMATION_CLASS, TOKEN_LINKED_TOKEN, TOKEN_QUERY, TOKEN_USER,
};
use windows_sys::Win32::System::Threading::{GetCurrentProcess, OpenProcessToken};

use super::from_wide_ptr;

fn process_token() -> io::Result<OwnedHandle> {
    let mut token: HANDLE = ptr::null_mut();
    // SAFETY: GetCurrentProcess returns a pseudo handle that needs no closing;
    // `token` is a valid out pointer.
    let ok = unsafe { OpenProcessToken(GetCurrentProcess(), TOKEN_QUERY, &mut token) };
    if ok == 0 {
        return Err(io::Error::last_os_error());
    }
    // SAFETY: `token` is a new handle that we own from here on.
    Ok(unsafe { OwnedHandle::from_raw_handle(token) })
}

/// Fixed-size token information (`T` must match `class`).
fn token_value<T: Copy>(token: &OwnedHandle, class: TOKEN_INFORMATION_CLASS) -> io::Result<T> {
    // SAFETY: the token information structs used here are plain data.
    let mut value: T = unsafe { mem::zeroed() };
    let mut returned = 0u32;
    // SAFETY: the buffer is a `T` of the size passed.
    let ok = unsafe {
        GetTokenInformation(
            token.as_raw_handle(),
            class,
            &mut value as *mut T as *mut c_void,
            mem::size_of::<T>() as u32,
            &mut returned,
        )
    };
    if ok == 0 {
        return Err(io::Error::last_os_error());
    }
    Ok(value)
}

/// The string SID of the account the app runs as, such as `S-1-5-21-…-1001`.
pub fn current_user_sid() -> io::Result<String> {
    let token = process_token()?;
    let mut needed = 0u32;
    // SAFETY: a size query with a null buffer; it fails with
    // ERROR_INSUFFICIENT_BUFFER and reports the size.
    unsafe {
        GetTokenInformation(
            token.as_raw_handle(),
            TokenUser,
            ptr::null_mut(),
            0,
            &mut needed,
        )
    };
    if needed == 0 {
        return Err(io::Error::last_os_error());
    }
    // u64 elements keep the TOKEN_USER (which holds a pointer) aligned.
    let mut buf: Vec<u64> = vec![0; (needed as usize).div_ceil(8)];
    // SAFETY: `buf` holds at least `needed` bytes.
    let ok = unsafe {
        GetTokenInformation(
            token.as_raw_handle(),
            TokenUser,
            buf.as_mut_ptr() as *mut c_void,
            (buf.len() * 8) as u32,
            &mut needed,
        )
    };
    if ok == 0 {
        return Err(io::Error::last_os_error());
    }
    // SAFETY: the call wrote a TOKEN_USER at the start of the aligned buffer;
    // its SID pointer points into the same buffer, which is still alive.
    let sid = unsafe { (*(buf.as_ptr() as *const TOKEN_USER)).User.Sid };
    let mut text: PWSTR = ptr::null_mut();
    // SAFETY: `sid` is valid (see above) and `text` is a valid out pointer.
    if unsafe { ConvertSidToStringSidW(sid, &mut text) } == 0 {
        return Err(io::Error::last_os_error());
    }
    // SAFETY: on success `text` is a NUL-terminated string from LocalAlloc.
    let result = unsafe { from_wide_ptr(text) };
    // SAFETY: frees the LocalAlloc buffer exactly once.
    unsafe { LocalFree(text as HLOCAL) };
    Ok(result)
}

/// Whether the app's account is a local administrator, so a UAC prompt can be
/// approved with its own credentials.
/// - Limited (UAC on, filtered token): its linked, full token must be a member
///   of Administrators.
/// - Full (the app runs elevated): yes.
/// - Default (UAC off, or no split token): the token itself must be a member.
pub fn can_elevate() -> bool {
    let Ok(token) = process_token() else {
        return false;
    };
    let elevation: TOKEN_ELEVATION_TYPE = match token_value(&token, TokenElevationType) {
        Ok(value) => value,
        Err(_) => return false,
    };
    if elevation == TokenElevationTypeFull {
        return true;
    }
    if elevation != TokenElevationTypeLimited {
        return is_admin_member(None).unwrap_or(false);
    }
    match token_value::<TOKEN_LINKED_TOKEN>(&token, TokenLinkedToken) {
        Ok(linked) if !linked.LinkedToken.is_null() => {
            // SAFETY: the linked token handle is new and owned by us.
            let linked = unsafe { OwnedHandle::from_raw_handle(linked.LinkedToken) };
            is_admin_member(Some(&linked)).unwrap_or(true)
        }
        // Only administrators and a few privileged groups get a split token;
        // the elevated command checks again.
        _ => true,
    }
}

/// Membership of BUILTIN\Administrators. `None` checks the caller's token.
fn is_admin_member(token: Option<&OwnedHandle>) -> io::Result<bool> {
    // u32 elements keep the SID aligned.
    let mut sid = [0u32; SECURITY_MAX_SID_SIZE as usize / 4];
    let mut size = SECURITY_MAX_SID_SIZE;
    // SAFETY: `sid` holds SECURITY_MAX_SID_SIZE bytes, which fits every
    // well-known SID.
    let ok = unsafe {
        CreateWellKnownSid(
            WinBuiltinAdministratorsSid,
            ptr::null_mut(),
            sid.as_mut_ptr() as *mut c_void,
            &mut size,
        )
    };
    if ok == 0 {
        return Err(io::Error::last_os_error());
    }
    let handle = token.map_or(ptr::null_mut(), |t| t.as_raw_handle());
    let mut member: BOOL = 0;
    // SAFETY: `handle` is null (the caller's token) or a valid token handle,
    // and `sid` holds the SID just built.
    let ok = unsafe { CheckTokenMembership(handle, sid.as_mut_ptr() as *mut c_void, &mut member) };
    if ok == 0 {
        return Err(io::Error::last_os_error());
    }
    Ok(member != 0)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn current_user_sid_is_a_user_sid() {
        let sid = current_user_sid().unwrap();
        assert!(crate::imagepath::is_sid(&sid), "{sid}");
    }

    #[test]
    fn can_elevate_does_not_fail() {
        // The answer depends on the account running the tests; it must not panic.
        let _ = can_elevate();
    }
}
