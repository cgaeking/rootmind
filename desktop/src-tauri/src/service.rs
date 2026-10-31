// Background service: RootMind-Backend and RootMind-MCP-HTTP, installed by
// the backend's own `service install` command. Since 2.0 the installer runs it
// (per-machine install) and the service is the app's only backend; the app is
// a client of it.
//
// Reading needs no rights beyond a standard user's: the ImagePath from HKLM,
// the state and process ID from the Service Control Manager, the token file
// under %ProgramData%\RootMind (its ACL grants the installing user read), and
// the TCP listener table.
//
// The token is handed to the UI only after the listener check: the process
// that listens on 127.0.0.1:<port> must be the service's own process. A port
// squatter (another user's program, or WSL forwarding) never receives it.
//
// Changes run elevated (one UAC prompt each) with arguments built here from
// fixed parts:
// - Update and Repair (service_update) run the backend installed with the
//   app (<install folder>\backend), so the service gets the app's version.
//   The installer always installs the app into %ProgramFiles%\RootMind (no
//   folder page; another folder is refused), and `service install` itself
//   refuses to copy from a folder that accounts other than administrators
//   can change (exit 10), so a program planted in a writable folder never
//   becomes the LocalSystem service. Only when the app's backend is missing
//   does Repair fall back to the service's registered admin-only copy.
// - Switching MCP over HTTP runs the service's own copy under
//   %ProgramFiles%\RootMind Service\ (the one in the ImagePath).
// Installing and removing the service is the installer's job.
// The exit codes are the backend's service codes (0 ok, 3 foreign service,
// 4 port busy, 5 not elevated, 6 data folder refused, 7 health check failed,
// 8 account mismatch, 9 no access context, 10 unsafe program folder); 1223
// means the prompt was cancelled.
//
// Linux (linux/): the service is two systemd user units of the account that
// runs the app (~/.config/systemd/user/rootmind-*.service), its data is in
// ~/.local/share/rootmind and it runs from a program copy in its runtime/
// folder. Reading uses the unit files, `systemctl --user show` (state and
// main PID), /proc for the listener check and the owner-only token file.
// Changes need no elevation: the backend that ships with the app (Tauri
// resources: /usr/lib/RootMind/backend, or inside the AppImage) runs
// `service install` as the account itself; switching MCP over HTTP runs the
// registered copy. The app also sets the service up on its first start and
// updates it when it is older than the app (src/state/autoSetup.ts); exit
// code 11 means the account's systemd user manager cannot be reached.

use std::fs::File;
use std::io::{self, Read, Seek, SeekFrom};
use std::path::{Path, PathBuf};
use std::process::{Command, Stdio};
use std::time::{Duration, Instant};

use serde::Serialize;
use tauri::{AppHandle, Manager};

use crate::app;
// Windows only (the UAC prompt); Linux runs the backend directly.
#[cfg_attr(target_os = "linux", allow(unused_imports))]
use crate::args;
use crate::imagepath::{self, ImagePath, Role};
use crate::proof::{self, Proof};
use crate::tcptable;

pub const BACKEND_SERVICE: &str = "RootMind-Backend";
pub const MCP_SERVICE: &str = "RootMind-MCP-HTTP";
const ELEVATION_WAIT: Duration = Duration::from_secs(10 * 60);
const DETAILS_TIMEOUT: Duration = Duration::from_secs(30);
const TOKEN_MAX_BYTES: u64 = 4096;
const INSTALL_LOG_LINES: usize = 60;

// ---------------------------------------------------------------------------
// IPC types (camelCase over IPC)

/// The service's address and REST token, `{url, token}`.
#[derive(Clone, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct BackendInfo {
    pub url: String,
    pub token: String,
}

/// Never prints the token.
impl std::fmt::Debug for BackendInfo {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.debug_struct("BackendInfo")
            .field("url", &self.url)
            .field("token", &"<redacted>")
            .finish()
    }
}

#[derive(Clone, Copy, Debug, PartialEq, Eq, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum SvcState {
    Stopped,
    StartPending,
    StopPending,
    Running,
    Other,
}

impl SvcState {
    /// Map SERVICE_STATUS_PROCESS.dwCurrentState.
    pub fn from_scm(state: u32) -> Self {
        match state {
            1 => Self::Stopped,
            2 => Self::StartPending,
            3 => Self::StopPending,
            4 => Self::Running,
            _ => Self::Other,
        }
    }
}

#[derive(Clone, Copy, Debug, PartialEq, Eq, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum BlockedReason {
    Unsupported,
    NotAdmin,
    OtherAccount,
    ForeignService,
}

#[derive(Clone, Debug, PartialEq, Eq, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct McpInfo {
    pub installed: bool,
    pub state: Option<SvcState>,
    pub port: Option<u16>,
    /// The MCP endpoint, `http://127.0.0.1:<port>/mcp`.
    pub url: Option<String>,
}

#[derive(Clone, Debug, PartialEq, Eq, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct ServiceInfo {
    pub supported: bool,
    pub installed: bool,
    pub ours: bool,
    pub state: Option<SvcState>,
    pub port: Option<u16>,
    pub url: Option<String>,
    pub token_readable: bool,
    pub can_elevate: bool,
    pub blocked_reason: Option<BlockedReason>,
    pub mcp: McpInfo,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum ConnectFailure {
    NotInstalled,
    NotRunning,
    ListenerMismatch,
    TokenUnreadable,
}

/// `{ok: true, url, token} | {ok: false, reason}`
#[derive(Clone, Serialize)]
#[serde(untagged)]
pub enum ServiceConnect {
    Connected {
        ok: bool,
        url: String,
        token: String,
    },
    Failed {
        ok: bool,
        reason: ConnectFailure,
    },
}

impl ServiceConnect {
    fn connected(url: String, token: String) -> Self {
        Self::Connected {
            ok: true,
            url,
            token,
        }
    }

    fn failed(reason: ConnectFailure) -> Self {
        Self::Failed { ok: false, reason }
    }

    fn backend(self) -> Option<BackendInfo> {
        match self {
            Self::Connected { url, token, .. } => Some(BackendInfo { url, token }),
            Self::Failed { .. } => None,
        }
    }
}

/// `{ok: true, backend} | {ok: false, cancelled, code, backend | null}`
#[derive(Clone, Serialize)]
#[serde(untagged)]
pub enum ServiceResult {
    Done {
        ok: bool,
        backend: BackendInfo,
    },
    Failed {
        ok: bool,
        cancelled: bool,
        code: u32,
        backend: Option<BackendInfo>,
    },
}

impl ServiceResult {
    fn done(backend: BackendInfo) -> Self {
        Self::Done { ok: true, backend }
    }

    fn failed(cancelled: bool, code: u32, backend: Option<BackendInfo>) -> Self {
        Self::Failed {
            ok: false,
            cancelled,
            code,
            backend,
        }
    }
}

// ---------------------------------------------------------------------------
// Pure helpers (unit-tested)

/// The state and process ID of a service, as the SCM reports them.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct ScmStatus {
    pub state: u32,
    pub pid: u32,
}

/// How an elevated program ended.
#[cfg_attr(target_os = "linux", allow(dead_code))]
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Elevated {
    /// It ran and exited with this code.
    Exited(u32),
    /// ShellExecuteExW failed with this Win32 error.
    LaunchFailed(u32),
}

