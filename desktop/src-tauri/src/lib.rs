// Every unsafe block states why it is sound.
#![warn(clippy::undocumented_unsafe_blocks)]

mod app;
mod args;
mod conversations;
mod imagepath;
#[cfg(target_os = "linux")]
mod linux;
mod procnet;
mod proof;
mod service;
mod tcptable;
mod unitfile;
#[cfg(windows)]
mod win;

// RootMind desktop is a client of the RootMind backend REST API. Since 2.0
// the backend always runs as a Windows service, which the per-machine
// installer sets up for the account that installs the app (on Linux: two
// systemd user units, which the app sets up itself; service.rs, linux/):
// - Background service: this side reads its state unelevated, hands out its
//   token only after checking that the service's own process listens on its
//   port, and runs the backend's `service install` elevated to repair it or to
//   switch MCP over HTTP, with arguments built here and never taken from the
//   webview (service.rs, win/).
// - Custom: a URL (and optional token) the user enters, for development and
//   other setups; Rust is not involved.
// The backend owns indexing, scheduling and its config.yaml. The Rust side
// also persists chat history (conversations.rs).

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    let builder = tauri::Builder::default();
    // Must be the first plugin: a second launch focuses this window instead
    // of opening a second one that writes the same chat history.
    #[cfg(any(target_os = "macos", windows, target_os = "linux"))]
    let builder = builder.plugin(tauri_plugin_single_instance::init(|app, _args, _cwd| {
        use tauri::Manager;
        if let Some(window) = app.get_webview_window("main") {
            let _ = window.unminimize();
            let _ = window.show();
            let _ = window.set_focus();
        }
    }));
    builder
        .plugin(tauri_plugin_opener::init())
        .plugin(tauri_plugin_dialog::init())
        .manage(service::ServiceOps::default())
        .invoke_handler(tauri::generate_handler![
            conversations::save_conversation,
            conversations::load_conversation,
            conversations::list_conversations,
            conversations::delete_conversation,
            app::app_mode,
            service::service_info,
            service::service_connect,
            service::service_set_mcp,
            service::service_update,
            service::service_mcp_token,
            service::service_details,
        ])
        .run(tauri::generate_context!())
        .expect("error while running the tauri application");
}
