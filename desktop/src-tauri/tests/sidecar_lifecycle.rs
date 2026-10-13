//! The built-in backend's lifecycle with real child processes: the
//! `fake_backend` example (tests/support/fake_backend.rs) started through the
//! shell plugin in a mock app. The fake binds 127.0.0.1:0; every test uses its
//! own folder under the system temp directory.

use std::fs;
use std::path::PathBuf;
use std::time::{Duration, Instant};

use rootmind_desktop_lib::testing::{
    BackendInfo, Launch, Sidecar, SidecarError, SidecarState, SidecarStatus, LOG_FILE,
    READY_PREFIX, STOP_GRACE,
};
use tauri::async_runtime::block_on;
use tauri::Manager;

fn fake_backend_exe() -> PathBuf {
    let deps = std::env::current_exe().unwrap();
    let target = deps.parent().unwrap().parent().unwrap();
    let exe = target
        .join("examples")
        .join(format!("fake_backend{}", std::env::consts::EXE_SUFFIX));
    assert!(
        exe.is_file(),
        "{} is missing; `cargo test` builds it with the examples",
        exe.display()
    );
    exe
}

struct FakeApp {
    app: tauri::App<tauri::test::MockRuntime>,
    home: PathBuf,
}

impl FakeApp {
    fn new(tag: &str, mode: Option<&str>) -> Self {
        let home =
            std::env::temp_dir().join(format!("rootmind-sidecar-{tag}-{}", std::process::id()));
        let _ = fs::remove_dir_all(&home);
        fs::create_dir_all(&home).unwrap();
        if let Some(mode) = mode {
            fs::write(home.join("fake-mode"), mode).unwrap();
        }
        let app = tauri::test::mock_builder()
            .plugin(tauri_plugin_shell::init())
            .manage(Sidecar::default())
            .build(tauri::test::mock_context(tauri::test::noop_assets()))
            .unwrap();
        Self { app, home }
    }

    fn launch(&self) -> Launch {
        Launch {
            exe: fake_backend_exe(),
            home: self.home.clone(),
        }
    }

    fn sidecar(&self) -> tauri::State<'_, Sidecar> {
        self.app.state::<Sidecar>()
    }

    fn start(&self) -> Result<BackendInfo, String> {
        block_on(
            self.sidecar()
                .ensure_started_with(self.app.handle(), self.launch()),
        )
    }

    fn launch_record(&self) -> String {
        fs::read_to_string(self.home.join("fake-launch.txt")).unwrap()
    }

    /// Poll `check` for up to `secs` seconds.
    fn wait_for(&self, secs: u64, check: impl Fn(&SidecarStatus) -> bool) -> SidecarStatus {
        let deadline = Instant::now() + Duration::from_secs(secs);
        loop {
            let status = self.sidecar().status();
            if check(&status) || Instant::now() > deadline {
                return status;
            }
            std::thread::sleep(Duration::from_millis(50));
        }
    }
}

impl Drop for FakeApp {
    fn drop(&mut self) {
        self.sidecar().shutdown();
        let _ = fs::remove_dir_all(&self.home);
    }
}

#[test]
fn starts_the_backend_and_stops_it_on_request() {
    let fake = FakeApp::new("start", None);
    let info = fake.start().unwrap();
    let addr = info.url.strip_prefix("http://").unwrap();
    std::net::TcpStream::connect(addr).expect("the backend's port answers");

    let record = fake.launch_record();
    assert!(
        record.contains("args=api --host 127.0.0.1 --port 0 --exit-with-parent\n"),
        "{record}"
    );
    assert!(
        record.contains(&format!("cwd={}\n", fake.home.display())),
        "{record}"
    );
    assert!(
        record.contains(&format!("home={}\n", fake.home.display())),
        "{record}"
    );
    assert!(
        record.contains(&format!("token={}\n", info.token)),
        "{record}"
    );
    assert_eq!(info.token.len(), 64);
    assert!(
        record.contains("dev=1\n"),
        "debug builds set RLM_DEV: {record}"
    );
    // The variables that choose the backend's files are pinned.
    assert!(record.contains("config_path=config.yaml\n"), "{record}");
    assert!(record.contains("disable_scheduler=\n"), "{record}");
    // The token is only in the child's environment.
    assert!(std::env::var_os("RLM_API_TOKEN").is_none());

    // Starting again returns the running instance.
    assert_eq!(fake.start().unwrap().url, info.url);
    let log = fs::read_to_string(fake.home.join("logs").join(LOG_FILE)).unwrap();
    assert!(log.contains("INFO: fake backend starting"), "{log}");

    let began = Instant::now();
    block_on(fake.sidecar().stop());
    assert!(
        began.elapsed() < STOP_GRACE,
        "the shutdown line was not honoured"
    );
    assert_eq!(fake.sidecar().status().state, SidecarState::Stopped);
}

