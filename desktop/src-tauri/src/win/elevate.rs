// Runs a program elevated through the UAC consent prompt and returns its exit
// code. ShellExecuteExW runs on its own thread with a single-threaded COM
// apartment, as the shell requires; the working directory is System32, and
// the window is hidden (the backend is a console program).

use std::io;
use std::mem;
use std::os::windows::io::{AsRawHandle, FromRawHandle, OwnedHandle};
use std::path::{Path, PathBuf};
use std::ptr;
use std::time::Duration;

use windows_sys::Win32::Foundation::{GetLastError, HWND, WAIT_OBJECT_0, WAIT_TIMEOUT};
use windows_sys::Win32::System::Com::{
    CoInitializeEx, CoUninitialize, COINIT_APARTMENTTHREADED, COINIT_DISABLE_OLE1DDE,
};
use windows_sys::Win32::System::Threading::{GetExitCodeProcess, WaitForSingleObject};
use windows_sys::Win32::UI::Shell::{
    ShellExecuteExW, SEE_MASK_NOASYNC, SEE_MASK_NOCLOSEPROCESS, SHELLEXECUTEINFOW,
};
use windows_sys::Win32::UI::WindowsAndMessaging::SW_HIDE;

use super::{paths, wide};
use crate::service::Elevated;

/// Start `exe` with `params` (already quoted, see args.rs) through the "runas"
/// verb and wait up to `wait` for it to finish. `hwnd` is the window that owns
/// the consent prompt (0 for none).
pub fn run_elevated(exe: &Path, params: &str, hwnd: isize, wait: Duration) -> io::Result<Elevated> {
    let exe = exe.to_path_buf();
    let params = params.to_string();
    let directory = paths::system_dir()?;
    std::thread::Builder::new()
        .name("rootmind-elevate".into())
        .spawn(move || with_sta(|| shell_execute(&exe, &params, &directory, hwnd, wait)))?
        .join()
        .map_err(|_| io::Error::other("the elevation thread panicked"))?
}

/// Run `f` inside a single-threaded COM apartment on the current thread.
fn with_sta<T>(f: impl FnOnce() -> T) -> T {
    // SAFETY: initializes COM for this thread; balanced by CoUninitialize below
    // when it succeeded (S_OK or S_FALSE).
    let hr = unsafe {
        CoInitializeEx(
            ptr::null(),
            (COINIT_APARTMENTTHREADED | COINIT_DISABLE_OLE1DDE) as u32,
        )
    };
    let result = f();
    if hr >= 0 {
        // SAFETY: matches the successful CoInitializeEx on this thread.
        unsafe { CoUninitialize() };
    }
    result
}

fn shell_execute(
    exe: &PathBuf,
    params: &str,
    directory: &Path,
    hwnd: isize,
    wait: Duration,
) -> io::Result<Elevated> {
    let verb = wide("runas")?;
    let file = wide(exe)?;
    let parameters = wide(params)?;
    let dir = wide(directory)?;

    // SAFETY: SHELLEXECUTEINFOW is plain data; all zeros is a valid start.
    let mut info: SHELLEXECUTEINFOW = unsafe { mem::zeroed() };
    info.cbSize = mem::size_of::<SHELLEXECUTEINFOW>() as u32;
    info.fMask = SEE_MASK_NOCLOSEPROCESS | SEE_MASK_NOASYNC;
    info.hwnd = hwnd as HWND;
    info.lpVerb = verb.as_ptr();
    info.lpFile = file.as_ptr();
    info.lpParameters = parameters.as_ptr();
    info.lpDirectory = dir.as_ptr();
    info.nShow = SW_HIDE;

    // SAFETY: every string pointer in `info` refers to a NUL-terminated buffer
    // that lives until the call returns.
    if unsafe { ShellExecuteExW(&mut info) } == 0 {
        // SAFETY: reads this thread's last error, set by ShellExecuteExW.
        let code = unsafe { GetLastError() };
        return Ok(Elevated::LaunchFailed(code));
    }
    if info.hProcess.is_null() {
        // The shell started nothing we can wait for (for example a DDE
        // hand-off); the outcome is unknown.
        return Err(io::Error::other(
            "the elevated program returned no process handle",
        ));
    }
    // SAFETY: SEE_MASK_NOCLOSEPROCESS hands the process handle to us.
    let process = unsafe { OwnedHandle::from_raw_handle(info.hProcess) };

    let millis = u32::try_from(wait.as_millis()).unwrap_or(u32::MAX - 1);
    // SAFETY: `process` is a valid process handle.
    match unsafe { WaitForSingleObject(process.as_raw_handle(), millis) } {
        WAIT_OBJECT_0 => {}
        WAIT_TIMEOUT => {
            return Err(io::Error::new(
                io::ErrorKind::TimedOut,
                "the elevated program did not finish in time",
            ))
        }
        _ => return Err(io::Error::last_os_error()),
    }
    let mut code = 0u32;
    // SAFETY: `process` is valid and `code` is a valid out pointer.
    if unsafe { GetExitCodeProcess(process.as_raw_handle(), &mut code) } == 0 {
        return Err(io::Error::last_os_error());
    }
    Ok(Elevated::Exited(code))
}

#[cfg(test)]
mod tests {
    use super::*;

    /// GitHub's Windows runners use an administrator account without a consent
    /// prompt, so "runas" starts the program directly. Anywhere else this test
    /// would open a UAC prompt, so it only runs there.
    #[test]
    fn runs_a_program_elevated_and_returns_its_exit_code() {
        if std::env::var("GITHUB_ACTIONS").as_deref() != Ok("true") {
            eprintln!("skipped: needs a GitHub Actions Windows runner (no UAC prompt there)");
            return;
        }
        let cmd = paths::system_dir().unwrap().join("cmd.exe");
        let result = run_elevated(&cmd, "/d /c exit 7", 0, Duration::from_secs(60)).unwrap();
        assert_eq!(result, Elevated::Exited(7));
    }
}