/// What the UI is told about an elevated change.
#[cfg_attr(target_os = "linux", allow(dead_code))]
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Outcome {
    Succeeded,
    Cancelled,
    /// The `code` field of ServiceResult.
    Failed(u32),
}

pub const ERROR_CANCELLED: u32 = 1223;

/// Whether a Win32 error code means Windows security blocked a program:
/// ERROR_VIRUS_INFECTED (225), ERROR_VIRUS_DELETED (226),
/// ERROR_ACCESS_DISABLED_BY_POLICY (1260), and the ERROR_SYSTEM_INTEGRITY_*
/// range 4550-4599 (Smart App Control and WDAC, including the reputation
/// checks). The same list is in src/state/service.ts.
#[cfg_attr(target_os = "linux", allow(dead_code))]
pub fn is_blocked_code(code: u32) -> bool {
    matches!(code, 225 | 226 | 1260 | 4550..=4599)
}

/// "Unexpected failure", also used for launch errors without a meaning of
/// their own (they would collide with the service exit codes).
pub const CODE_UNEXPECTED: u32 = 1;
/// The service was installed, but the app could not connect to it.
pub const CODE_HEALTH: u32 = 7;

#[cfg_attr(target_os = "linux", allow(dead_code))]
pub fn classify(result: Elevated) -> Outcome {
    match result {
        Elevated::Exited(0) => Outcome::Succeeded,
        Elevated::Exited(code) => Outcome::Failed(code),
        Elevated::LaunchFailed(ERROR_CANCELLED) => Outcome::Cancelled,
        // Windows security blocked the program: passed to the UI unchanged so
        // it can say so.
        Elevated::LaunchFailed(code) if is_blocked_code(code) => Outcome::Failed(code),
        Elevated::LaunchFailed(_) => Outcome::Failed(CODE_UNEXPECTED),
    }
}

/// `service install` on the existing service: update and repair. Ports, MCP
/// and the account are read back from the service (sticky options). For a
/// missing service, free ports are chosen as the installer does (MCP over
/// HTTP is on by default), so a busy default port does not fail it.
pub fn update_args(user_sid: &str) -> Result<Vec<String>, String> {
    if !imagepath::is_sid(user_sid) {
        return Err("The account SID is malformed.".into());
    }
    Ok([
        "service",
        "install",
        "--port",
        "auto",
        "--mcp-port",
        "auto",
        "--user-sid",
        user_sid,
    ]
    .map(String::from)
    .to_vec())
}

/// The program Update and Repair run: the backend installed with the app
/// (`app_backend`, the app's own version), else the first registered service
/// copy that exists (the backend's, then the MCP service's), which repairs
/// the service at its current version.
pub fn repair_exe(
    app_backend: Option<&Path>,
    views: &[&ServiceView],
    exists: impl Fn(&Path) -> bool,
) -> Result<PathBuf, String> {
    if let Some(exe) = app_backend.filter(|exe| exists(exe)) {
        return Ok(exe.to_path_buf());
    }
    views
        .iter()
        .filter_map(|view| view.image.as_ref())
        .map(|image| PathBuf::from(&image.exe))
        .find(|exe| exists(exe))
        .ok_or_else(|| "The backend that ships with the app is missing. Reinstall RootMind.".into())
}

/// Linux: `service install` on the existing units, as `update_args` without
/// an account (the units belong to the account that runs it).
#[cfg_attr(windows, allow(dead_code))]
pub fn update_args_linux() -> Vec<String> {
    ["service", "install", "--port", "auto", "--mcp-port", "auto"]
        .map(String::from)
        .to_vec()
}

/// How a `service install` the app ran unelevated (Linux) ended: its exit
/// code, or `None` when it could not run, was killed or timed out.
#[cfg_attr(not(target_os = "linux"), allow(dead_code))]
pub fn classify_exit(code: Option<i32>) -> Outcome {
    match code {
        Some(0) => Outcome::Succeeded,
        Some(code) if code > 0 => Outcome::Failed(code as u32),
        _ => Outcome::Failed(CODE_UNEXPECTED),
    }
}

/// `service install` from the Program Files copy, switching MCP over HTTP.
pub fn set_mcp_args(enabled: bool) -> Vec<String> {
    if enabled {
        ["service", "install", "--mcp-http", "--mcp-port", "auto"]
            .map(String::from)
            .to_vec()
    } else {
        ["service", "install", "--no-mcp-http"]
            .map(String::from)
            .to_vec()
    }
}

/// What the app learned about one of the two services.
#[derive(Clone, Debug, Default, PartialEq, Eq)]
pub struct ServiceView {
    /// A service with this name exists.
    pub present: bool,
    /// Its ImagePath follows the contract and points into our Program Files folder.
    pub image: Option<ImagePath>,
    pub status: Option<ScmStatus>,
}

impl ServiceView {
    pub fn ours(&self) -> bool {
        self.image.is_some()
    }

    /// Build a view from a Linux unit file's text and its systemd state
    /// (unitfile.rs): ours when the unit follows the contract and runs the
    /// program copy below `data_root`.
    #[cfg_attr(not(target_os = "linux"), allow(dead_code))]
    pub fn from_unit(
        text: Option<&str>,
        status: Option<ScmStatus>,
        role: Role,
        data_root: Option<&Path>,
    ) -> Self {
        let image = match (text, data_root) {
            (Some(text), Some(root)) => crate::unitfile::parse_unit(text)
                .filter(|ip| ip.role == role && crate::unitfile::exe_is_ours(&ip.exe, root)),
            _ => None,
        };
        Self {
            present: text.is_some(),
            image,
            status,
        }
    }

    /// Build a view from the raw ImagePath and SCM status.
    #[cfg_attr(target_os = "linux", allow(dead_code))]
    pub fn from_parts(
        raw_image: Option<&str>,
        status: Option<ScmStatus>,
        role: Role,
        program_files: Option<&str>,
    ) -> Self {
        let image = match (raw_image, program_files) {
            (Some(raw), Some(pf)) => imagepath::parse(raw)
                .filter(|ip| ip.role == role && imagepath::exe_is_ours(&ip.exe, pf)),
            _ => None,
        };
        Self {
            present: raw_image.is_some() || status.is_some(),
            image,
            status,
        }
    }
}

/// The result of reading a token file.
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum TokenRead {
    Readable(String),
    Missing,
    Denied,
    Invalid,
}

/// Why the toggle cannot be used, in the order the UI should explain it.
pub fn blocked_reason(
    supported: bool,
    backend: &ServiceView,
    mcp: &ServiceView,
    own_sid: Option<&str>,
    token: &TokenRead,
    can_elevate: bool,
) -> Option<BlockedReason> {
    if !supported {
        return Some(BlockedReason::Unsupported);
    }
    if (backend.present && !backend.ours()) || (mcp.present && !mcp.ours()) {
        return Some(BlockedReason::ForeignService);
    }
    if let Some(image) = &backend.image {
        let other_sid = matches!(
            (image.user_sid.as_deref(), own_sid),
            (Some(theirs), Some(mine)) if !theirs.eq_ignore_ascii_case(mine)
        );
        if other_sid || *token == TokenRead::Denied {
            return Some(BlockedReason::OtherAccount);
        }
    }
    if !can_elevate {
        return Some(BlockedReason::NotAdmin);
    }
    None
}

