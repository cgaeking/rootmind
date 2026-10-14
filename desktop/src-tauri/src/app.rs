// Build information for the UI, and the backend program that ships with the
// app.
//
// Since 2.0 the app has no backend of its own: the installer sets up the
// RootMind Windows service, and the app is a client of it (service.rs). The
// backend program still ships with the app, in <install folder>\backend\
// (always %ProgramFiles%\RootMind: the installer has no folder page and
// refuses another folder). The installer sets the service up from it, and the
// app runs it elevated for Update and Repair (`service install`, so the
// service gets the app's version) and unelevated for `service status --json`
// (service_details). `service install` copies it into the service's own
// admin-only folder under %ProgramFiles%\RootMind Service\, where the service
// runs from, and refuses to copy from a folder that non-administrators can
// change. Only when this backend is missing does Repair use the service's
// registered copy instead.
//
// On Linux the backend ships in the packages' resource folder
// (/usr/lib/RootMind/backend/ for the .deb and .rpm; inside the AppImage's
// temporary mount for the AppImage) and the app runs `service install` from
// there as the account itself, which copies it into
// ~/.local/share/rootmind/runtime/ for the systemd user units.

use std::path::PathBuf;

use serde::Serialize;
use tauri::{AppHandle, Manager, Runtime};

/// The backend's folder below the resource folder (tauri.release.conf.json).
const BACKEND_DIR: &str = "backend";
#[cfg(windows)]
const BACKEND_EXE: &str = "rootmind-backend.exe";
#[cfg(not(windows))]
const BACKEND_EXE: &str = "rootmind-backend";

/// `{version, dev, platform}` over IPC.
#[derive(Clone, Debug, PartialEq, Eq, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct AppMode {
    pub version: String,
    pub dev: bool,
    /// `windows`, `linux` or `other`: the UI words the service accordingly.
    pub platform: &'static str,
}

pub fn platform() -> &'static str {
    if cfg!(windows) {
        "windows"
    } else if cfg!(target_os = "linux") {
        "linux"
    } else {
        "other"
    }
}

pub fn app_mode_info() -> AppMode {
    AppMode {
        version: env!("CARGO_PKG_VERSION").to_string(),
        dev: cfg!(debug_assertions),
        platform: platform(),
    }
}

/// `<resource_dir>\backend\rootmind-backend.exe`, installed with the app
/// (release builds only; `cargo build` and `pnpm tauri dev` have none); on
/// Linux `<resource_dir>/backend/rootmind-backend`.
pub fn bundled_exe<R: Runtime>(app: &AppHandle<R>) -> Result<PathBuf, String> {
    let dir = app.path().resource_dir().map_err(|e| e.to_string())?;
    Ok(dir.join(BACKEND_DIR).join(BACKEND_EXE))
}

#[tauri::command]
pub fn app_mode() -> AppMode {
    app_mode_info()
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn app_mode_ipc_shape() {
        let value = serde_json::to_value(app_mode_info()).unwrap();
        assert_eq!(
            value,
            serde_json::json!({
                "version": env!("CARGO_PKG_VERSION"),
                "dev": cfg!(debug_assertions),
                "platform": if cfg!(windows) { "windows" } else if cfg!(target_os = "linux") { "linux" } else { "other" },
            })
        );
    }
}
