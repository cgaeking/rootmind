// Chat-history persistence: each conversation is one JSON file under
// <app_data_dir>/conversations/<id>.json. Listing returns lightweight metadata
// only (parsed server-side); full message content is loaded on demand.

use std::fs;
use std::path::PathBuf;

use tauri::Manager;

fn conversations_dir(app: &tauri::AppHandle) -> Result<PathBuf, String> {
    let dir = app
        .path()
        .app_data_dir()
        .map_err(|e| e.to_string())?
        .join("conversations");
    fs::create_dir_all(&dir).map_err(|e| e.to_string())?;
    Ok(dir)
}

/// Reject ids that aren't a plain slug, so they can't escape the directory.
fn safe_id(id: &str) -> Result<(), String> {
    let ok = !id.is_empty()
        && id.len() <= 100
        && id
            .chars()
            .all(|c| c.is_ascii_alphanumeric() || c == '-' || c == '_');
    if ok {
        Ok(())
    } else {
        Err("invalid conversation id".into())
    }
}

#[tauri::command]
pub fn save_conversation(app: tauri::AppHandle, id: String, json: String) -> Result<(), String> {
    safe_id(&id)?;
    let path = conversations_dir(&app)?.join(format!("{id}.json"));
    fs::write(path, json).map_err(|e| e.to_string())
}

#[tauri::command]
pub fn delete_conversation(app: tauri::AppHandle, id: String) -> Result<(), String> {
    safe_id(&id)?;
    let path = conversations_dir(&app)?.join(format!("{id}.json"));
    if path.exists() {
        fs::remove_file(path).map_err(|e| e.to_string())?;
    }
    Ok(())
}

#[tauri::command]
pub fn load_conversation(app: tauri::AppHandle, id: String) -> Result<Option<String>, String> {
    safe_id(&id)?;
    let path = conversations_dir(&app)?.join(format!("{id}.json"));
    if !path.exists() {
        return Ok(None);
    }
    fs::read_to_string(path).map(Some).map_err(|e| e.to_string())
}

/// Return metadata for every saved conversation, newest first. Each entry is a
/// JSON string: { id, title, createdAt, updatedAt, messageCount }.
#[tauri::command]
pub fn list_conversations(app: tauri::AppHandle) -> Result<Vec<String>, String> {
    let dir = conversations_dir(&app)?;
    let mut metas: Vec<(f64, String)> = Vec::new();
    for entry in fs::read_dir(&dir).map_err(|e| e.to_string())? {
        let entry = match entry {
            Ok(e) => e,
            Err(_) => continue,
        };
        let path = entry.path();
        if path.extension().and_then(|e| e.to_str()) != Some("json") {
            continue;
        }
        let content = match fs::read_to_string(&path) {
            Ok(c) => c,
            Err(_) => continue,
        };
        let v: serde_json::Value = match serde_json::from_str(&content) {
            Ok(v) => v,
            Err(_) => continue,
        };
        let created = v.get("createdAt").and_then(|x| x.as_f64()).unwrap_or(0.0);
        let updated = v
            .get("updatedAt")
            .and_then(|x| x.as_f64())
            .unwrap_or(created);
        let meta = serde_json::json!({
            "id": v.get("id").and_then(|x| x.as_str()).unwrap_or(""),
            "title": v.get("title").and_then(|x| x.as_str()).unwrap_or("Untitled"),
            "createdAt": created,
            "updatedAt": updated,
            "messageCount": v.get("messages").and_then(|x| x.as_array()).map(|a| a.len()).unwrap_or(0),
        });
        metas.push((updated, meta.to_string()));
    }
    metas.sort_by(|a, b| b.0.partial_cmp(&a.0).unwrap_or(std::cmp::Ordering::Equal));
    Ok(metas.into_iter().map(|(_, m)| m).collect())
}

#[cfg(test)]
mod tests {
    use super::safe_id;

    #[test]
    fn accepts_plain_slugs() {
        assert!(safe_id("abc-1_2").is_ok());
        assert!(safe_id("1712345678901-x7k2").is_ok());
        assert!(safe_id(&"a".repeat(100)).is_ok());
    }

    #[test]
    fn rejects_empty_and_too_long() {
        assert!(safe_id("").is_err());
        assert!(safe_id(&"a".repeat(101)).is_err());
    }

    #[test]
    fn rejects_path_traversal_and_separators() {
        for id in ["../x", "..", "a/b", "a\\b", "a.b", "/abs", "C:x", "a b"] {
            assert!(safe_id(id).is_err(), "accepted {id:?}");
        }
    }

    #[test]
    fn rejects_non_ascii() {
        assert!(safe_id("caf\u{e9}").is_err());
        assert!(safe_id("\u{0430}bc").is_err()); // Cyrillic "a"
    }
}
