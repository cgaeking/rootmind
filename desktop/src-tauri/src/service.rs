// Background service: RootMind-Backend (and optionally RootMind-MCP-HTTP)
// installed by the backend's own `service install` command.
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
// - turning the service on and updating it run the per-user backend that
//   ships with the app;
// - turning it off and switching MCP over HTTP run the copy under
//   %ProgramFiles%\RootMind Service\, which must be the one in the ImagePath.
// The exit codes are the backend's service codes (0 ok, 3 foreign service,
// 4 port busy, 5 not elevated, 6 data folder refused, 7 health check failed,
// 8 account mismatch, 9 no access context); 1223 means the prompt was
// cancelled.

use std::fs::{self, File};
use std::io::{self, Read, Seek, SeekFrom};
use std::path::{Path, PathBuf};
use std::time::Duration;

use serde::Serialize;
use tauri::{AppHandle, Manager};
use tauri_plugin_shell::ShellExt;

use crate::args;
use crate::backend::{self, BackendInfo, Sidecar};
use crate::imagepath::{self, ImagePath, Role};
use crate::tcptable;

pub const BACKEND_SERVICE: &str = "RootMind-Backend";
pub const MCP_SERVICE: &str = "RootMind-MCP-HTTP";
const ELEVATION_WAIT: Duration = Duration::from_secs(10 * 60);
const DETAILS_TIMEOUT: Duration = Duration::from_secs(30);
const TOKEN_MAX_BYTES: u64 = 4096;
const INSTALL_LOG_LINES: usize = 60;

// ---------------------------------------------------------------------------
// IPC types (camelCase over IPC)

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
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Elevated {
    /// It ran and exited with this code.
    Exited(u32),
    /// ShellExecuteExW failed with this Win32 error.
    LaunchFailed(u32),
}

/// What the UI is told about an elevated change.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Outcome {
    Succeeded,
    Cancelled,
    /// The `code` field of ServiceResult.
    Failed(u32),
}

pub const ERROR_CANCELLED: u32 = 1223;
/// "Unexpected failure", also used for launch errors without a meaning of
/// their own (they would collide with the service exit codes).
pub const CODE_UNEXPECTED: u32 = 1;
/// The service was installed, but the app could not connect to it.
pub const CODE_HEALTH: u32 = 7;

pub fn classify(result: Elevated) -> Outcome {
    match result {
        Elevated::Exited(0) => Outcome::Succeeded,
        Elevated::Exited(code) => Outcome::Failed(code),
        Elevated::LaunchFailed(ERROR_CANCELLED) => Outcome::Cancelled,
        // Windows security blocked the program: passed to the UI unchanged so
        // it can say so.
        Elevated::LaunchFailed(code) if backend::is_blocked_code(code) => Outcome::Failed(code),
        Elevated::LaunchFailed(_) => Outcome::Failed(CODE_UNEXPECTED),
    }
}

/// `service install` for turning the service on.
pub fn install_args(
    mcp: bool,
    user_sid: &str,
    import_config: Option<&Path>,
) -> Result<Vec<String>, String> {
    if !imagepath::is_sid(user_sid) {
        return Err("The account SID is malformed.".into());
    }
    let mut out: Vec<String> = ["service", "install", "--port", "auto"]
        .map(String::from)
        .to_vec();
    if mcp {
        out.extend(["--mcp-http", "--mcp-port", "auto"].map(String::from));
    } else {
        out.push("--no-mcp-http".into());
    }
    out.extend(["--user-sid".to_string(), user_sid.to_string()]);
    if let Some(file) = import_config {
        out.push("--import-config".into());
        out.push(path_arg(file)?);
    }
    Ok(out)
}

/// `service install` on the existing service: update and repair. Ports, MCP
/// and the account are read back from the service (sticky options).
pub fn update_args(user_sid: &str) -> Result<Vec<String>, String> {
    if !imagepath::is_sid(user_sid) {
        return Err("The account SID is malformed.".into());
    }
    Ok([
        "service",
        "install",
        "--port",
        "auto",
        "--user-sid",
        user_sid,
    ]
    .map(String::from)
    .to_vec())
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

pub fn uninstall_args(purge: bool) -> Vec<String> {
    let mut out = vec!["service".to_string(), "uninstall".to_string()];
    if purge {
        out.push("--purge".into());
    }
    out
}

fn path_arg(path: &Path) -> Result<String, String> {
    path.to_str()
        .map(str::to_string)
        .ok_or_else(|| format!("The path {} cannot be passed on.", path.display()))
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

    /// Build a view from the raw ImagePath and SCM status.
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
        // rights, so the app must not pick it.
        token_readable: matches!(token, TokenRead::Readable(_))
            && backend
                .image
                .as_ref()
                .is_some_and(|image| same_sid(image.user_sid.as_deref(), own_sid)),
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
}

#[cfg(not(windows))]
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
}