#[test]
fn an_early_exit_reports_the_code_and_the_output() {
    let fake = FakeApp::new("early", Some("exit-early"));
    let err = fake.start().expect_err("the start fails");
    assert!(err.contains("exit code 3"), "{err}");
    let status = fake.sidecar().status();
    assert_eq!(status.state, SidecarState::Failed);
    assert_eq!(status.error, Some(SidecarError::Exited));
    assert_eq!(status.exit_code, Some(3));
    assert!(
        status
            .log_tail
            .iter()
            .any(|l| l.contains("refusing to start")),
        "{:?}",
        status.log_tail
    );
}

#[test]
fn a_missing_program_is_reported_as_missing() {
    let fake = FakeApp::new("missing", None);
    let launch = Launch {
        exe: fake.home.join("no-such-backend.exe"),
        home: fake.home.clone(),
    };
    let result = block_on(
        fake.sidecar()
            .ensure_started_with(fake.app.handle(), launch),
    );
    let err = result.expect_err("the start fails");
    assert!(err.contains("missing"), "{err}");
    assert_eq!(fake.sidecar().status().error, Some(SidecarError::Missing));
}

/// Every failed start is recorded, so nothing waits for a start that never
/// comes (an automatic restart takes the same path).
#[test]
fn a_start_that_cannot_create_its_log_folder_is_recorded() {
    let fake = FakeApp::new("nologs", None);
    fs::write(fake.home.join("logs"), b"a file where the folder should be").unwrap();
    let err = fake.start().expect_err("the start fails");
    assert!(err.contains("Could not create the backend folder"), "{err}");
    let status = fake.sidecar().status();
    assert_eq!(status.state, SidecarState::Failed);
    assert!(
        status
            .log_tail
            .iter()
            .any(|l| l.contains("Could not create")),
        "{:?}",
        status.log_tail
    );
    // A second start fails the same way instead of hanging.
    assert!(fake.start().is_err());
}

#[test]
fn a_backend_that_ignores_shutdown_is_killed() {
    let fake = FakeApp::new("kill", Some("ignore-shutdown"));
    let info = fake.start().unwrap();
    let began = Instant::now();
    block_on(fake.sidecar().stop());
    assert!(began.elapsed() >= STOP_GRACE);
    assert_eq!(fake.sidecar().status().state, SidecarState::Stopped);
    let addr = info.url.strip_prefix("http://").unwrap();
    assert!(
        std::net::TcpStream::connect(addr).is_err(),
        "still listening"
    );
}

#[test]
fn a_crash_restarts_with_a_new_token() {
    let fake = FakeApp::new("crash", Some("crash-once"));
    let first = fake.start();
    let status = fake.wait_for(15, |s| {
        s.state == SidecarState::Running
            && s.log_tail
                .iter()
                .filter(|l| l.starts_with(READY_PREFIX))
                .count()
                == 2
    });
    assert_eq!(status.state, SidecarState::Running, "{:?}", status.log_tail);
    assert!(fake.home.join("fake-crashed").exists());
    let token = status.token.clone().unwrap();
    assert!(fake.launch_record().contains(&format!("token={token}\n")));
    // Each process got its own token; the UI takes the new one over.
    let launches = fs::read_to_string(fake.home.join("fake-launches.txt")).unwrap();
    let tokens: Vec<&str> = launches
        .lines()
        .filter_map(|l| l.strip_prefix("token="))
        .collect();
    assert_eq!(tokens.len(), 2, "{launches}");
    assert_ne!(tokens[0], tokens[1], "a crash restart reused the token");
    assert_eq!(tokens[1], token);
    if let Ok(first) = first {
        assert!(tokens.contains(&first.token.as_str()));
    }
}

#[test]
fn an_imported_config_is_passed_and_removed_once_ready() {
    let fake = FakeApp::new("import", None);
    let file = fake.home.join("config.from-service.yaml");
    fs::write(&file, "connectors: []\n").unwrap();
    block_on(
        fake.sidecar()
            .restart_with(fake.app.handle(), fake.launch(), Some(file.clone())),
    )
    .unwrap();
    let record = fake.launch_record();
    let expected = format!("--exit-with-parent --import-config {}\n", file.display());
    assert!(record.contains(&expected), "{record}");
    let deadline = Instant::now() + Duration::from_secs(5);
    while file.exists() && Instant::now() < deadline {
        std::thread::sleep(Duration::from_millis(50));
    }
    assert!(!file.exists(), "the imported file was kept");
}
