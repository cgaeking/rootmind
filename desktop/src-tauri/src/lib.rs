mod conversations;

// RootMind desktop is a pure client of the RootMind backend REST API
// (http://127.0.0.1:8000 by default). The backend runs as its own process or
// Windows service and owns indexing, scheduling and its config.yaml. This app
// never starts, stops or manages the backend; it only talks to it over HTTP.
// The Rust side only persists chat history (see conversations.rs).

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    tauri::Builder::default()
        .plugin(tauri_plugin_opener::init())
        .plugin(tauri_plugin_dialog::init())
        .invoke_handler(tauri::generate_handler![
            conversations::save_conversation,
            conversations::load_conversation,
            conversations::list_conversations,
            conversations::delete_conversation,
        ])
        .run(tauri::generate_context!())
        .expect("error while running tauri application");
}
