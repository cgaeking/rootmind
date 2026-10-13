// Built-in backend: the app runs the backend that ships with it
// (<resource_dir>\backend\rootmind-backend.exe) as a child process.
//
// - The child gets `api --host 127.0.0.1 --port 0 --exit-with-parent`, binds
//   a free loopback port itself and reports it on stdout with one ready line,
//   `ROOTMIND_LISTENING {"port":N}`.
// - RLM_HOME and the working directory are <app_local_data_dir>\backend, so
//   the uninstaller's "Delete app data" removes its config, index and logs.
// - A random REST token is created for every start of the backend, crash
//   restarts included, and passed only in the child's environment. It never
//   touches the disk or the app's own environment. A token sent to the old
//   port after a crash is therefore worthless to whoever took that port.
// - The variables that choose the backend's files are set on the child
//   (CONFIG_PATH, RLM_DISABLE_SCHEDULER), so a value in the user's
//   environment cannot point the built-in backend elsewhere.
// - stdout and stderr are always drained into <home>\logs\backend.log; the
//   last 200 lines stay in memory for the error dialog.
// - On app exit the child gets a `shutdown` line on stdin and 5 s to stop
//   before it is killed. If the app itself dies, the OS closes the stdin
//   pipe and the backend stops on EOF.
// - After a crash the backend is restarted with a backoff of 1, 2 and 4 s.
//   Three failures within 2 minutes give up; 5 minutes of healthy running
//   reset the count. The port and the token change, and the UI picks up the
//   new ones through `sidecar_status`.
//
// The commands are the only way the webview reaches this module; it cannot
// choose the program, its arguments or its environment.

use std::collections::VecDeque;
use std::ffi::OsString;
use std::fs::{self, File, OpenOptions};
use std::io::{self, Write};
use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Mutex, MutexGuard};
use std::time::{Duration, Instant};

use serde::{Deserialize, Serialize};
use tauri::async_runtime::Receiver;
use tauri::{AppHandle, Manager, Runtime};
use tauri_plugin_shell::process::{CommandChild, CommandEvent};
use tauri_plugin_shell::ShellExt;
use tokio::sync::watch;

/// The prefix of the ready line, including its single space.
pub const READY_PREFIX: &str = "ROOTMIND_LISTENING ";
const READY_TIMEOUT: Duration = Duration::from_secs(90);
pub const STOP_GRACE: Duration = Duration::from_secs(5);
const TAIL_LINES: usize = 200;
const LOG_ROTATE_BYTES: u64 = 5 * 1024 * 1024;
pub const LOG_FILE: &str = "backend.log";

/// Sub-folder of the app's local data folder that holds the backend's home.
pub const HOME_DIR: &str = "backend";
#[cfg(windows)]
const BACKEND_EXE: &str = "rootmind-backend.exe";
#[cfg(not(windows))]
const BACKEND_EXE: &str = "rootmind-backend";

// ---------------------------------------------------------------------------
// IPC types (camelCase over IPC)

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
pub enum SidecarState {
    Stopped,
    Starting,
    Running,
    Restarting,
    Failed,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum SidecarError {
    Missing,
    Blocked,
    AccessDenied,
    Exited,
    Timeout,
}

impl SidecarError {
    fn message(self, exit_code: Option<i32>) -> String {
        match self {
            Self::Missing => "The RootMind backend is missing from the installation. Reinstall \
                              RootMind, or check whether antivirus software removed it."
                .into(),
            Self::Blocked => "Windows security blocked the RootMind backend.".into(),
            Self::AccessDenied => "Windows denied access to the RootMind backend.".into(),
            Self::Exited => match exit_code {
                Some(code) => {
                    format!("The RootMind backend stopped unexpectedly (exit code {code}).")
                }
                None => "The RootMind backend stopped unexpectedly.".into(),
            },
            Self::Timeout => "The RootMind backend did not start within 90 seconds.".into(),
        }
    }
}

#[derive(Clone, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct SidecarStatus {
    pub state: SidecarState,
    pub url: Option<String>,
    pub token: Option<String>,
    pub exit_code: Option<i32>,
    pub error: Option<SidecarError>,
    pub log_tail: Vec<String>,
}

#[derive(Clone, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct AppMode {
    pub version: String,
    pub dev: bool,
    pub builtin_available: bool,
}

// ---------------------------------------------------------------------------
// Pure helpers (unit-tested)

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct ReadyPayload {
    port: u32,
}

/// Parse the backend's ready line, `ROOTMIND_LISTENING {"port":N}`: the exact
/// prefix, one space, then a JSON object with nothing but a port in 1–65535.
/// A trailing line break is allowed; anything else is not a ready line.
pub fn parse_ready_line(line: &str) -> Option<u16> {
    let line = line.strip_suffix('\n').unwrap_or(line);
    let line = line.strip_suffix('\r').unwrap_or(line);
    let json = line.strip_prefix(READY_PREFIX)?;
    if !json.starts_with('{') || !json.ends_with('}') {
        return None;
    }
    let payload: ReadyPayload = serde_json::from_str(json).ok()?;
    match payload.port {
        1..=65535 => u16::try_from(payload.port).ok(),
        _ => None,
    }
}

/// Whether a Win32 error code means Windows security blocked a program:
/// ERROR_VIRUS_INFECTED (225), ERROR_VIRUS_DELETED (226),
/// ERROR_ACCESS_DISABLED_BY_POLICY (1260), and the ERROR_SYSTEM_INTEGRITY_*
/// range 4550-4599 (Smart App Control and WDAC, including the reputation
/// checks). The same list is in service.rs and src/state/service.ts.
pub fn is_blocked_code(code: u32) -> bool {
    matches!(code, 225 | 226 | 1260 | 4550..=4599)
}

/// What a program blocked by Application Control prints before it exits (the
/// backend's loader, when its Python DLL is blocked).
const POLICY_BLOCK_TEXT: &str = "Application Control policy";

