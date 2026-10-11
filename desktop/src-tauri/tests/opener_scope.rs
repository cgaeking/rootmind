//! Guards the opener scope in `capabilities/default.json`: the webview may
//! open the document types the backend indexes and nothing else (no
//! executables, scripts or shortcuts).
//!
//! The matching mirrors tauri's fs scope (`tauri::scope::fs`): it matches the
//! canonical path, `*` does not cross path separators, and globs are case
//! sensitive. That is why the patterns spell each extension as `[pP][dD][fF]`.

use std::fs;
use std::path::{Path, PathBuf};

use glob::{MatchOptions, Pattern};
use serde_json::Value;

const OPTIONS: MatchOptions = MatchOptions {
    case_sensitive: true,
    require_literal_separator: true,
    require_literal_leading_dot: false,
};

/// Files a compromised webview must never be able to launch.
const NEVER_OPEN: &[&str] = &[
    "setup.exe",
    "report.pdf.exe",
    "script.bat",
    "script.cmd",
    "program.com",
    "script.ps1",
    "script.vbs",
    "script.js",
    "script.py",
    "app.hta",
    "installer.msi",
    "screensaver.scr",
    "shortcut.lnk",
    "website.url",
    "no-extension",
];

fn manifest_path(rel: &str) -> PathBuf {
    Path::new(env!("CARGO_MANIFEST_DIR")).join(rel)
}

/// The `opener:allow-open-path` patterns, compiled the way tauri compiles them.
fn open_path_patterns() -> Vec<Pattern> {
    let file = manifest_path("capabilities/default.json");
    let raw = fs::read_to_string(file).unwrap();
    let capability: Value = serde_json::from_str(&raw).unwrap();
    let permissions = capability["permissions"].as_array().unwrap();
    let open_path = permissions
        .iter()
        .find(|p| p["identifier"] == "opener:allow-open-path")
        .expect("opener:allow-open-path is missing");
    let mut patterns = Vec::new();
    for entry in open_path["allow"].as_array().unwrap() {
        // tauri parses the pattern as a path first, which normalizes separators.
        let spec = entry["path"].as_str().unwrap();
        let path: PathBuf = Path::new(spec).components().collect();
        let pattern = Pattern::new(&path.to_string_lossy()).unwrap();
        patterns.push(pattern);
    }
    patterns
}

/// The file extensions the backend indexes, read from its parser registry.
fn backend_extensions() -> Vec<String> {
    let file = manifest_path("../../backend/src/rootmind/indexer/parser.py");
    let source = fs::read_to_string(file).expect("read parser.py");
    let mut extensions = Vec::new();
    for line in source.lines().map(str::trim) {
        // Registry entries look like `".pdf": PDFParser(),`.
        if line.starts_with("\".") && line.contains("Parser()") {
            let ext = line.split('"').nth(1).unwrap();
            extensions.push(ext.trim_start_matches('.').to_string());
        }
    }
    assert!(!extensions.is_empty(), "parser.py has no extensions");
    extensions
}

/// A scratch directory that is removed when the test ends.
struct ScratchDir(PathBuf);

impl ScratchDir {
    fn new(tag: &str) -> Self {
        let name = format!("rootmind-scope-{}-{tag}", std::process::id());
        let dir = std::env::temp_dir().join(name);
        fs::create_dir_all(&dir).unwrap();
        Self(dir)
    }

    /// Create an empty file and report whether the scope allows opening it.
    /// Like tauri, match the canonical path (`\\?\C:\...` on Windows).
    fn allows(&self, patterns: &[Pattern], name: &str) -> bool {
        let file = self.0.join(name);
        fs::write(&file, b"").unwrap();
        let canonical: PathBuf = fs::canonicalize(file).unwrap().components().collect();
        let is_match = |p: &Pattern| p.matches_path_with(&canonical, OPTIONS);
        patterns.iter().any(is_match)
    }
}

impl Drop for ScratchDir {
    fn drop(&mut self) {
        let _ = fs::remove_dir_all(&self.0);
    }
}

#[test]
fn opens_every_indexed_document_type_in_any_case() {
    let patterns = open_path_patterns();
    let dir = ScratchDir::new("indexed");
    for ext in backend_extensions() {
        let lower = format!("doc.{ext}");
        let upper = format!("DOC2.{}", ext.to_uppercase());
        assert!(dir.allows(&patterns, &lower), "cannot open {lower}");
        assert!(dir.allows(&patterns, &upper), "cannot open {upper}");
    }
}

#[test]
fn never_opens_executables_scripts_or_shortcuts() {
    let patterns = open_path_patterns();
    let dir = ScratchDir::new("blocked");
    for name in NEVER_OPEN {
        assert!(!dir.allows(&patterns, name), "may open {name}");
    }
}
