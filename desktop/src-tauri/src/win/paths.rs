// Known folders. SHGetKnownFolderPath reads the system configuration, so a
// changed ProgramData or ProgramFiles environment variable has no effect.

use std::ffi::c_void;
use std::io;
use std::path::PathBuf;
use std::ptr;

use windows_sys::core::{GUID, PWSTR};
use windows_sys::Win32::System::Com::CoTaskMemFree;
use windows_sys::Win32::UI::Shell::{
    FOLDERID_ProgramData, FOLDERID_ProgramFiles, FOLDERID_System, SHGetKnownFolderPath,
    KF_FLAG_DEFAULT,
};

use super::from_wide_ptr;

fn known_folder(id: &GUID) -> io::Result<PathBuf> {
    let mut raw: PWSTR = ptr::null_mut();
    // SAFETY: `id` points to a valid GUID and `raw` is a valid out pointer; a
    // null token means the current user.
    let hr = unsafe { SHGetKnownFolderPath(id, KF_FLAG_DEFAULT as u32, ptr::null_mut(), &mut raw) };
    let result = if hr >= 0 && !raw.is_null() {
        // SAFETY: on success `raw` is a NUL-terminated string owned by us.
        Ok(PathBuf::from(unsafe { from_wide_ptr(raw) }))
    } else {
        Err(io::Error::from_raw_os_error(hr))
    };
    // SAFETY: the buffer comes from the COM allocator (or is null, which is
    // allowed) and is freed exactly once.
    unsafe { CoTaskMemFree(raw as *const c_void) };
    result
}

/// `C:\ProgramData` on a default install.
pub fn program_data() -> io::Result<PathBuf> {
    known_folder(&FOLDERID_ProgramData)
}

/// `C:\Program Files` for this 64-bit process.
pub fn program_files() -> io::Result<PathBuf> {
    known_folder(&FOLDERID_ProgramFiles)
}

/// `C:\Windows\System32`, the working directory of elevated commands.
pub fn system_dir() -> io::Result<PathBuf> {
    known_folder(&FOLDERID_System)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn known_folders_are_absolute() {
        for path in [program_data(), program_files(), system_dir()] {
            let path = path.unwrap();
            assert!(path.is_absolute(), "{}", path.display());
            assert!(path.is_dir(), "{}", path.display());
        }
    }
}