/// Map a failed process start to the error the UI explains.
pub fn classify_spawn_error(err: &io::Error) -> Option<SidecarError> {
    if cfg!(windows) {
        match err.raw_os_error() {
            Some(code) if u32::try_from(code).is_ok_and(is_blocked_code) => {
                return Some(SidecarError::Blocked)
            }
            Some(5) => return Some(SidecarError::AccessDenied),
            Some(2 | 3) => return Some(SidecarError::Missing),
            _ => {}
        }
    }
    match err.kind() {
        io::ErrorKind::NotFound => Some(SidecarError::Missing),
        io::ErrorKind::PermissionDenied => Some(SidecarError::AccessDenied),
        _ => None,
    }
}

/// The error of a backend that exited before it was ready: blocked when its
/// last lines say Application Control blocked a file, else exited.
fn exit_error<'a>(tail: impl DoubleEndedIterator<Item = &'a String>) -> SidecarError {
    if tail
        .rev()
        .take(20)
        .any(|line| line.contains(POLICY_BLOCK_TEXT))
    {
        SidecarError::Blocked
    } else {
        SidecarError::Exited
    }
}

/// The backend's home: `<app_local_data_dir>\backend`. A debug build may use
/// an absolute RLM_HOME from its own environment instead, for scratch folders.
pub fn home_for(app_local_data: &Path, dev_override: Option<PathBuf>) -> PathBuf {
    match dev_override {
        Some(dir) if cfg!(debug_assertions) && dir.is_absolute() => dir,
        _ => app_local_data.join(HOME_DIR),
    }
}

/// Crash-restart policy: wait 1, 2, then 4 s; give up when 3 failures fall
/// within 2 minutes; forget past failures after 5 minutes of healthy running.
#[derive(Debug, Default)]
pub struct RestartPolicy {
    failures: Vec<Instant>,
    consecutive: usize,
}

impl RestartPolicy {
    pub const BACKOFF: [Duration; 3] = [
        Duration::from_secs(1),
        Duration::from_secs(2),
        Duration::from_secs(4),
    ];
    pub const WINDOW: Duration = Duration::from_secs(120);
    pub const MAX_FAILURES: usize = 3;
    pub const HEALTHY_RESET: Duration = Duration::from_secs(300);

    /// Record a crash at `now` after the process ran healthy for
    /// `healthy_for`. Returns the delay before the next start, or `None` to
    /// give up.
    pub fn on_crash(&mut self, now: Instant, healthy_for: Duration) -> Option<Duration> {
        if healthy_for >= Self::HEALTHY_RESET {
            self.reset();
        }
        self.failures
            .retain(|t| now.saturating_duration_since(*t) < Self::WINDOW);
        self.failures.push(now);
        if self.failures.len() >= Self::MAX_FAILURES {
            return None;
        }
        let delay = Self::BACKOFF[self.consecutive.min(Self::BACKOFF.len() - 1)];
        self.consecutive += 1;
        Some(delay)
    }

    pub fn reset(&mut self) {
        self.failures.clear();
        self.consecutive = 0;
    }
}

/// Rename `path` to `<path>.1` (replacing an older one) when it is larger
/// than `limit` bytes.
pub fn rotate_if_larger(path: &Path, limit: u64) -> io::Result<()> {
    match fs::metadata(path) {
        Ok(meta) if meta.len() > limit => {
            let mut old = path.as_os_str().to_owned();
            old.push(".1");
            let old = PathBuf::from(old);
            match fs::remove_file(&old) {
                Err(e) if e.kind() != io::ErrorKind::NotFound => return Err(e),
                _ => {}
            }
            fs::rename(path, old)
        }
        Ok(_) => Ok(()),
        Err(e) if e.kind() == io::ErrorKind::NotFound => Ok(()),
        Err(e) => Err(e),
    }
}

/// 32 random bytes as 64 lowercase hex characters.
fn new_token() -> Result<String, String> {
    const HEX: &[u8; 16] = b"0123456789abcdef";
    let mut bytes = [0u8; 32];
    getrandom::fill(&mut bytes).map_err(|e| format!("Could not create a token: {e}"))?;
    Ok(bytes
        .iter()
        .flat_map(|b| [HEX[usize::from(b >> 4)], HEX[usize::from(b & 0x0f)]])
        .map(char::from)
        .collect())
}

fn decode_line(bytes: &[u8]) -> String {
    String::from_utf8_lossy(bytes)
        .trim_end_matches(['\r', '\n'])
        .to_string()
}

// ---------------------------------------------------------------------------
// Paths

/// `<resource_dir>\backend\rootmind-backend.exe`, the per-user copy installed
/// with the app.
pub fn bundled_exe<R: Runtime>(app: &AppHandle<R>) -> Result<PathBuf, String> {
    let dir = app.path().resource_dir().map_err(|e| e.to_string())?;
    Ok(dir.join(HOME_DIR).join(BACKEND_EXE))
}

/// The built-in backend's RLM_HOME (see `home_for`).
pub fn sidecar_home<R: Runtime>(app: &AppHandle<R>) -> Result<PathBuf, String> {
    let local = app.path().app_local_data_dir().map_err(|e| e.to_string())?;
    let dev = std::env::var_os("RLM_HOME").map(PathBuf::from);
    Ok(home_for(&local, dev))
}

/// What to start: the program and its home (RLM_HOME and working directory).
#[derive(Clone, Debug)]
pub struct Launch {
    pub exe: PathBuf,
    pub home: PathBuf,
}

fn resolve_launch<R: Runtime>(app: &AppHandle<R>) -> Result<Launch, String> {
    Ok(Launch {
        exe: bundled_exe(app)?,
        home: sidecar_home(app)?,
    })
}

// ---------------------------------------------------------------------------
// Lifecycle

#[derive(Clone, Copy, PartialEq, Eq)]
enum Attempt {
    /// Started by the UI (or the service toggle). An exit before the ready
    /// line is reported, not retried.
    Initial,
    /// Started by the crash-restart policy.
    AutoRestart,
}

struct Inner {
    state: SidecarState,
    port: Option<u16>,
    token: Option<String>,
    exit_code: Option<i32>,
    error: Option<SidecarError>,
    tail: VecDeque<String>,
    child: Option<CommandChild>,
    /// Increases with every spawn; events of older processes are ignored.
    generation: u64,
    /// The current generation's process has not terminated yet.
    alive: bool,
    attempt: Attempt,
    ready_at: Option<Instant>,
    policy: RestartPolicy,
    /// What the last start ran; automatic restarts run the same.
    launch: Option<Launch>,
    /// The app asked the current process to stop: its exit is not a crash.
    expected_exit: bool,
    /// The app is exiting: never start again.
    exiting: bool,
    log_rotated: bool,
}