fn same_sid(a: Option<&str>, b: Option<&str>) -> bool {
    matches!((a, b), (Some(a), Some(b)) if a.eq_ignore_ascii_case(b))
}

/// Build ServiceInfo from what was observed.
pub fn build_info(
    supported: bool,
    backend: &ServiceView,
    mcp: &ServiceView,
    own_sid: Option<&str>,
    token: &TokenRead,
    can_elevate: bool,
) -> ServiceInfo {
    let state = |v: &ServiceView| v.status.map(|s| SvcState::from_scm(s.state));
    let port = |v: &ServiceView| v.image.as_ref().map(|ip| ip.port);
    ServiceInfo {
        supported,
        installed: backend.present,
        ours: backend.ours(),
        state: state(backend),
        port: port(backend),
        url: port(backend).map(|p| format!("http://127.0.0.1:{p}")),
        // Readable *and* this account's: with UAC off another administrator
        // can read the file, but the service reads with the first account's
        // rights, so the app must not pick it. A Linux unit has no SID: it
        // is the account's own (its user units).
        token_readable: matches!(token, TokenRead::Readable(_))
            && backend.image.as_ref().is_some_and(|image| {
                image.user_sid.is_none() || same_sid(image.user_sid.as_deref(), own_sid)
            }),
        can_elevate,
        blocked_reason: blocked_reason(supported, backend, mcp, own_sid, token, can_elevate),
        mcp: McpInfo {
            installed: mcp.present,
            state: state(mcp),
            port: port(mcp),
            url: port(mcp).map(|p| format!("http://127.0.0.1:{p}/mcp")),
        },
    }
}

/// Read a token file: at most 4 KB, printable ASCII without spaces.
pub fn read_token(path: &Path) -> TokenRead {
    let file = match File::open(path) {
        Ok(file) => file,
        Err(e) if e.kind() == io::ErrorKind::NotFound => return TokenRead::Missing,
        Err(e) if e.kind() == io::ErrorKind::PermissionDenied => return TokenRead::Denied,
        Err(_) => return TokenRead::Invalid,
    };
    let mut text = String::new();
    if file
        .take(TOKEN_MAX_BYTES + 1)
        .read_to_string(&mut text)
        .is_err()
        || text.len() as u64 > TOKEN_MAX_BYTES
    {
        return TokenRead::Invalid;
    }
    let token = text.trim();
    if token.is_empty() || !token.bytes().all(|b| b.is_ascii_graphic()) {
        return TokenRead::Invalid;
    }
    TokenRead::Readable(token.to_string())
}

/// The last `max_lines` lines of a text file (reading at most its last 64 KB).
pub fn tail_lines(path: &Path, max_lines: usize) -> io::Result<String> {
    const MAX_BYTES: u64 = 64 * 1024;
    let mut file = File::open(path)?;
    let len = file.metadata()?.len();
    let start = len.saturating_sub(MAX_BYTES);
    file.seek(SeekFrom::Start(start))?;
    let mut bytes = Vec::new();
    file.read_to_end(&mut bytes)?;
    let text = String::from_utf8_lossy(&bytes);
    let mut lines: Vec<&str> = text.lines().collect();
    if start > 0 && !lines.is_empty() {
        lines.remove(0); // probably cut in the middle
    }
    let skip = lines.len().saturating_sub(max_lines);
    Ok(lines[skip..].join("\n"))
}

// ---------------------------------------------------------------------------
// Platform layer

#[cfg(windows)]
mod platform {
    pub use crate::win::elevate::run_elevated;
    pub use crate::win::paths::{program_data, program_files};
    pub use crate::win::scm::{image_path, query};
    pub use crate::win::tcp::listeners;
    pub use crate::win::token::{can_elevate, current_user_sid};

    /// The listener table (the PIDs come with it on Windows).
    pub fn listeners_for(_pid: u32) -> std::io::Result<Vec<crate::tcptable::Listener>> {
        listeners()
    }

    /// `%ProgramData%\RootMind`.
    pub fn service_home() -> Option<std::path::PathBuf> {
        program_data().ok().map(|p| p.join("RootMind"))
    }
}

#[cfg(target_os = "linux")]
mod platform {
    use std::io;
    use std::path::PathBuf;

    pub use crate::linux::listeners_for;

    /// `~/.local/share/rootmind`.
    pub fn service_home() -> Option<PathBuf> {
        crate::linux::data_root().ok()
    }

    /// The account changes its own user units: no elevation is needed.
    pub fn can_elevate() -> bool {
        true
    }

    /// Linux units carry no SID.
    pub fn current_user_sid() -> io::Result<String> {
        Err(io::Error::new(
            io::ErrorKind::Unsupported,
            "no SID on Linux",
        ))
    }
}

#[cfg(not(any(windows, target_os = "linux")))]
mod platform {
    use std::io;
    use std::path::{Path, PathBuf};
    use std::time::Duration;

    use super::{Elevated, ScmStatus};
    use crate::tcptable::Listener;

    fn unsupported() -> io::Error {
        io::Error::new(io::ErrorKind::Unsupported, "Windows only")
    }
    pub fn run_elevated(_: &Path, _: &str, _: isize, _: Duration) -> io::Result<Elevated> {
        Err(unsupported())
    }
    pub fn program_data() -> io::Result<PathBuf> {
        Err(unsupported())
    }
    pub fn program_files() -> io::Result<PathBuf> {
        Err(unsupported())
    }
    pub fn image_path(_: &str) -> io::Result<Option<String>> {
        Ok(None)
    }
    pub fn query(_: &str) -> io::Result<Option<ScmStatus>> {
        Ok(None)
    }
    pub fn listeners() -> io::Result<Vec<Listener>> {
        Err(unsupported())
    }
    pub fn can_elevate() -> bool {
        false
    }
    pub fn current_user_sid() -> io::Result<String> {
        Err(unsupported())
    }
    pub fn listeners_for(_pid: u32) -> io::Result<Vec<Listener>> {
        listeners()
    }
    pub fn service_home() -> Option<PathBuf> {
        program_data().ok().map(|p| p.join("RootMind"))
    }
}

/// `%ProgramData%\RootMind` (Linux: `~/.local/share/rootmind`).
fn service_home() -> Option<PathBuf> {
    platform::service_home()
}

/// `%ProgramData%\RootMind\logs` (Linux: `~/.local/share/rootmind/logs`).
fn service_log_dir() -> Option<PathBuf> {
    service_home().map(|h| h.join("logs"))
}

#[cfg(not(target_os = "linux"))]
fn inspect(name: &str, role: Role) -> ServiceView {
    let raw = platform::image_path(name).ok().flatten();
    let status = platform::query(name).ok().flatten();
    let pf = platform::program_files().ok();
    let pf = pf.as_ref().and_then(|p| p.to_str());
    ServiceView::from_parts(raw.as_deref(), status, role, pf)
}

/// Linux: the user unit of `role` (the Windows service name is not used).
#[cfg(target_os = "linux")]
fn inspect(_name: &str, role: Role) -> ServiceView {
    crate::linux::inspect(role)
}

fn rest_token() -> TokenRead {
    match service_home() {
        Some(home) => read_token(&home.join("data").join("rest_token.txt")),
        None => TokenRead::Missing,
    }
}

