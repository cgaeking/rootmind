// Every unsafe block states why it is sound.
#![warn(clippy::undocumented_unsafe_blocks)]

mod args;
mod backend;
mod conversations;
mod imagepath;
mod service;
mod tcptable;
#[cfg(windows)]
mod win;

/// Hooks for the integration tests in tests/; not an API.
#[doc(hidden)]
pub mod testing {
    pub use crate::backend::{
        BackendInfo, Launch, Sidecar, SidecarError, SidecarState, SidecarStatus, LOG_FILE,
        READY_PREFIX, STOP_GRACE,
    };
}

// RootMind desktop is a client of the RootMind backend REST API. Which
// backend it talks to is decided in the UI (src/state/bootstrap.ts):
// - Built-in (default): the backend that ships with the app, started here as
//   a child process on a free loopback port with a fresh token for every
//   start, and stopped when the app exits (backend.rs).
// - Background service: the backend runs as a Windows service. This side reads
//   its state unelevated, hands out its token only after checking that the
//   service's own process listens on its port, and runs the backend's
//   `service install|uninstall` commands elevated, with arguments built here
//   and never taken from the webview (service.rs, win/).
// - Custom: a URL (and optional token) the user enters; Rust is not involved.
// The backend owns indexing, scheduling and its config.yaml in every mode.
// The Rust side also persists chat history (conversations.rs).

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    let builder = tauri::Builder::default();
    // Must be the first plugin: a second launch focuses this window instead
    // of starting a second backend on the same data folder.
    #[cfg(any(target_os = "macos", windows, target_os = "linux"))]
    let builder = builder.plugin(tauri_plugin_single_instance::init(|app, _args, _cwd| {
        use tauri::Manager;
        if let Some(window) = app.get_webview_window("main") {
            let _ = window.unminimize();
            let _ = window.show();
            let _ = window.set_focus();
        }
    }));
    let app = builder
        // Process spawning from Rust only; the webview has no shell:* permission.
        .plugin(tauri_plugin_shell::init())
        .plugin(tauri_plugin_opener::init())
        .plugin(tauri_plugin_dialog::init())
        .manage(backend::Sidecar::default())
        .manage(service::ServiceOps::default())
        .invoke_handler(tauri::generate_handler![
            conversations::save_conversation,
            conversations::load_conversation,
            conversations::list_conversations,
            conversations::delete_conversation,
            backend::app_mode,
            backend::sidecar_start,
            backend::sidecar_status,
            backend::sidecar_restart,
            backend::backend_log_dir,
            service::service_info,
            service::service_connect,
            service::service_enable,
            service::service_disable,
            service::service_set_mcp,
            service::service_update,
            service::service_mcp_token,
            service::service_details,
        ])
        .build(tauri::generate_context!())
        .expect("error while building the tauri application");

    app.run(|handle, event| {
        if let tauri::RunEvent::Exit = event {
            backend::shutdown(handle);
        }
    });
}