/// Managed state for the built-in backend.
pub struct Sidecar {
    inner: Mutex<Inner>,
    /// Bumped on every state change; waiters re-check the state.
    changed: watch::Sender<u64>,
    /// Serializes start, stop and restart.
    ops: tokio::sync::Mutex<()>,
    /// The UI is connected to the background service, not to the child.
    service_mode: AtomicBool,
}

impl Default for Sidecar {
    fn default() -> Self {
        Self {
            inner: Mutex::new(Inner {
                state: SidecarState::Stopped,
                port: None,
                token: None,
                exit_code: None,
                error: None,
                tail: VecDeque::with_capacity(TAIL_LINES),
                child: None,
                generation: 0,
                alive: false,
                attempt: Attempt::Initial,
                ready_at: None,
                policy: RestartPolicy::default(),
                launch: None,
                expected_exit: false,
                exiting: false,
                log_rotated: false,
            }),
            changed: watch::channel(0).0,
            ops: tokio::sync::Mutex::new(()),
            service_mode: AtomicBool::new(false),
        }
    }
}

impl Sidecar {
    fn lock(&self) -> MutexGuard<'_, Inner> {
        // A panic while holding the lock leaves plain data behind; keep going.
        self.inner.lock().unwrap_or_else(|e| e.into_inner())
    }

    fn notify(&self) {
        self.changed.send_modify(|n| *n = n.wrapping_add(1));
    }

    pub fn set_service_mode(&self, on: bool) {
        self.service_mode.store(on, Ordering::SeqCst);
    }

    pub fn service_mode(&self) -> bool {
        self.service_mode.load(Ordering::SeqCst)
    }

    pub fn status(&self) -> SidecarStatus {
        let i = self.lock();
        let running = i.state == SidecarState::Running;
        SidecarStatus {
            state: i.state,
            url: running.then(|| i.port.map(url_for)).flatten(),
            token: if running { i.token.clone() } else { None },
            exit_code: i.exit_code,
            error: i.error,
            log_tail: i.tail.iter().cloned().collect(),
        }
    }

    /// Start the backend unless it is already running or starting, and wait
    /// until it is ready.
    pub async fn ensure_started<R: Runtime>(
        &self,
        app: &AppHandle<R>,
    ) -> Result<BackendInfo, String> {
        let launch = resolve_launch(app)?;
        self.ensure_started_with(app, launch).await
    }

    pub async fn ensure_started_with<R: Runtime>(
        &self,
        app: &AppHandle<R>,
        launch: Launch,
    ) -> Result<BackendInfo, String> {
        self.set_service_mode(false);
        {
            let _op = self.ops.lock().await;
            let idle = matches!(
                self.lock().state,
                SidecarState::Stopped | SidecarState::Failed
            );
            if idle {
                self.lock().policy.reset();
                self.spawn(app, launch, None, Attempt::Initial)?;
            }
        }
        self.wait_ready().await
    }

    /// Stop the backend if it runs, start it again (optionally importing a
    /// config file first) and wait until it is ready.
    pub async fn restart<R: Runtime>(
        &self,
        app: &AppHandle<R>,
        import_config: Option<PathBuf>,
    ) -> Result<BackendInfo, String> {
        let launch = resolve_launch(app)?;
        self.restart_with(app, launch, import_config).await
    }

    pub async fn restart_with<R: Runtime>(
        &self,
        app: &AppHandle<R>,
        launch: Launch,
        import_config: Option<PathBuf>,
    ) -> Result<BackendInfo, String> {
        self.set_service_mode(false);
        {
            let _op = self.ops.lock().await;
            self.stop_locked().await;
            self.lock().policy.reset();
            self.spawn(app, launch, import_config, Attempt::Initial)?;
        }
        self.wait_ready().await
    }

    /// Stop the backend gracefully (the background service takes over).
    pub async fn stop(&self) {
        let _op = self.ops.lock().await;
        self.stop_locked().await;
    }

    /// Ask the running process to stop, wait up to 5 s, then kill it.
    async fn stop_locked(&self) {
        let generation = {
            let mut i = self.lock();
            if i.child.is_none() {
                // Nothing runs; a pending restart must not start one either.
                if i.state != SidecarState::Failed {
                    i.state = SidecarState::Stopped;
                }
                i.port = None;
                drop(i);
                self.notify();
                return;
            }
            i.expected_exit = true;
            if let Some(child) = i.child.as_mut() {
                let _ = child.write(b"shutdown\n");
            }
            i.generation
        };
        if !self.wait_terminated(generation, STOP_GRACE).await {
            let child = self.lock().child.take();
            if let Some(child) = child {
                let _ = tauri::async_runtime::spawn_blocking(move || child.kill()).await;
            }
        }
        {
            let mut i = self.lock();
            if i.generation == generation {
                i.state = SidecarState::Stopped;
                i.port = None;
                i.ready_at = None;
                i.child = None;
            }
        }
        self.notify();
    }

    /// Called on app exit: never restart, ask the process to stop, and kill
    /// it after 5 s. Blocks the calling (main) thread.
    pub fn shutdown(&self) {
        let generation = {
            let mut i = self.lock();
            i.exiting = true;
            i.expected_exit = true;
            match i.child.as_mut() {
                Some(child) => {
                    let _ = child.write(b"shutdown\n");
                }
                None => return,
            }
            i.generation
        };
        let ended = tauri::async_runtime::block_on(self.wait_terminated(generation, STOP_GRACE));
        if !ended {
            let child = self.lock().child.take();
            if let Some(child) = child {
                let _ = child.kill();
            }
        }
    }

    /// Wait until the process of `generation` has terminated (true) or the
    /// timeout passed (false).
    async fn wait_terminated(&self, generation: u64, timeout: Duration) -> bool {
        let mut rx = self.changed.subscribe();
        let deadline = tokio::time::Instant::now() + timeout;
        loop {
            {
                let i = self.lock();
                if i.generation != generation || !i.alive {
                    return true;
                }
            }
            match tokio::time::timeout_at(deadline, rx.changed()).await {
                Ok(Ok(())) => continue,
                _ => return false,
            }
        }
    }

    /// Wait until the backend is running (its URL and token) or has failed.
    async fn wait_ready(&self) -> Result<BackendInfo, String> {
        let mut rx = self.changed.subscribe();
        loop {
            {
                let i = self.lock();
                match i.state {
                    SidecarState::Running => {
                        if let (Some(port), Some(token)) = (i.port, i.token.clone()) {
                            return Ok(BackendInfo {
                                url: url_for(port),
                                token,
                            });
                        }
                    }
                    SidecarState::Failed => {
                        let error = i.error.unwrap_or(SidecarError::Exited);
                        return Err(error.message(i.exit_code));
                    }
                    SidecarState::Stopped => {
                        return Err("The built-in backend was stopped.".into());
                    }
                    SidecarState::Starting | SidecarState::Restarting => {}
                }
            }
            if rx.changed().await.is_err() {
                return Err("The built-in backend is shutting down.".into());
            }
        }
    }

    /// Start a new process. Must be called with `ops` held. Every failure is
    /// recorded (state Failed), so nobody waits for a start that never comes.
    fn spawn<R: Runtime>(
        &self,
        app: &AppHandle<R>,
        launch: Launch,
        import_config: Option<PathBuf>,
        attempt: Attempt,
    ) -> Result<(), String> {
        let Launch { exe, home } = launch.clone();
        if !exe.is_file() {
            return Err(self.fail(SidecarError::Missing, None));
        }
        fs::create_dir_all(home.join("logs")).map_err(|e| {
            self.fail_other(format!(
                "Could not create the backend folder {}: {e}",
                home.display()
            ))
        })?;
        // A new token for every process: one sent to an old port is worthless.
        let token = new_token().map_err(|e| self.fail_other(e))?;
        let rotate = {
            let mut i = self.lock();
            if i.exiting {
                return Err("The app is closing.".into());
            }
            i.token = Some(token.clone());
            i.launch = Some(launch);
            let rotate = !i.log_rotated;
            i.log_rotated = true;
            rotate
        };
        let log = open_log(&home, rotate);

        let mut args: Vec<OsString> = [
            "api",
            "--host",
            "127.0.0.1",
            "--port",
            "0",
            "--exit-with-parent",
        ]
        .iter()
        .map(OsString::from)
        .collect();
        if let Some(file) = &import_config {
            args.push("--import-config".into());
            args.push(file.as_os_str().to_owned());
        }
        let mut command = app
            .shell()
            .command(&exe)
            .args(args)
            .current_dir(&home)
            .env("RLM_HOME", &home)
            .env("RLM_API_TOKEN", &token)
            // The backend's own config.yaml in RLM_HOME, and indexing on,
            // whatever the user's environment says (the plugin can set
            // variables, not remove them).
            .env("CONFIG_PATH", "config.yaml")
            .env("RLM_DISABLE_SCHEDULER", "")
            .set_process_group(true);
        if cfg!(debug_assertions) {
            command = command.env("RLM_DEV", "1");
        }

        let (rx, child) = match command.spawn() {
            Ok(spawned) => spawned,
            Err(e) => {
                let detail = e.to_string();
                let kind = match &e {
                    tauri_plugin_shell::Error::Io(io) => classify_spawn_error(io),
                    _ => None,
                };
                return Err(match kind {
                    Some(kind) => self.fail(kind, Some(detail)),
                    None => {
                        self.fail_other(format!("Could not start the RootMind backend: {detail}"))
                    }
                });
            }
        };
        let pid = child.pid();
        let generation = self.started(Some(child), attempt);
        let mut log = log;
        write_log(&mut log, &format!("--- backend process {pid} started ---"));
        monitor(app.clone(), generation, rx, log, import_config);
        Ok(())
    }

    /// Record a new process (state Starting) and return its generation.
    fn started(&self, child: Option<CommandChild>, attempt: Attempt) -> u64 {
        let generation = {
            let mut i = self.lock();
            i.generation += 1;
            i.child = child;
            i.alive = true;
            i.state = SidecarState::Starting;
            i.port = None;
            i.exit_code = None;
            i.error = None;
            i.attempt = attempt;
            i.ready_at = None;
            i.expected_exit = false;
            i.generation
        };
        self.notify();
        generation
    }

    /// Record a failure before a process exists; returns the message.
    fn fail(&self, error: SidecarError, detail: Option<String>) -> String {
        let message = error.message(None);
        {
            let mut i = self.lock();
            i.state = SidecarState::Failed;
            i.error = Some(error);
            i.port = None;
            push_tail(&mut i.tail, format!("[app] {message}"));
            if let Some(detail) = detail {
                push_tail(&mut i.tail, format!("[app] {detail}"));
            }
        }
        self.notify();
        message
    }

    /// Record a failure that has no SidecarError of its own.
    fn fail_other(&self, message: String) -> String {
        {
            let mut i = self.lock();
            i.state = SidecarState::Failed;
            i.error = None;
            i.port = None;
            push_tail(&mut i.tail, format!("[app] {message}"));
        }
        self.notify();
        message
    }

    fn record(&self, log: &mut Option<File>, line: String) {
        if line.is_empty() {
            return;
        }
        write_log(log, &line);
        push_tail(&mut self.lock().tail, line);
    }

    fn became_ready(&self, generation: u64, port: u16) -> bool {
        {
            let mut i = self.lock();
            if i.generation != generation
                || !i.alive
                || i.expected_exit
                || i.state != SidecarState::Starting
            {
                return false;
            }
            i.state = SidecarState::Running;
            i.port = Some(port);
            i.ready_at = Some(Instant::now());
            i.error = None;
        }
        self.notify();
        true
    }

    fn ready_timed_out(&self, generation: u64) {
        let child = {
            let mut i = self.lock();
            if i.generation != generation || i.state != SidecarState::Starting {
                return;
            }
            i.state = SidecarState::Failed;
            i.error = Some(SidecarError::Timeout);
            i.expected_exit = true;
            push_tail(
                &mut i.tail,
                format!("[app] {}", SidecarError::Timeout.message(None)),
            );
            i.child.take()
        };
        self.notify();
        if let Some(child) = child {
            tauri::async_runtime::spawn_blocking(move || {
                let _ = child.kill();
            });
        }
    }

    /// Record the exit of the process of `generation`. Returns the delay
    /// before an automatic restart, if the policy wants one.
    fn terminated(&self, generation: u64, code: Option<i32>) -> Option<Duration> {
        let mut restart = None;
        {
            let mut i = self.lock();
            if i.generation != generation {
                return None;
            }
            i.alive = false;
            i.child = None;
            i.exit_code = code;
            i.port = None;
            let healthy_for = i.ready_at.take().map(|t| t.elapsed()).unwrap_or_default();
            if i.exiting || i.expected_exit {
                if i.state != SidecarState::Failed {
                    i.state = SidecarState::Stopped;
                }
            } else if i.state == SidecarState::Starting && i.attempt == Attempt::Initial {
                i.state = SidecarState::Failed;
                i.error = Some(exit_error(i.tail.iter()));
            } else {
                restart = i.policy.on_crash(Instant::now(), healthy_for);
                if restart.is_some() {
                    i.state = SidecarState::Restarting;
                } else {
                    i.state = SidecarState::Failed;
                    i.error = Some(exit_error(i.tail.iter()));
                }
            }
            push_tail(&mut i.tail, exit_line(code));
        }
        self.notify();
        restart
    }
}