/// `%ProgramData%\RootMind`.
fn service_home() -> Option<PathBuf> {
    platform::program_data().ok().map(|p| p.join("RootMind"))
}

/// `%ProgramData%\RootMind\logs`, for "Show log" in service mode.
pub fn service_log_dir() -> Option<PathBuf> {
    service_home().map(|h| h.join("logs"))
}

fn inspect(name: &str, role: Role) -> ServiceView {
    let raw = platform::image_path(name).ok().flatten();
    let status = platform::query(name).ok().flatten();
    let pf = platform::program_files().ok();
    let pf = pf.as_ref().and_then(|p| p.to_str());
    ServiceView::from_parts(raw.as_deref(), status, role, pf)
}

fn rest_token() -> TokenRead {
    match service_home() {
        Some(home) => read_token(&home.join("data").join("rest_token.txt")),
        None => TokenRead::Missing,
    }
}

fn collect_info() -> ServiceInfo {
    let supported = cfg!(windows);
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

/// The listener check, then the token.
fn connect() -> ServiceConnect {
    connect_with(
        &inspect(BACKEND_SERVICE, Role::Backend),
        platform::listeners,
        rest_token,
    )
}

/// The order that keeps the token safe: our service, running, and its own
/// process listening on its port; only then is the token read. A listener
/// table that cannot be read counts as a mismatch.
pub fn connect_with(
    backend: &ServiceView,
    listeners: impl FnOnce() -> io::Result<Vec<tcptable::Listener>>,
    read_token: impl FnOnce() -> TokenRead,
) -> ServiceConnect {
    let Some(image) = &backend.image else {
        return ServiceConnect::failed(ConnectFailure::NotInstalled);
    };
    let pid = match backend.status {
        Some(status) if SvcState::from_scm(status.state) == SvcState::Running => status.pid,
        _ => return ServiceConnect::failed(ConnectFailure::NotRunning),
    };
    let owned = listeners()
        .map(|table| tcptable::port_owned_by(&table, image.port, pid))
        .unwrap_or(false);
    if !owned {
        return ServiceConnect::failed(ConnectFailure::ListenerMismatch);
    }
    match read_token() {
        TokenRead::Readable(token) => {
            ServiceConnect::connected(format!("http://127.0.0.1:{}", image.port), token)
        }
        _ => ServiceConnect::failed(ConnectFailure::TokenUnreadable),
    }
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

fn per_user_exe(app: &AppHandle) -> Result<PathBuf, String> {
    let exe = backend::bundled_exe(app)?;
    if !exe.is_file() {
        return Err("The backend that ships with the app is missing.".into());
    }
    Ok(exe)
}

/// The backend the UI should use after a failed change: the service if it
/// answers, else the built-in backend if it runs.
async fn current_backend(app: &AppHandle) -> Option<BackendInfo> {
    if let Ok(connected) = blocking(connect).await {
        if let Some(info) = connected.backend() {
            return Some(info);
        }
    }
    let status = app.state::<Sidecar>().status();
    match (status.url, status.token) {
        (Some(url), Some(token)) => Some(BackendInfo { url, token }),
        _ => None,
    }
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
pub async fn service_connect(app: AppHandle) -> Result<ServiceConnect, String> {
    let result = blocking(connect).await?;
    if matches!(result, ServiceConnect::Connected { .. }) {
        app.state::<Sidecar>().set_service_mode(true);
    }
    Ok(result)
}

/// Turn the service on: stop the built-in backend, install the service from
/// the per-user exe (importing the built-in config), then connect to it. On
/// failure or cancel the built-in backend starts again.
#[tauri::command]
pub async fn service_enable(app: AppHandle, mcp: bool) -> Result<ServiceResult, String> {
    let ops = app.state::<ServiceOps>();
    let _busy = ops.begin()?;
    let exe = per_user_exe(&app)?;
    let sid = blocking(platform::current_user_sid)
        .await?
        .map_err(|e| format!("Could not read the account SID: {e}"))?;
    let config = backend::sidecar_home(&app)?.join("config.yaml");
    let import = config.is_file().then_some(config);
    let args = install_args(mcp, &sid, import.as_deref())?;

    let sidecar = app.state::<Sidecar>();
    sidecar.stop().await;
    let outcome = elevate(&app, exe, args).await?;
    if outcome == Outcome::Succeeded {
        if let Some(info) = connect_after_change().await {
            sidecar.set_service_mode(true);
            return Ok(ServiceResult::done(info));
        }
    }
    let (cancelled, code) = match outcome {
        Outcome::Cancelled => (true, ERROR_CANCELLED),
        Outcome::Failed(code) => (false, code),
        Outcome::Succeeded => (false, CODE_HEALTH),
    };
    let backend = sidecar.ensure_started(&app).await.ok();
    Ok(ServiceResult::failed(cancelled, code, backend))
}

/// Turn the service off: keep a copy of its config, uninstall it from the
/// Program Files exe, then start the built-in backend with that config. On
/// failure or cancel the service stays in use.
#[tauri::command]
pub async fn service_disable(app: AppHandle, purge: bool) -> Result<ServiceResult, String> {
    let ops = app.state::<ServiceOps>();
    let _busy = ops.begin()?;
    let view = blocking(|| inspect(BACKEND_SERVICE, Role::Backend)).await?;
    let exe = program_files_exe(&view)?;
    let home = backend::sidecar_home(&app)?;
    fs::create_dir_all(&home).map_err(|e| e.to_string())?;
    let copy = home.join("config.from-service.yaml");
    let copied = match service_home() {
        Some(service_home) => fs::copy(service_home.join("config.yaml"), &copy).is_ok(),
        None => false,
    };

    let failure = match elevate(&app, exe, uninstall_args(purge)).await? {
        Outcome::Succeeded => None,
        Outcome::Cancelled => Some((true, ERROR_CANCELLED)),
        Outcome::Failed(code) => Some((false, code)),
    };
    if let Some((cancelled, code)) = failure {
        if copied {
            let _ = fs::remove_file(&copy);
        }
        let backend = current_backend(&app).await;
        return Ok(ServiceResult::failed(cancelled, code, backend));
    }
    // The backend deletes nothing it imported; the copy is removed once the
    // ready line arrived (backend.rs), or here if the start failed.
    let started = app
        .state::<Sidecar>()
        .restart(&app, copied.then(|| copy.clone()))
        .await;
    match started {
        Ok(info) => Ok(ServiceResult::done(info)),
        Err(e) => {
            if copied {
                let _ = fs::remove_file(&copy);
            }
            Err(format!(
                "The background service was removed, but the built-in backend did not start: {e}"
            ))
        }
    }
}

/// Switch MCP over HTTP on or off from the Program Files exe.
#[tauri::command]
pub async fn service_set_mcp(app: AppHandle, enabled: bool) -> Result<ServiceResult, String> {
    let ops = app.state::<ServiceOps>();
    let _busy = ops.begin()?;
    let view = blocking(|| inspect(BACKEND_SERVICE, Role::Backend)).await?;
    let exe = program_files_exe(&view)?;
    let outcome = elevate(&app, exe, set_mcp_args(enabled)).await?;
    finish_change(&app, outcome).await
}

/// Update or repair the service from the per-user exe of this app version.
#[tauri::command]
pub async fn service_update(app: AppHandle) -> Result<ServiceResult, String> {
    let ops = app.state::<ServiceOps>();
    let _busy = ops.begin()?;
    let exe = per_user_exe(&app)?;
    let sid = blocking(platform::current_user_sid)
        .await?
        .map_err(|e| format!("Could not read the account SID: {e}"))?;
    let outcome = elevate(&app, exe, update_args(&sid)?).await?;
    finish_change(&app, outcome).await
}

async fn finish_change(app: &AppHandle, outcome: Outcome) -> Result<ServiceResult, String> {
    if outcome == Outcome::Succeeded {
        if let Some(info) = connect_after_change().await {
            // The service is the backend again; a built-in one started for the
            // meantime must not index the same folders.
            let sidecar = app.state::<Sidecar>();
            sidecar.stop().await;
            sidecar.set_service_mode(true);
            return Ok(ServiceResult::done(info));
        }
    }
    let backend = current_backend(app).await;
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

/// Diagnostics for a failed change: `service status --json` run unelevated
/// from the per-user exe, plus the end of `logs\install.log`.
#[tauri::command]
pub async fn service_details(app: AppHandle) -> Result<String, String> {
    let mut out = String::new();
    match per_user_exe(&app) {
        Ok(exe) => {
            let command = app
                .shell()
                .command(&exe)
                .args(["service", "status", "--json"]);
            match tokio::time::timeout(DETAILS_TIMEOUT, command.output()).await {
                Ok(Ok(output)) => {
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
                Ok(Err(e)) => out.push_str(&format!("service status could not run: {e}")),
                Err(_) => out.push_str("service status did not answer within 30 seconds."),
            }
        }
        Err(e) => out.push_str(&e),
    }
    out.push_str("\n\nlogs\\install.log:\n");
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
        for code in [3, 4, 5, 6, 7, 8, 9] {
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
    fn install_arguments() {
        let args = install_args(false, SID, None).unwrap();
        assert_eq!(
            args::join_args(&args).unwrap(),
            "service install --port auto --no-mcp-http --user-sid S-1-5-21-1-2-3-1001"
        );
        let config = Path::new(
            r"C:\Users\Jane Doe\AppData\Local\io.github.cgaeking.rootmind\backend\config.yaml",
        );
        let args = install_args(true, SID, Some(config)).unwrap();
        assert_eq!(
            args::join_args(&args).unwrap(),
            r#"service install --port auto --mcp-http --mcp-port auto --user-sid S-1-5-21-1-2-3-1001 --import-config "C:\Users\Jane Doe\AppData\Local\io.github.cgaeking.rootmind\backend\config.yaml""#
        );
        assert!(install_args(false, "S-1-5-21 --purge", None).is_err());
    }

    #[test]
    fn other_change_arguments() {
        assert_eq!(
            args::join_args(&update_args(SID).unwrap()).unwrap(),
            "service install --port auto --user-sid S-1-5-21-1-2-3-1001"
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
        assert_eq!(
            args::join_args(&uninstall_args(false)).unwrap(),
            "service uninstall"
        );
        assert_eq!(
            args::join_args(&uninstall_args(true)).unwrap(),
            "service uninstall --purge"
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
        // The legacy NSSM service is present but foreign.
        let nssm = view(
            Some(r#""C:\Program Files\RootMind Service\nssm.exe""#),
            None,
            Role::Backend,
        );
        assert!(nssm.present && !nssm.ours());
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

    // connect_with: the token is read only after every check passed.

    fn listener(addr: [u8; 4], port: u16, pid: u32) -> tcptable::Listener {
        tcptable::Listener {
            addr: std::net::Ipv4Addr::from(addr),
            port,
            pid,
        }
    }

    fn try_connect(
        backend: &ServiceView,
        table: io::Result<Vec<tcptable::Listener>>,
        token: TokenRead,
    ) -> (ServiceConnect, bool) {
        let read = std::cell::Cell::new(false);
        let result = connect_with(
            backend,
            || table,
            || {
                read.set(true);
                token
            },
        );
        (result, read.get())
    }

    fn failure(result: &ServiceConnect) -> Option<ConnectFailure> {
        match result {
            ServiceConnect::Failed { reason, .. } => Some(*reason),
            ServiceConnect::Connected { .. } => None,
        }
    }

    #[test]
    fn connect_reads_the_token_only_after_the_checks() {
        let ours = view(Some(GOLDEN_BACKEND), Some(RUNNING), Role::Backend);
        let readable = || TokenRead::Readable("s3cret".into());
        let own = || Ok(vec![listener([127, 0, 0, 1], 8000, 4242)]);

        let (result, read) = try_connect(&ours, own(), readable());
        assert!(read);
        match result {
            ServiceConnect::Connected { url, token, .. } => {
                assert_eq!(url, "http://127.0.0.1:8000");
                assert_eq!(token, "s3cret");
            }
            ServiceConnect::Failed { reason, .. } => panic!("{reason:?}"),
        }

        let cases: Vec<(
            &str,
            ServiceView,
            io::Result<Vec<tcptable::Listener>>,
            ConnectFailure,
        )> = vec![
            (
                "not installed",
                ServiceView::default(),
                own(),
                ConnectFailure::NotInstalled,
            ),
            (
                "foreign service",
                view(Some(r#""C:\nssm\nssm.exe""#), Some(RUNNING), Role::Backend),
                own(),
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
                own(),
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
                own(),
                ConnectFailure::NotRunning,
            ),
            (
                "another process on the port",
                ours.clone(),
                Ok(vec![listener([127, 0, 0, 1], 8000, 666)]),
                ConnectFailure::ListenerMismatch,
            ),
            (
                "a wildcard listener of another process too",
                ours.clone(),
                Ok(vec![
                    listener([127, 0, 0, 1], 8000, 4242),
                    listener([0, 0, 0, 0], 8000, 666),
                ]),
                ConnectFailure::ListenerMismatch,
            ),
            (
                "nobody listens",
                ours.clone(),
                Ok(vec![]),
                ConnectFailure::ListenerMismatch,
            ),
            (
                "the table cannot be read",
                ours.clone(),
                Err(io::Error::other("no table")),
                ConnectFailure::ListenerMismatch,
            ),
        ];
        for (what, backend, table, expected) in cases {
            let (result, read) = try_connect(&backend, table, readable());
            assert_eq!(failure(&result), Some(expected), "{what}");
            assert!(!read, "{what}: the token was read before the checks passed");
        }

        for token in [TokenRead::Missing, TokenRead::Denied, TokenRead::Invalid] {
            let (result, read) = try_connect(&ours, own(), token);
            assert!(read);
            assert_eq!(failure(&result), Some(ConnectFailure::TokenUnreadable));
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
        let foreign = view(Some(r#""C:\nssm\nssm.exe""#), Some(RUNNING), Role::Backend);
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