fn collect_info() -> ServiceInfo {
    let supported = cfg!(windows) || cfg!(target_os = "linux");
    let backend = inspect(BACKEND_SERVICE, Role::Backend);
    let mcp = inspect(MCP_SERVICE, Role::McpHttp);
    let token = if backend.ours() {
        rest_token()
    } else {
        TokenRead::Missing
    };
    let own_sid = platform::current_user_sid().ok();
    build_info(
        supported,
        &backend,
        &mcp,
        own_sid.as_deref(),
        &token,
        platform::can_elevate(),
    )
}

/// The service's checks, then the token, then the proof that the server on
/// the port knows it.
fn connect() -> ServiceConnect {
    let backend = inspect(BACKEND_SERVICE, Role::Backend);
    let pid = backend.status.map_or(0, |status| status.pid);
    connect_with(
        &backend,
        || platform::listeners_for(pid),
        rest_token,
        proof::ask,
    )
}

/// The order that keeps the token safe: our service and running; then the
/// token is read, and handed out only when the server on the port proves
/// that it knows it (proof.rs). The token itself is never sent for that. A
/// service from before /proof is checked in the listener table instead: its
/// own process must listen on its port, and a table that cannot be read
/// counts as a mismatch.
pub fn connect_with(
    backend: &ServiceView,
    listeners: impl FnOnce() -> io::Result<Vec<tcptable::Listener>>,
    read_token: impl FnOnce() -> TokenRead,
    prove: impl FnOnce(u16, &str) -> Proof,
) -> ServiceConnect {
    let Some(image) = &backend.image else {
        return ServiceConnect::failed(ConnectFailure::NotInstalled);
    };
    let pid = match backend.status {
        Some(status) if SvcState::from_scm(status.state) == SvcState::Running => status.pid,
        _ => return ServiceConnect::failed(ConnectFailure::NotRunning),
    };
    let TokenRead::Readable(token) = read_token() else {
        return ServiceConnect::failed(ConnectFailure::TokenUnreadable);
    };
    let owned = match prove(image.port, &token) {
        Proof::Proved => true,
        Proof::Failed => false,
        Proof::Unknown => listeners()
            .map(|table| tcptable::port_owned_by(&table, image.port, pid))
            .unwrap_or(false),
    };
    if !owned {
        return ServiceConnect::failed(ConnectFailure::ListenerMismatch);
    }
    ServiceConnect::connected(format!("http://127.0.0.1:{}", image.port), token)
}

async fn blocking<T: Send + 'static>(f: impl FnOnce() -> T + Send + 'static) -> Result<T, String> {
    tauri::async_runtime::spawn_blocking(f)
        .await
        .map_err(|e| e.to_string())
}

/// Connect to the service just installed or changed. It answered its health
/// check before the elevated command returned, so a few tries are plenty.
async fn connect_after_change() -> Option<BackendInfo> {
    for attempt in 0..10 {
        if attempt > 0 {
            tokio::time::sleep(Duration::from_secs(1)).await;
        }
        if let Ok(ServiceConnect::Connected { url, token, .. }) = blocking(connect).await {
            return Some(BackendInfo { url, token });
        }
    }
    None
}

/// The window that owns the UAC prompt.
#[cfg(not(target_os = "linux"))]
fn main_hwnd(app: &AppHandle) -> isize {
    #[cfg(windows)]
    if let Some(window) = app.get_webview_window("main") {
        if let Ok(hwnd) = window.hwnd() {
            return hwnd.0 as isize;
        }
    }
    #[cfg(not(windows))]
    let _ = app;
    0
}

/// Run `exe` elevated with `args` and classify the result.
#[cfg(not(target_os = "linux"))]
async fn elevate(app: &AppHandle, exe: PathBuf, args: Vec<String>) -> Result<Outcome, String> {
    let params = args::join_args(&args).map_err(|e| e.to_string())?;
    let hwnd = main_hwnd(app);
    let result =
        blocking(move || platform::run_elevated(&exe, &params, hwnd, ELEVATION_WAIT)).await?;
    Ok(match result {
        Ok(elevated) => classify(elevated),
        Err(_) => Outcome::Failed(CODE_UNEXPECTED),
    })
}

/// Linux: run `exe` with `args` as this account (no elevation) and classify
/// its exit code.
#[cfg(target_os = "linux")]
async fn elevate(_app: &AppHandle, exe: PathBuf, args: Vec<String>) -> Result<Outcome, String> {
    let result = blocking(move || {
        let args: Vec<&str> = args.iter().map(String::as_str).collect();
        run_captured(&exe, &args, ELEVATION_WAIT)
    })
    .await?;
    Ok(classify_exit(
        result.ok().and_then(|output| output.status.code()),
    ))
}

/// The Program Files copy named in the backend service's ImagePath.
fn program_files_exe(view: &ServiceView) -> Result<PathBuf, String> {
    let image = view
        .image
        .as_ref()
        .ok_or("The background service is not installed.")?;
    let exe = PathBuf::from(&image.exe);
    if !exe.is_file() {
        return Err(format!("{} does not exist.", exe.display()));
    }
    Ok(exe)
}

/// The backend program installed with the app (see app.rs), for
/// `service status`.
fn installed_exe(app: &AppHandle) -> Result<PathBuf, String> {
    let exe = app::bundled_exe(app)?;
    if !exe.is_file() {
        return Err("The backend that ships with the app is missing. Reinstall RootMind.".into());
    }
    Ok(exe)
}

/// The service, if the app can connect to it after a failed change.
async fn current_backend() -> Option<BackendInfo> {
    blocking(connect)
        .await
        .ok()
        .and_then(ServiceConnect::backend)
}

/// One service change at a time.
#[derive(Default)]
pub struct ServiceOps {
    busy: tokio::sync::Mutex<()>,
}

impl ServiceOps {
    fn begin(&self) -> Result<tokio::sync::MutexGuard<'_, ()>, String> {
        self.busy
            .try_lock()
            .map_err(|_| "Another background service change is in progress.".to_string())
    }
}

// ---------------------------------------------------------------------------
// Commands

#[tauri::command]
pub async fn service_info() -> Result<ServiceInfo, String> {
    blocking(collect_info).await
}

#[tauri::command]
pub async fn service_connect() -> Result<ServiceConnect, String> {
    blocking(connect).await
}

/// Switch MCP over HTTP on or off from the Program Files exe.
#[tauri::command]
pub async fn service_set_mcp(app: AppHandle, enabled: bool) -> Result<ServiceResult, String> {
    let ops = app.state::<ServiceOps>();
    let _busy = ops.begin()?;
    let view = blocking(|| inspect(BACKEND_SERVICE, Role::Backend)).await?;
    let exe = program_files_exe(&view)?;
    let outcome = elevate(&app, exe, set_mcp_args(enabled)).await?;
    finish_change(outcome).await
}

/// Update or repair the service to the app's version: `service install` on
/// an installed service (also a stopped one) keeps its ports, MCP setting,
/// tokens and data, and installs it again when it is missing. It runs the
/// backend installed with the app (see `repair_exe`).
#[tauri::command]
pub async fn service_update(app: AppHandle) -> Result<ServiceResult, String> {
    let ops = app.state::<ServiceOps>();
    let _busy = ops.begin()?;
    let app_backend = app::bundled_exe(&app).ok();
    let views = blocking(|| {
        (
            inspect(BACKEND_SERVICE, Role::Backend),
            inspect(MCP_SERVICE, Role::McpHttp),
        )
    })
    .await?;
    let exe = repair_exe(app_backend.as_deref(), &[&views.0, &views.1], Path::is_file)?;
    let args = if cfg!(target_os = "linux") {
        update_args_linux()
    } else {
        let sid = blocking(platform::current_user_sid)
            .await?
            .map_err(|e| format!("Could not read the account SID: {e}"))?;
        update_args(&sid)?
    };
    let outcome = elevate(&app, exe, args).await?;
    finish_change(outcome).await
}