fn exit_line(code: Option<i32>) -> String {
    match code {
        Some(code) => format!("--- backend process exited with code {code} ---"),
        None => "--- backend process exited ---".to_string(),
    }
}

fn url_for(port: u16) -> String {
    format!("http://127.0.0.1:{port}")
}

fn push_tail(tail: &mut VecDeque<String>, line: String) {
    if tail.len() == TAIL_LINES {
        tail.pop_front();
    }
    tail.push_back(line);
}

fn open_log(home: &Path, rotate: bool) -> Option<File> {
    let path = home.join("logs").join(LOG_FILE);
    if rotate {
        let _ = rotate_if_larger(&path, LOG_ROTATE_BYTES);
    }
    OpenOptions::new().create(true).append(true).open(path).ok()
}

fn write_log(log: &mut Option<File>, line: &str) {
    if let Some(file) = log {
        if writeln!(file, "{line}").is_err() {
            *log = None;
        }
    }
}

/// Drain the child's events: log every line, watch for the ready line within
/// 90 s, and report the exit.
fn monitor<R: Runtime>(
    app: AppHandle<R>,
    generation: u64,
    mut rx: Receiver<CommandEvent>,
    mut log: Option<File>,
    mut import_config: Option<PathBuf>,
) {
    tauri::async_runtime::spawn(async move {
        let sidecar = app.state::<Sidecar>();
        let deadline = tokio::time::Instant::now() + READY_TIMEOUT;
        let mut waiting = true;
        loop {
            let event = if waiting {
                match tokio::time::timeout_at(deadline, rx.recv()).await {
                    Ok(event) => event,
                    Err(_) => {
                        waiting = false;
                        sidecar.ready_timed_out(generation);
                        continue;
                    }
                }
            } else {
                rx.recv().await
            };
            let Some(event) = event else { break };
            match event {
                CommandEvent::Stdout(bytes) => {
                    let line = decode_line(&bytes);
                    if waiting {
                        if let Some(port) = parse_ready_line(&line) {
                            waiting = false;
                            if sidecar.became_ready(generation, port) {
                                // The backend has imported the file by now.
                                if let Some(file) = import_config.take() {
                                    let _ = fs::remove_file(file);
                                }
                            }
                        }
                    }
                    sidecar.record(&mut log, line);
                }
                CommandEvent::Stderr(bytes) => sidecar.record(&mut log, decode_line(&bytes)),
                CommandEvent::Error(message) => {
                    sidecar.record(&mut log, format!("[app] {message}"))
                }
                CommandEvent::Terminated(payload) => {
                    waiting = false;
                    write_log(&mut log, &exit_line(payload.code));
                    if let Some(delay) = sidecar.terminated(generation, payload.code) {
                        schedule_restart(app.clone(), generation, delay);
                    }
                }
                _ => {}
            }
        }
    });
}

fn schedule_restart<R: Runtime>(app: AppHandle<R>, generation: u64, delay: Duration) {
    tauri::async_runtime::spawn(async move {
        tokio::time::sleep(delay).await;
        let sidecar = app.state::<Sidecar>();
        let _op = sidecar.ops.lock().await;
        let launch = {
            let i = sidecar.lock();
            let due = i.state == SidecarState::Restarting && i.generation == generation;
            if due && !i.exiting {
                i.launch.clone()
            } else {
                None
            }
        };
        if let Some(launch) = launch {
            let _ = sidecar.spawn(&app, launch, None, Attempt::AutoRestart);
        }
    });
}

// ---------------------------------------------------------------------------
// Commands

#[tauri::command]
pub fn app_mode(app: AppHandle) -> AppMode {
    AppMode {
        version: env!("CARGO_PKG_VERSION").to_string(),
        dev: cfg!(debug_assertions),
        builtin_available: bundled_exe(&app).map(|p| p.is_file()).unwrap_or(false),
    }
}

#[tauri::command]
pub async fn sidecar_start(app: AppHandle) -> Result<BackendInfo, String> {
    app.state::<Sidecar>().ensure_started(&app).await
}

#[tauri::command]
pub fn sidecar_status(app: AppHandle) -> SidecarStatus {
    app.state::<Sidecar>().status()
}

#[tauri::command]
pub async fn sidecar_restart(app: AppHandle) -> Result<BackendInfo, String> {
    app.state::<Sidecar>().restart(&app, None).await
}

/// The folder with the logs of the backend the UI uses: `<home>\logs` for the
/// built-in backend, `%ProgramData%\RootMind\logs` for the background service.
#[tauri::command]
pub fn backend_log_dir(app: AppHandle) -> Result<String, String> {
    if app.state::<Sidecar>().service_mode() {
        if let Some(dir) = crate::service::service_log_dir() {
            return Ok(dir.to_string_lossy().into_owned());
        }
    }
    let dir = sidecar_home(&app)?.join("logs");
    fs::create_dir_all(&dir).map_err(|e| e.to_string())?;
    Ok(dir.to_string_lossy().into_owned())
}

/// Stop the built-in backend on app exit (RunEvent::Exit).
pub fn shutdown(app: &AppHandle) {
    app.state::<Sidecar>().shutdown();
}