async fn finish_change(outcome: Outcome) -> Result<ServiceResult, String> {
    if outcome == Outcome::Succeeded {
        if let Some(info) = connect_after_change().await {
            return Ok(ServiceResult::done(info));
        }
    }
    let backend = current_backend().await;
    Ok(match outcome {
        Outcome::Cancelled => ServiceResult::failed(true, ERROR_CANCELLED, backend),
        Outcome::Failed(code) => ServiceResult::failed(false, code, backend),
        Outcome::Succeeded => ServiceResult::failed(false, CODE_HEALTH, backend),
    })
}

/// The MCP token for clients such as n8n (`data\api_token.txt`).
#[tauri::command]
pub async fn service_mcp_token() -> Result<String, String> {
    blocking(|| {
        let mcp = inspect(MCP_SERVICE, Role::McpHttp);
        if !mcp.ours() {
            return Err("MCP over HTTP is not enabled.".to_string());
        }
        let home = service_home().ok_or("The service folder could not be found.")?;
        match read_token(&home.join("data").join("api_token.txt")) {
            TokenRead::Readable(token) => Ok(token),
            _ => Err("The MCP token could not be read.".to_string()),
        }
    })
    .await?
}

/// Run `exe` with `args` unelevated and without a window, and collect its
/// output; the program is stopped after `timeout`.
fn run_captured(exe: &Path, args: &[&str], timeout: Duration) -> io::Result<std::process::Output> {
    let mut command = Command::new(exe);
    command
        .args(args)
        .stdin(Stdio::null())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped());
    #[cfg(windows)]
    {
        use std::os::windows::process::CommandExt;
        const CREATE_NO_WINDOW: u32 = 0x0800_0000;
        command.creation_flags(CREATE_NO_WINDOW);
    }
    let mut child = command.spawn()?;
    // Drain both pipes on their own threads, so a full pipe never blocks the child.
    let mut stdout = child.stdout.take();
    let mut stderr = child.stderr.take();
    let out = std::thread::spawn(move || {
        let mut buf = Vec::new();
        if let Some(pipe) = stdout.as_mut() {
            let _ = pipe.read_to_end(&mut buf);
        }
        buf
    });
    let err = std::thread::spawn(move || {
        let mut buf = Vec::new();
        if let Some(pipe) = stderr.as_mut() {
            let _ = pipe.read_to_end(&mut buf);
        }
        buf
    });
    let deadline = Instant::now() + timeout;
    let status = loop {
        if let Some(status) = child.try_wait()? {
            break status;
        }
        if Instant::now() >= deadline {
            let _ = child.kill();
            let _ = child.wait();
            return Err(io::Error::new(
                io::ErrorKind::TimedOut,
                "the program did not finish in time",
            ));
        }
        std::thread::sleep(Duration::from_millis(100));
    };
    Ok(std::process::Output {
        status,
        stdout: out.join().unwrap_or_default(),
        stderr: err.join().unwrap_or_default(),
    })
}