// ---------------------------------------------------------------------------

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn ready_line_valid() {
        assert_eq!(
            parse_ready_line(r#"ROOTMIND_LISTENING {"port":8000}"#),
            Some(8000)
        );
        assert_eq!(
            parse_ready_line(r#"ROOTMIND_LISTENING {"port":1}"#),
            Some(1)
        );
        assert_eq!(
            parse_ready_line(r#"ROOTMIND_LISTENING {"port":65535}"#),
            Some(65535)
        );
        // A line break from the pipe is fine.
        assert_eq!(
            parse_ready_line("ROOTMIND_LISTENING {\"port\":54321}\r\n"),
            Some(54321)
        );
        assert_eq!(
            parse_ready_line("ROOTMIND_LISTENING {\"port\":54321}\n"),
            Some(54321)
        );
        // JSON whitespace inside the object is still JSON.
        assert_eq!(
            parse_ready_line(r#"ROOTMIND_LISTENING { "port": 18000 }"#),
            Some(18000)
        );
    }

    #[test]
    fn ready_line_port_out_of_range() {
        assert_eq!(parse_ready_line(r#"ROOTMIND_LISTENING {"port":0}"#), None);
        assert_eq!(
            parse_ready_line(r#"ROOTMIND_LISTENING {"port":70000}"#),
            None
        );
        assert_eq!(
            parse_ready_line(r#"ROOTMIND_LISTENING {"port":65536}"#),
            None
        );
        assert_eq!(parse_ready_line(r#"ROOTMIND_LISTENING {"port":-1}"#), None);
        assert_eq!(
            parse_ready_line(r#"ROOTMIND_LISTENING {"port":80.5}"#),
            None
        );
        assert_eq!(
            parse_ready_line(r#"ROOTMIND_LISTENING {"port":"8000"}"#),
            None
        );
    }

    #[test]
    fn ready_line_invalid_json() {
        for line in [
            "ROOTMIND_LISTENING ",
            "ROOTMIND_LISTENING {",
            "ROOTMIND_LISTENING {port:8000}",
            r#"ROOTMIND_LISTENING {"port":8000"#,
            r#"ROOTMIND_LISTENING [8000]"#,
            r#"ROOTMIND_LISTENING {}"#,
            r#"ROOTMIND_LISTENING {"port":8000,"port":8001}"#,
            r#"ROOTMIND_LISTENING {"port":8000,"host":"0.0.0.0"}"#,
            r#"ROOTMIND_LISTENING {"port":8000} trailing"#,
        ] {
            assert_eq!(parse_ready_line(line), None, "accepted {line}");
        }
    }

    #[test]
    fn ready_line_wrong_prefix() {
        for line in [
            r#"ROOTMIND_LISTENING{"port":8000}"#,
            r#"ROOTMIND_LISTENING  {"port":8000}"#,
            r#" ROOTMIND_LISTENING {"port":8000}"#,
            r#"rootmind_listening {"port":8000}"#,
            r#"INFO: ROOTMIND_LISTENING {"port":8000}"#,
            r#"ROOTMIND_READY {"port":8000}"#,
            "INFO:     Uvicorn running on http://127.0.0.1:8000",
            "",
        ] {
            assert_eq!(parse_ready_line(line), None, "accepted {line}");
        }
    }

    #[test]
    fn spawn_errors_map_to_ui_errors() {
        let os = io::Error::from_raw_os_error;
        if cfg!(windows) {
            // 4556-4559 and 4580-4582: Smart App Control's reputation checks.
            for code in [225, 226, 1260, 4550, 4551, 4556, 4559, 4580, 4582, 4599] {
                assert_eq!(
                    classify_spawn_error(&os(code)),
                    Some(SidecarError::Blocked),
                    "{code}"
                );
            }
            assert_eq!(classify_spawn_error(&os(4549)), None);
            assert_eq!(classify_spawn_error(&os(4600)), None);
            assert_eq!(
                classify_spawn_error(&os(5)),
                Some(SidecarError::AccessDenied)
            );
            assert_eq!(classify_spawn_error(&os(2)), Some(SidecarError::Missing));
            assert_eq!(classify_spawn_error(&os(3)), Some(SidecarError::Missing));
            // ERROR_NOT_ENOUGH_MEMORY has no dedicated message.
            assert_eq!(classify_spawn_error(&os(8)), None);
        }
        let not_found = io::Error::new(io::ErrorKind::NotFound, "gone");
        assert_eq!(
            classify_spawn_error(&not_found),
            Some(SidecarError::Missing)
        );
        let denied = io::Error::new(io::ErrorKind::PermissionDenied, "no");
        assert_eq!(
            classify_spawn_error(&denied),
            Some(SidecarError::AccessDenied)
        );
        let other = io::Error::other("something else");
        assert_eq!(classify_spawn_error(&other), None);
    }

    #[test]
    fn error_codes_serialize_like_the_ipc_contract() {
        let names: Vec<String> = [
            SidecarError::Missing,
            SidecarError::Blocked,
            SidecarError::AccessDenied,
            SidecarError::Exited,
            SidecarError::Timeout,
        ]
        .iter()
        .map(|e| {
            serde_json::to_value(e)
                .unwrap()
                .as_str()
                .unwrap()
                .to_string()
        })
        .collect();
        assert_eq!(
            names,
            ["missing", "blocked", "access_denied", "exited", "timeout"]
        );
        let status = SidecarStatus {
            state: SidecarState::Restarting,
            url: None,
            token: None,
            exit_code: Some(3),
            error: None,
            log_tail: vec!["line".into()],
        };
        let value = serde_json::to_value(status).unwrap();
        assert_eq!(
            value,
            serde_json::json!({"state": "restarting", "url": null, "token": null,
                               "exitCode": 3, "error": null, "logTail": ["line"]})
        );
        let mode = serde_json::to_value(AppMode {
            version: "1.2.0".into(),
            dev: false,
            builtin_available: true,
        })
        .unwrap();
        assert_eq!(
            mode,
            serde_json::json!({"version": "1.2.0", "dev": false, "builtinAvailable": true})
        );
    }

    #[test]
    fn backoff_one_two_then_give_up_within_two_minutes() {
        let mut policy = RestartPolicy::default();
        let t0 = Instant::now();
        assert_eq!(
            policy.on_crash(t0, Duration::ZERO),
            Some(Duration::from_secs(1))
        );
        assert_eq!(
            policy.on_crash(t0 + Duration::from_secs(10), Duration::from_secs(5)),
            Some(Duration::from_secs(2))
        );
        assert_eq!(
            policy.on_crash(t0 + Duration::from_secs(20), Duration::from_secs(5)),
            None
        );
    }

    #[test]
    fn backoff_reaches_four_seconds_when_failures_are_spread_out() {
        let mut policy = RestartPolicy::default();
        let t0 = Instant::now();
        let minute = Duration::from_secs(60);
        let healthy = Duration::from_secs(90); // less than the 5-minute reset
        assert_eq!(policy.on_crash(t0, healthy), Some(Duration::from_secs(1)));
        assert_eq!(
            policy.on_crash(t0 + 2 * minute, healthy),
            Some(Duration::from_secs(2))
        );
        assert_eq!(
            policy.on_crash(t0 + 4 * minute, healthy),
            Some(Duration::from_secs(4))
        );
        assert_eq!(
            policy.on_crash(t0 + 6 * minute, healthy),
            Some(Duration::from_secs(4))
        );
        // Two more soon after: the third failure within 2 minutes gives up.
        assert_eq!(
            policy.on_crash(t0 + 6 * minute + Duration::from_secs(30), healthy),
            Some(Duration::from_secs(4))
        );
        assert_eq!(
            policy.on_crash(t0 + 6 * minute + Duration::from_secs(45), healthy),
            None
        );
    }

    #[test]
    fn five_healthy_minutes_reset_the_backoff() {
        let mut policy = RestartPolicy::default();
        let t0 = Instant::now();
        assert_eq!(
            policy.on_crash(t0, Duration::ZERO),
            Some(Duration::from_secs(1))
        );
        assert_eq!(
            policy.on_crash(t0 + Duration::from_secs(5), Duration::ZERO),
            Some(Duration::from_secs(2))
        );
        // Ran five minutes before this crash: starts over at 1 s.
        assert_eq!(
            policy.on_crash(t0 + Duration::from_secs(400), Duration::from_secs(300)),
            Some(Duration::from_secs(1))
        );
        policy.reset();
        assert_eq!(
            policy.on_crash(t0 + Duration::from_secs(401), Duration::ZERO),
            Some(Duration::from_secs(1))
        );
    }

    #[test]
    fn sidecar_home_is_under_app_local_data_dir() {
        // The uninstaller's "Delete app data" removes $LOCALAPPDATA\<identifier>
        // (Tauri's app_local_data_dir), so the backend's config, index and logs
        // must live below it.
        let conf: serde_json::Value = serde_json::from_str(
            &fs::read_to_string(Path::new(env!("CARGO_MANIFEST_DIR")).join("tauri.conf.json"))
                .unwrap(),
        )
        .unwrap();
        let identifier = conf["identifier"].as_str().unwrap();
        let local_app_data = std::env::temp_dir().join("LocalAppData");
        let app_local_data = local_app_data.join(identifier);
        let home = home_for(&app_local_data, None);
        assert!(home.starts_with(&app_local_data));
        assert_eq!(home, app_local_data.join("backend"));
        // A relative override is ignored, so it can never point elsewhere by accident.
        assert_eq!(home_for(&app_local_data, Some(PathBuf::from("rel"))), home);
    }

    #[test]
    fn tokens_are_random_hex() {
        let a = new_token().unwrap();
        let b = new_token().unwrap();
        assert_eq!(a.len(), 64);
        assert!(a.bytes().all(|c| c.is_ascii_hexdigit()));
        assert_ne!(a, b);
    }

    #[test]
    fn log_rotation_keeps_one_old_file() {
        let dir = std::env::temp_dir().join(format!("rootmind-log-test-{}", std::process::id()));
        let _ = fs::remove_dir_all(&dir);
        fs::create_dir_all(&dir).unwrap();
        let log = dir.join(LOG_FILE);
        let old = dir.join(format!("{LOG_FILE}.1"));

        // Missing file: nothing to do.
        rotate_if_larger(&log, 10).unwrap();
        // Small file stays.
        fs::write(&log, b"short").unwrap();
        rotate_if_larger(&log, 10).unwrap();
        assert!(log.exists() && !old.exists());
        // Large file moves to .1, replacing an older .1.
        fs::write(&old, b"older").unwrap();
        fs::write(&log, b"more than ten bytes").unwrap();
        rotate_if_larger(&log, 10).unwrap();
        assert!(!log.exists());
        assert_eq!(fs::read(&old).unwrap(), b"more than ten bytes");

        fs::remove_dir_all(&dir).unwrap();
    }

    #[test]
    fn tail_keeps_the_last_lines() {
        let mut tail = VecDeque::new();
        for n in 0..TAIL_LINES + 5 {
            push_tail(&mut tail, n.to_string());
        }
        assert_eq!(tail.len(), TAIL_LINES);
        assert_eq!(tail.front().unwrap(), "5");
        assert_eq!(tail.back().unwrap(), &(TAIL_LINES + 4).to_string());
    }

    #[test]
    fn decode_strips_line_breaks() {
        assert_eq!(decode_line(b"hello\r\n"), "hello");
        assert_eq!(decode_line(b"hello\r"), "hello");
        assert_eq!(decode_line(b"\n"), "");
        assert_eq!(decode_line(b"caf\xc3\xa9\n"), "café");
    }

    // State machine, driven the way the event monitor drives it (no process).

    fn sidecar_with_token() -> Sidecar {
        let sidecar = Sidecar::default();
        sidecar.lock().token = Some("t0k3n".into());
        sidecar
    }

    #[test]
    fn ready_line_makes_it_running() {
        let sidecar = sidecar_with_token();
        let generation = sidecar.started(None, Attempt::Initial);
        assert_eq!(sidecar.status().state, SidecarState::Starting);
        assert_eq!(sidecar.status().url, None);
        assert!(sidecar.became_ready(generation, 54321));
        let status = sidecar.status();
        assert_eq!(status.state, SidecarState::Running);
        assert_eq!(status.url.as_deref(), Some("http://127.0.0.1:54321"));
        assert_eq!(status.token.as_deref(), Some("t0k3n"));
        let info = tauri::async_runtime::block_on(sidecar.wait_ready()).unwrap();
        assert_eq!(info.url, "http://127.0.0.1:54321");
        // A second ready line changes nothing.
        assert!(!sidecar.became_ready(generation, 1));
    }

    #[test]
    fn exit_before_ready_fails_without_restart() {
        let sidecar = sidecar_with_token();
        let generation = sidecar.started(None, Attempt::Initial);
        assert_eq!(sidecar.terminated(generation, Some(3)), None);
        let status = sidecar.status();
        assert_eq!(status.state, SidecarState::Failed);
        assert_eq!(status.error, Some(SidecarError::Exited));
        assert_eq!(status.exit_code, Some(3));
        assert_eq!(status.token, None);
        let err = tauri::async_runtime::block_on(sidecar.wait_ready()).unwrap_err();
        assert!(err.contains("exit code 3"), "{err}");
    }

    #[test]
    fn crash_after_ready_restarts_with_backoff_then_gives_up() {
        let sidecar = sidecar_with_token();
        let mut generation = sidecar.started(None, Attempt::Initial);
        sidecar.became_ready(generation, 50001);
        let delays: Vec<_> = (0..3)
            .map(|n| {
                let delay = sidecar.terminated(generation, Some(1));
                if delay.is_some() {
                    assert_eq!(sidecar.status().state, SidecarState::Restarting);
                    generation = sidecar.started(None, Attempt::AutoRestart);
                    sidecar.became_ready(generation, 50002 + n);
                }
                delay
            })
            .collect();
        assert_eq!(
            delays,
            [
                Some(Duration::from_secs(1)),
                Some(Duration::from_secs(2)),
                None
            ]
        );
        assert_eq!(sidecar.status().state, SidecarState::Failed);
        assert_eq!(sidecar.status().error, Some(SidecarError::Exited));
    }

    #[test]
    fn a_failed_automatic_restart_counts_as_another_crash() {
        let sidecar = sidecar_with_token();
        let generation = sidecar.started(None, Attempt::Initial);
        sidecar.became_ready(generation, 50001);
        assert!(sidecar.terminated(generation, Some(1)).is_some());
        let generation = sidecar.started(None, Attempt::AutoRestart);
        // Exits before its ready line: retried, not reported.
        assert_eq!(
            sidecar.terminated(generation, Some(1)),
            Some(Duration::from_secs(2))
        );
        assert_eq!(sidecar.status().state, SidecarState::Restarting);
    }

    #[test]
    fn expected_exits_stop_without_restart() {
        let sidecar = sidecar_with_token();
        let generation = sidecar.started(None, Attempt::Initial);
        sidecar.became_ready(generation, 50001);
        sidecar.lock().expected_exit = true;
        assert_eq!(sidecar.terminated(generation, Some(0)), None);
        assert_eq!(sidecar.status().state, SidecarState::Stopped);

        let generation = sidecar.started(None, Attempt::Initial);
        sidecar.became_ready(generation, 50002);
        sidecar.lock().exiting = true;
        assert_eq!(sidecar.terminated(generation, None), None);
        assert_eq!(sidecar.status().state, SidecarState::Stopped);
    }

    #[test]
    fn ready_timeout_fails_and_the_kill_keeps_the_error() {
        let sidecar = sidecar_with_token();
        let generation = sidecar.started(None, Attempt::Initial);
        sidecar.ready_timed_out(generation);
        assert_eq!(sidecar.status().state, SidecarState::Failed);
        assert_eq!(sidecar.status().error, Some(SidecarError::Timeout));
        // A late ready line is ignored, and so is the exit after the kill.
        assert!(!sidecar.became_ready(generation, 50001));
        assert_eq!(sidecar.terminated(generation, Some(1)), None);
        assert_eq!(sidecar.status().state, SidecarState::Failed);
        assert_eq!(sidecar.status().error, Some(SidecarError::Timeout));
    }

    #[test]
    fn events_of_an_older_process_are_ignored() {
        let sidecar = sidecar_with_token();
        let old = sidecar.started(None, Attempt::Initial);
        let new = sidecar.started(None, Attempt::Initial);
        assert!(!sidecar.became_ready(old, 50001));
        assert_eq!(sidecar.terminated(old, Some(1)), None);
        sidecar.ready_timed_out(old);
        assert_eq!(sidecar.status().state, SidecarState::Starting);
        assert!(sidecar.became_ready(new, 50002));
    }

    #[test]
    fn stop_without_a_process_cancels_a_pending_restart() {
        let sidecar = sidecar_with_token();
        let generation = sidecar.started(None, Attempt::Initial);
        sidecar.became_ready(generation, 50001);
        assert!(sidecar.terminated(generation, Some(1)).is_some());
        tauri::async_runtime::block_on(sidecar.stop());
        assert_eq!(sidecar.status().state, SidecarState::Stopped);
        let err = tauri::async_runtime::block_on(sidecar.wait_ready()).unwrap_err();
        assert!(err.contains("stopped"), "{err}");
    }

    #[test]
    fn an_exit_after_an_application_control_message_is_blocked() {
        let sidecar = sidecar_with_token();
        let generation = sidecar.started(None, Attempt::Initial);
        sidecar.record(
            &mut None,
            "[PYI-1234:ERROR] Failed to load Python DLL: LoadLibrary: An Application Control \
             policy has blocked this file."
                .into(),
        );
        assert_eq!(sidecar.terminated(generation, Some(1)), None);
        assert_eq!(sidecar.status().error, Some(SidecarError::Blocked));

        // Without that message an early exit stays "exited".
        let generation = sidecar.started(None, Attempt::Initial);
        for n in 0..25 {
            sidecar.record(&mut None, format!("line {n}"));
        }
        sidecar.terminated(generation, Some(1));
        assert_eq!(sidecar.status().error, Some(SidecarError::Exited));
    }

    #[test]
    fn exit_lines_reach_the_tail() {
        let sidecar = sidecar_with_token();
        let generation = sidecar.started(None, Attempt::Initial);
        sidecar.terminated(generation, Some(9));
        let tail = sidecar.status().log_tail;
        assert_eq!(
            tail.last().map(String::as_str),
            Some("--- backend process exited with code 9 ---")
        );
    }
}