/// Diagnostics for a failed change: `service status --json` run unelevated
/// from the backend installed with the app, plus the end of `logs\install.log`.
#[tauri::command]
pub async fn service_details(app: AppHandle) -> Result<String, String> {
    let mut out = String::new();
    match installed_exe(&app) {
        Ok(exe) => {
            let output = blocking(move || {
                run_captured(&exe, &["service", "status", "--json"], DETAILS_TIMEOUT)
            })
            .await?;
            match output {
                Ok(output) => {
                    let code = output
                        .status
                        .code()
                        .map_or_else(|| "none".to_string(), |c| c.to_string());
                    out.push_str(&format!("service status --json (exit code {code}):\n"));
                    out.push_str(String::from_utf8_lossy(&output.stdout).trim_end());
                    let stderr = String::from_utf8_lossy(&output.stderr);
                    if !stderr.trim().is_empty() {
                        out.push_str("\n\nstderr:\n");
                        out.push_str(stderr.trim_end());
                    }
                }
                Err(e) if e.kind() == io::ErrorKind::TimedOut => {
                    out.push_str("service status did not answer within 30 seconds.")
                }
                Err(e) => out.push_str(&format!("service status could not run: {e}")),
            }
        }
        Err(e) => out.push_str(&e),
    }
    out.push_str(if cfg!(windows) {
        "\n\nlogs\\install.log:\n"
    } else {
        "\n\nlogs/install.log:\n"
    });
    let log = blocking(|| {
        let path = service_log_dir()?.join("install.log");
        Some(tail_lines(&path, INSTALL_LOG_LINES).map_err(|e| e.to_string()))
    })
    .await?;
    match log {
        Some(Ok(text)) if !text.is_empty() => out.push_str(&text),
        Some(Ok(_)) => out.push_str("(empty)"),
        Some(Err(e)) => out.push_str(&format!("(not readable: {e})")),
        None => out.push_str("(the service folder could not be found)"),
    }
    Ok(out)
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::fs;

    const PF: &str = r"C:\Program Files";
    const GOLDEN_BACKEND: &str = r#""C:\Program Files\RootMind Service\1.2.0-0a1b2c3d\rootmind-backend.exe" service run backend --port 8000 --user-sid S-1-5-21-1-2-3-1001"#;
    const GOLDEN_MCP: &str = r#""C:\Program Files\RootMind Service\1.2.0-0a1b2c3d\rootmind-backend.exe" service run mcp-http --port 3000"#;
    const SID: &str = "S-1-5-21-1-2-3-1001";
    const RUNNING: ScmStatus = ScmStatus {
        state: 4,
        pid: 4242,
    };

    fn view(raw: Option<&str>, status: Option<ScmStatus>, role: Role) -> ServiceView {
        ServiceView::from_parts(raw, status, role, Some(PF))
    }

    #[test]
    fn exit_codes_map_to_outcomes() {
        assert_eq!(classify(Elevated::Exited(0)), Outcome::Succeeded);
        for code in [3, 4, 5, 6, 7, 8, 9, 10] {
            assert_eq!(
                classify(Elevated::Exited(code)),
                Outcome::Failed(code),
                "{code}"
            );
        }
        assert_eq!(classify(Elevated::LaunchFailed(1223)), Outcome::Cancelled);
        for code in [225, 226, 1260, 4550, 4551, 4556, 4580, 4599] {
            assert_eq!(
                classify(Elevated::LaunchFailed(code)),
                Outcome::Failed(code),
                "{code}"
            );
        }
        // Other launch errors would collide with the service codes (2 is
        // ERROR_FILE_NOT_FOUND and also "usage"), so they become "unexpected".
        assert_eq!(
            classify(Elevated::LaunchFailed(2)),
            Outcome::Failed(CODE_UNEXPECTED)
        );
        assert_eq!(
            classify(Elevated::LaunchFailed(5)),
            Outcome::Failed(CODE_UNEXPECTED)
        );
        // A crash of the elevated program keeps its code.
        assert_eq!(
            classify(Elevated::Exited(0xC000_0005)),
            Outcome::Failed(0xC000_0005)
        );
    }

    #[test]
    fn other_change_arguments() {
        assert_eq!(
            args::join_args(&update_args(SID).unwrap()).unwrap(),
            "service install --port auto --mcp-port auto --user-sid S-1-5-21-1-2-3-1001"
        );
        assert!(update_args("not-a-sid").is_err());
        assert_eq!(
            args::join_args(&set_mcp_args(true)).unwrap(),
            "service install --mcp-http --mcp-port auto"
        );
        assert_eq!(
            args::join_args(&set_mcp_args(false)).unwrap(),
            "service install --no-mcp-http"
        );
    }

    #[test]
    fn scm_states() {
        assert_eq!(SvcState::from_scm(1), SvcState::Stopped);
        assert_eq!(SvcState::from_scm(2), SvcState::StartPending);
        assert_eq!(SvcState::from_scm(3), SvcState::StopPending);
        assert_eq!(SvcState::from_scm(4), SvcState::Running);
        assert_eq!(SvcState::from_scm(7), SvcState::Other); // paused
    }

    #[test]
    fn views_recognise_our_services() {
        let backend = view(Some(GOLDEN_BACKEND), Some(RUNNING), Role::Backend);
        assert!(backend.present && backend.ours());
        // The MCP ImagePath under the backend's name is not ours.
        assert!(!view(Some(GOLDEN_MCP), None, Role::Backend).ours());
        // Another program's service under our name is present but foreign.
        let foreign = view(
            Some(r#""C:\Program Files\RootMind Service\other-service.exe""#),
            None,
            Role::Backend,
        );
        assert!(foreign.present && !foreign.ours());
        // Without the Program Files path nothing is ours.
        assert!(!ServiceView::from_parts(Some(GOLDEN_BACKEND), None, Role::Backend, None).ours());
        assert!(!view(None, None, Role::Backend).present);
    }

    #[test]
    fn info_for_a_running_service_of_this_account() {
        let backend = view(Some(GOLDEN_BACKEND), Some(RUNNING), Role::Backend);
        let mcp = view(
            Some(GOLDEN_MCP),
            Some(ScmStatus { state: 1, pid: 0 }),
            Role::McpHttp,
        );
        let token = TokenRead::Readable("abc".into());
        let info = build_info(true, &backend, &mcp, Some(SID), &token, true);
        let value = serde_json::to_value(&info).unwrap();
        assert_eq!(
            value,
            serde_json::json!({
                "supported": true, "installed": true, "ours": true, "state": "running",
                "port": 8000, "url": "http://127.0.0.1:8000", "tokenReadable": true,
                "canElevate": true, "blockedReason": null,
                "mcp": {"installed": true, "state": "stopped", "port": 3000,
                        "url": "http://127.0.0.1:3000/mcp"}
            })
        );
    }

    #[test]
    fn a_token_of_another_account_is_never_readable() {
        // With UAC off another administrator can read the file; the service
        // still belongs to the account in its ImagePath.
        let backend = view(Some(GOLDEN_BACKEND), Some(RUNNING), Role::Backend);
        let none = ServiceView::default();
        let token = TokenRead::Readable("abc".into());
        let info = build_info(
            true,
            &backend,
            &none,
            Some("S-1-5-21-9-9-9-1002"),
            &token,
            true,
        );
        assert!(!info.token_readable);
        assert_eq!(info.blocked_reason, Some(BlockedReason::OtherAccount));
        // An unknown own SID is not a match either.
        assert!(!build_info(true, &backend, &none, None, &token, true).token_readable);
        let lower = SID.to_lowercase();
        assert!(build_info(true, &backend, &none, Some(&lower), &token, true).token_readable);
    }

    // connect_with: the token is handed out only after every check passed.

    fn listener(addr: [u8; 4], port: u16, pid: u32) -> tcptable::Listener {
        tcptable::Listener {
            addr: std::net::Ipv4Addr::from(addr),
            port,
            pid,
        }
    }

    /// Connect with `proof` as the answer of the server on the port; also
    /// whether the token file was read and the table consulted.
    fn try_connect(
        backend: &ServiceView,
        table: io::Result<Vec<tcptable::Listener>>,
        token: TokenRead,
        proof: Proof,
    ) -> (ServiceConnect, bool, bool) {
        let read = std::cell::Cell::new(false);
        let consulted = std::cell::Cell::new(false);
        let result = connect_with(
            backend,
            || {
                consulted.set(true);
                table
            },
            || {
                read.set(true);
                token
            },
            |port, _token| {
                assert_eq!(port, 8000);
                proof
            },
        );
        (result, read.get(), consulted.get())
    }

    fn failure(result: &ServiceConnect) -> Option<ConnectFailure> {
        match result {
            ServiceConnect::Failed { reason, .. } => Some(*reason),
            ServiceConnect::Connected { .. } => None,
        }
    }

    #[test]
    fn connect_hands_out_the_token_only_after_the_proof() {
        let ours = view(Some(GOLDEN_BACKEND), Some(RUNNING), Role::Backend);
        let readable = || TokenRead::Readable("s3cret".into());
        let own = || Ok(vec![listener([127, 0, 0, 1], 8000, 4242)]);

        // The proof is enough, also where the table misses the service's socket.
        let (result, _, consulted) = try_connect(&ours, Ok(vec![]), readable(), Proof::Proved);
        assert!(!consulted);
        match result {
            ServiceConnect::Connected { url, token, .. } => {
                assert_eq!(url, "http://127.0.0.1:8000");
                assert_eq!(token, "s3cret");
            }
            ServiceConnect::Failed { reason, .. } => panic!("{reason:?}"),
        }

        // A server that cannot prove the token never gets it, even where the
        // table names the service's PID.
        let (result, _, _) = try_connect(&ours, own(), readable(), Proof::Failed);
        assert_eq!(failure(&result), Some(ConnectFailure::ListenerMismatch));

        // Before the token file is read.
        let early: Vec<(&str, ServiceView, ConnectFailure)> = vec![
            (
                "not installed",
                ServiceView::default(),
                ConnectFailure::NotInstalled,
            ),
            (
                "foreign service",
                view(Some(r#""C:\x\other.exe""#), Some(RUNNING), Role::Backend),
                ConnectFailure::NotInstalled,
            ),
            (
                "start pending",
                view(
                    Some(GOLDEN_BACKEND),
                    Some(ScmStatus {
                        state: 2,
                        pid: 4242,
                    }),
                    Role::Backend,
                ),
                ConnectFailure::NotRunning,
            ),
            (
                "stop pending",
                view(
                    Some(GOLDEN_BACKEND),
                    Some(ScmStatus {
                        state: 3,
                        pid: 4242,
                    }),
                    Role::Backend,
                ),
                ConnectFailure::NotRunning,
            ),
        ];
        for (what, backend, expected) in early {
            let (result, read, _) = try_connect(&backend, own(), readable(), Proof::Proved);
            assert_eq!(failure(&result), Some(expected), "{what}");
            assert!(
                !read,
                "{what}: the token was read before the service checks passed"
            );
        }

        for token in [TokenRead::Missing, TokenRead::Denied, TokenRead::Invalid] {
            let (result, read, _) = try_connect(&ours, own(), token, Proof::Proved);
            assert!(read);
            assert_eq!(failure(&result), Some(ConnectFailure::TokenUnreadable));
        }
    }

    #[test]
    fn a_service_without_proof_is_checked_in_the_listener_table() {
        let ours = view(Some(GOLDEN_BACKEND), Some(RUNNING), Role::Backend);
        let readable = || TokenRead::Readable("s3cret".into());

        let (result, _, consulted) = try_connect(
            &ours,
            Ok(vec![listener([127, 0, 0, 1], 8000, 4242)]),
            readable(),
            Proof::Unknown,
        );
        assert!(consulted);
        assert_eq!(failure(&result), None);

        let cases: Vec<(&str, io::Result<Vec<tcptable::Listener>>)> = vec![
            (
                "another process on the port",
                Ok(vec![listener([127, 0, 0, 1], 8000, 666)]),
            ),
            (
                "a wildcard listener of another process too",
                Ok(vec![
                    listener([127, 0, 0, 1], 8000, 4242),
                    listener([0, 0, 0, 0], 8000, 666),
                ]),
            ),
            ("nobody listens", Ok(vec![])),
            (
                "the table cannot be read",
                Err(io::Error::other("no table")),
            ),
        ];
        for (what, table) in cases {
            let (result, _, _) = try_connect(&ours, table, readable(), Proof::Unknown);
            assert_eq!(
                failure(&result),
                Some(ConnectFailure::ListenerMismatch),
                "{what}"
            );
        }
    }

    #[test]
    fn info_when_nothing_is_installed() {
        let none = ServiceView::default();
        let info = build_info(true, &none, &none, Some(SID), &TokenRead::Missing, true);
        assert!(!info.installed && !info.ours && !info.token_readable);
        assert_eq!(info.state, None);
        assert_eq!(info.url, None);
        assert_eq!(info.blocked_reason, None);
        assert!(!info.mcp.installed);
    }

    #[test]
    fn blocked_reasons() {
        let none = ServiceView::default();
        let ours = view(Some(GOLDEN_BACKEND), Some(RUNNING), Role::Backend);
        let foreign = view(Some(r#""C:\x\other.exe""#), Some(RUNNING), Role::Backend);
        let readable = TokenRead::Readable("abc".into());

        assert_eq!(
            blocked_reason(false, &none, &none, Some(SID), &TokenRead::Missing, true),
            Some(BlockedReason::Unsupported)
        );
        assert_eq!(
            blocked_reason(true, &foreign, &none, Some(SID), &TokenRead::Missing, true),
            Some(BlockedReason::ForeignService)
        );
        // A foreign service under the MCP name blocks too.
        let foreign_mcp = view(Some(r#""C:\x\other.exe" --serve"#), None, Role::McpHttp);
        assert_eq!(
            blocked_reason(
                true,
                &none,
                &foreign_mcp,
                Some(SID),
                &TokenRead::Missing,
                true
            ),
            Some(BlockedReason::ForeignService)
        );
        // Installed for another account: its SID differs, or its token is not ours to read.
        assert_eq!(
            blocked_reason(
                true,
                &ours,
                &none,
                Some("S-1-5-21-9-9-9-500"),
                &readable,
                true
            ),
            Some(BlockedReason::OtherAccount)
        );
        assert_eq!(
            blocked_reason(true, &ours, &none, Some(SID), &TokenRead::Denied, true),
            Some(BlockedReason::OtherAccount)
        );
        // Standard accounts cannot turn the service on.
        assert_eq!(
            blocked_reason(true, &none, &none, Some(SID), &TokenRead::Missing, false),
            Some(BlockedReason::NotAdmin)
        );
        // Ours and readable: nothing blocks.
        assert_eq!(
            blocked_reason(true, &ours, &none, Some(SID), &readable, true),
            None
        );
        assert_eq!(
            blocked_reason(
                true,
                &ours,
                &none,
                Some(&SID.to_lowercase()),
                &readable,
                true
            ),
            None
        );
    }

    #[test]
    fn ipc_shapes_of_results() {
        let info = BackendInfo {
            url: "http://127.0.0.1:8000".into(),
            token: "t".into(),
        };
        assert_eq!(
            serde_json::to_value(ServiceResult::done(info.clone())).unwrap(),
            serde_json::json!({"ok": true, "backend": {"url": "http://127.0.0.1:8000", "token": "t"}})
        );
        assert_eq!(
            serde_json::to_value(ServiceResult::failed(true, 1223, None)).unwrap(),
            serde_json::json!({"ok": false, "cancelled": true, "code": 1223, "backend": null})
        );
        assert_eq!(
            serde_json::to_value(ServiceConnect::failed(ConnectFailure::ListenerMismatch)).unwrap(),
            serde_json::json!({"ok": false, "reason": "listener_mismatch"})
        );
        assert_eq!(
            serde_json::to_value(ServiceConnect::connected("u".into(), "t".into())).unwrap(),
            serde_json::json!({"ok": true, "url": "u", "token": "t"})
        );
        let reasons: Vec<_> = [
            ConnectFailure::NotInstalled,
            ConnectFailure::NotRunning,
            ConnectFailure::ListenerMismatch,
            ConnectFailure::TokenUnreadable,
        ]
        .iter()
        .map(|r| serde_json::to_value(r).unwrap())
        .collect();
        assert_eq!(
            reasons,
            [
                "not_installed",
                "not_running",
                "listener_mismatch",
                "token_unreadable"
            ]
        );
        let blocked: Vec<_> = [
            BlockedReason::Unsupported,
            BlockedReason::NotAdmin,
            BlockedReason::OtherAccount,
            BlockedReason::ForeignService,
        ]
        .iter()
        .map(|r| serde_json::to_value(r).unwrap())
        .collect();
        assert_eq!(
            blocked,
            [
                "unsupported",
                "not_admin",
                "other_account",
                "foreign_service"
            ]
        );
    }

    fn scratch(tag: &str) -> PathBuf {
        let dir = std::env::temp_dir().join(format!("rootmind-svc-{tag}-{}", std::process::id()));
        let _ = fs::remove_dir_all(&dir);
        fs::create_dir_all(&dir).unwrap();
        dir
    }

    #[test]
    fn token_files() {
        let dir = scratch("token");
        let file = dir.join("rest_token.txt");
        assert_eq!(read_token(&file), TokenRead::Missing);
        fs::write(&file, "abc-DEF_123\r\n").unwrap();
        assert_eq!(read_token(&file), TokenRead::Readable("abc-DEF_123".into()));
        fs::write(&file, "  \n").unwrap();
        assert_eq!(read_token(&file), TokenRead::Invalid);
        fs::write(&file, "two words").unwrap();
        assert_eq!(read_token(&file), TokenRead::Invalid);
        fs::write(&file, "x".repeat(5000)).unwrap();
        assert_eq!(read_token(&file), TokenRead::Invalid);
        fs::remove_dir_all(&dir).unwrap();
    }

    #[test]
    fn update_runs_the_app_backend_first() {
        let backend = view(Some(GOLDEN_BACKEND), Some(RUNNING), Role::Backend);
        let mcp = view(Some(GOLDEN_MCP), Some(RUNNING), Role::McpHttp);
        let none = ServiceView::default();
        let foreign = view(Some(r#""C:\x\other.exe" --x"#), None, Role::Backend);
        let copy =
            PathBuf::from(r"C:\Program Files\RootMind Service\1.2.0-0a1b2c3d\rootmind-backend.exe");
        let app = PathBuf::from(r"C:\Program Files\RootMind\backend\rootmind-backend.exe");
        // The app's backend wins, also over a (possibly older) service copy.
        assert_eq!(
            repair_exe(Some(&app), &[&backend, &mcp], |_| true).unwrap(),
            app
        );
        assert_eq!(
            repair_exe(Some(&app), &[&none, &none], |_| true).unwrap(),
            app
        );
        // Without it: the registered copy (the backend's, else the MCP service's).
        let copy_only = |exe: &Path| exe == copy.as_path();
        assert_eq!(
            repair_exe(Some(&app), &[&backend, &mcp], copy_only).unwrap(),
            copy
        );
        assert_eq!(repair_exe(None, &[&none, &mcp], |_| true).unwrap(), copy);
        // Neither: an error, also with a foreign service.
        assert!(repair_exe(Some(&app), &[&backend, &mcp], |_| false).is_err());
        assert!(repair_exe(None, &[&foreign, &none], |_| true).is_err());
        assert!(repair_exe(None, &[&none, &none], |_| true).is_err());
    }

    #[test]
    fn blocked_codes() {
        for code in [225, 226, 1260, 4550, 4580, 4599] {
            assert!(is_blocked_code(code), "{code}");
        }
        for code in [0, 2, 5, 1223, 4549, 4600] {
            assert!(!is_blocked_code(code), "{code}");
        }
    }

    #[cfg(windows)]
    #[test]
    fn captured_output_and_timeout() {
        let cmd = crate::win::paths::system_dir().unwrap().join("cmd.exe");
        let output = run_captured(
            &cmd,
            &["/d", "/c", "echo out& echo err 1>&2& exit 3"],
            DETAILS_TIMEOUT,
        )
        .unwrap();
        assert_eq!(output.status.code(), Some(3));
        assert_eq!(String::from_utf8_lossy(&output.stdout).trim(), "out");
        assert_eq!(String::from_utf8_lossy(&output.stderr).trim(), "err");
        let ping = crate::win::paths::system_dir().unwrap().join("PING.EXE");
        let slow = run_captured(
            &ping,
            &["-n", "30", "127.0.0.1"],
            Duration::from_millis(500),
        );
        assert_eq!(slow.unwrap_err().kind(), io::ErrorKind::TimedOut);
    }

    // Linux: the user units (unitfile.rs).

    const LINUX_ROOT: &str = "/home/me/.local/share/rootmind";
    const LINUX_EXE: &str =
        "/home/me/.local/share/rootmind/runtime/2.3.0-0a1b2c3d/rootmind-backend";

    fn linux_unit(role: &str, port: u16) -> String {
        format!(
            "{}
[Service]
Type=notify
ExecStart=\"{LINUX_EXE}\" service run {role} --port {port}
",
            crate::unitfile::MARKER
        )
    }

    fn linux_view(text: Option<&str>, status: Option<ScmStatus>, role: Role) -> ServiceView {
        ServiceView::from_unit(text, status, role, Some(Path::new(LINUX_ROOT)))
    }

    #[test]
    fn linux_units_are_ours_and_need_no_sid() {
        let backend = linux_view(
            Some(&linux_unit("backend", 8000)),
            Some(RUNNING),
            Role::Backend,
        );
        let mcp = linux_view(
            Some(&linux_unit("mcp-http", 3000)),
            Some(ScmStatus { state: 4, pid: 77 }),
            Role::McpHttp,
        );
        assert!(backend.present && backend.ours());
        let token = TokenRead::Readable("abc".into());
        let info = build_info(true, &backend, &mcp, None, &token, true);
        let value = serde_json::to_value(&info).unwrap();
        assert_eq!(
            value,
            serde_json::json!({
                "supported": true, "installed": true, "ours": true, "state": "running",
                "port": 8000, "url": "http://127.0.0.1:8000", "tokenReadable": true,
                "canElevate": true, "blockedReason": null,
                "mcp": {"installed": true, "state": "running", "port": 3000,
                        "url": "http://127.0.0.1:3000/mcp"}
            })
        );
        // The MCP unit under the backend's name, a foreign file and a copy
        // outside the data folder are not ours.
        assert!(!linux_view(Some(&linux_unit("mcp-http", 3000)), None, Role::Backend).ours());
        let foreign = linux_view(
            Some(
                "[Service]
ExecStart=/bin/true
",
            ),
            None,
            Role::Backend,
        );
        assert!(foreign.present && !foreign.ours());
        assert_eq!(
            blocked_reason(
                true,
                &foreign,
                &ServiceView::default(),
                None,
                &TokenRead::Missing,
                true
            ),
            Some(BlockedReason::ForeignService)
        );
        let elsewhere = ServiceView::from_unit(
            Some(&linux_unit("backend", 8000)),
            None,
            Role::Backend,
            Some(Path::new("/home/other/.local/share/rootmind")),
        );
        assert!(!elsewhere.ours());
        assert!(!linux_view(None, None, Role::Backend).present);
    }

    #[test]
    fn linux_connect_uses_the_main_pid() {
        let backend = linux_view(
            Some(&linux_unit("backend", 8000)),
            Some(RUNNING),
            Role::Backend,
        );
        let own = || Ok(vec![listener([127, 0, 0, 1], 8000, 4242)]);
        // A service from before /proof: checked in the listener table.
        let token = || TokenRead::Readable("t".into());
        let (result, _, _) = try_connect(&backend, own(), token(), Proof::Unknown);
        assert!(matches!(result, ServiceConnect::Connected { .. }));
        // A socket of another process shows PID 0 on Linux (procnet.rs).
        let other = Ok(vec![
            listener([127, 0, 0, 1], 8000, 4242),
            listener([0, 0, 0, 0], 8000, 0),
        ]);
        let (result, _, _) = try_connect(&backend, other, token(), Proof::Unknown);
        assert_eq!(failure(&result), Some(ConnectFailure::ListenerMismatch));
    }

    #[test]
    fn linux_changes() {
        assert_eq!(
            args::join_args(&update_args_linux()).unwrap(),
            "service install --port auto --mcp-port auto"
        );
        assert_eq!(classify_exit(Some(0)), Outcome::Succeeded);
        for code in [2, 3, 4, 6, 7, 8, 10, 11] {
            assert_eq!(classify_exit(Some(code)), Outcome::Failed(code as u32));
        }
        // Killed (no code), or a negative code: unexpected.
        assert_eq!(classify_exit(None), Outcome::Failed(CODE_UNEXPECTED));
        assert_eq!(classify_exit(Some(-1)), Outcome::Failed(CODE_UNEXPECTED));
    }

    #[test]
    fn install_log_tail() {
        let dir = scratch("tail");
        let file = dir.join("install.log");
        let text: String = (1..=100).map(|n| format!("line {n}\n")).collect();
        fs::write(&file, text).unwrap();
        let tail = tail_lines(&file, 3).unwrap();
        assert_eq!(tail, "line 98\nline 99\nline 100");
        assert!(tail_lines(&dir.join("missing.log"), 3).is_err());
        fs::remove_dir_all(&dir).unwrap();
    }
}
