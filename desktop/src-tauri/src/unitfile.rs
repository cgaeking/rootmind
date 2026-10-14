// The systemd user units of the RootMind services on Linux.
//
// The backend's `service install` writes (see ARCHITECTURE.md, interface
// contract; backend/src/rootmind/service/systemd.py):
//   # RootMind background service: written by "rootmind-backend service install"; it rewrites this file.
//   [Service]
//   ExecStart="<data>/runtime/<ver>-<id>/rootmind-backend" service run backend --port <P>
// in ~/.config/systemd/user/rootmind-backend.service and
// rootmind-mcp-http.service. In ExecStart `\` and `"` are escaped with a
// backslash, `%` is written `%%` and `$` `$$`. The app reads the file to learn
// the port and whether the unit is ours at all, with the same strict rules as
// the backend: anything else is a unit this app did not install.
//
// The parser is platform-neutral so it is tested everywhere; only the Linux
// build reads unit files (linux/).
#![cfg_attr(not(target_os = "linux"), allow(dead_code))]

use std::path::Path;

use crate::imagepath::{ImagePath, Role};

pub const MARKER: &str = "# RootMind background service: written by \"rootmind-backend service install\"; it rewrites this file.";
pub const BACKEND_UNIT: &str = "rootmind-backend.service";
pub const MCP_UNIT: &str = "rootmind-mcp-http.service";
const EXE_NAME: &str = "rootmind-backend";
const RUNTIME_DIR: &str = "runtime";

/// The unit file of a role.
pub fn unit_name(role: Role) -> &'static str {
    match role {
        Role::Backend => BACKEND_UNIT,
        Role::McpHttp => MCP_UNIT,
    }
}

fn unescape(text: &str) -> Option<String> {
    let mut out = String::with_capacity(text.len());
    let mut chars = text.chars();
    while let Some(c) = chars.next() {
        match c {
            '\\' | '%' | '$' => {
                let next = chars.next()?;
                let valid = (c == '\\' && (next == '\\' || next == '"')) || next == c;
                if !valid {
                    return None;
                }
                out.push(next);
            }
            '"' => return None,
            _ => out.push(c),
        }
    }
    Some(out)
}

/// Parse an `ExecStart=` value; `None` if it does not follow the contract.
pub fn parse_exec_start(value: &str) -> Option<ImagePath> {
    let value = value.trim();
    let rest = value.strip_prefix('"')?;
    // The closing quote is the first one not escaped by a backslash.
    let bytes = rest.as_bytes();
    let mut i = 0;
    let close = loop {
        match bytes.get(i)? {
            b'\\' => i += 2,
            b'"' => break i,
            _ => i += 1,
        }
    };
    let exe = unescape(rest.get(..close)?)?;
    let args = rest.get(close + 1..)?;
    if exe.is_empty() || !args.starts_with(' ') {
        return None;
    }
    let tokens: Vec<&str> = args.split_whitespace().collect();
    let [service, run, role, flag, port] = tokens.as_slice() else {
        return None;
    };
    if *service != "service" || *run != "run" || *flag != "--port" {
        return None;
    }
    let role = match *role {
        "backend" => Role::Backend,
        "mcp-http" => Role::McpHttp,
        _ => return None,
    };
    if port.is_empty() || port.len() > 5 || !port.bytes().all(|b| b.is_ascii_digit()) {
        return None;
    }
    let port = match port.parse::<u16>() {
        Ok(0) | Err(_) => return None,
        Ok(port) => port,
    };
    Some(ImagePath {
        exe,
        role,
        port,
        user_sid: None,
    })
}

/// The unit's program, role and port when it is a RootMind unit file: the
/// marker is the first line and `[Service]` has exactly one `ExecStart=` that
/// follows the contract.
pub fn parse_unit(text: &str) -> Option<ImagePath> {
    let mut lines = text.lines();
    if lines.next()?.trim() != MARKER {
        return None;
    }
    let mut section = "";
    let mut starts = Vec::new();
    for line in text.lines() {
        let line = line.trim();
        if line.starts_with('[') && line.ends_with(']') {
            section = line;
        } else if section == "[Service]" {
            if let Some(value) = line.strip_prefix("ExecStart=") {
                starts.push(value);
            }
        }
    }
    match starts.as_slice() {
        [only] => parse_exec_start(only),
        _ => None,
    }
}

/// Whether `exe` is exactly `<data_root>/runtime/<folder>/rootmind-backend`.
pub fn exe_is_ours(exe: &str, data_root: &Path) -> bool {
    let Some(root) = data_root.to_str() else {
        return false;
    };
    let prefix = format!("{}/{RUNTIME_DIR}/", root.trim_end_matches('/'));
    let Some(rest) = exe.strip_prefix(&prefix) else {
        return false;
    };
    let mut parts = rest.split('/');
    let (Some(folder), Some(file), None) = (parts.next(), parts.next(), parts.next()) else {
        return false;
    };
    !folder.is_empty() && folder != "." && folder != ".." && file == EXE_NAME
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::path::PathBuf;

    const EXE: &str = "/home/me/.local/share/rootmind/runtime/2.3.0-0a1b2c3d/rootmind-backend";

    // The golden unit shared with the backend (tests/test_service_systemd.py).
    fn golden(exec_start: &str) -> String {
        format!(
            "{MARKER}\n[Unit]\nDescription=RootMind backend (REST API and document indexing for the RootMind app)\n\
             Documentation=https://github.com/cgaeking/rootmind#linux\n\n[Service]\nType=notify\n\
             ExecStart={exec_start}\nRestart=on-failure\nRestartSec=5\nRestartPreventExitStatus=2\n\
             TimeoutStartSec=90\nTimeoutStopSec=30\nUMask=0077\nNoNewPrivileges=yes\n\n[Install]\n\
             WantedBy=default.target\n"
        )
    }

    #[test]
    fn parses_the_golden_unit() {
        let unit = parse_unit(&golden(&format!(
            "\"{EXE}\" service run backend --port 8000"
        )))
        .unwrap();
        assert_eq!(
            unit,
            ImagePath {
                exe: EXE.into(),
                role: Role::Backend,
                port: 8000,
                user_sid: None
            }
        );
        let mcp = parse_unit(&golden(&format!(
            "\"{EXE}\" service run mcp-http --port 18400"
        )))
        .unwrap();
        assert_eq!((mcp.role, mcp.port), (Role::McpHttp, 18400));
    }

    #[test]
    fn unescapes_like_the_backend() {
        let cases = [
            (
                r#""/home/a\"b/rootmind-backend""#,
                "/home/a\"b/rootmind-backend",
            ),
            (
                r#""/home/a\\b/rootmind-backend""#,
                "/home/a\\b/rootmind-backend",
            ),
            (
                r#""/home/100%%/rootmind-backend""#,
                "/home/100%/rootmind-backend",
            ),
            (
                r#""/home/$$USER/rootmind-backend""#,
                "/home/$USER/rootmind-backend",
            ),
            (
                r#""/home/José Müller/rootmind-backend""#,
                "/home/José Müller/rootmind-backend",
            ),
        ];
        for (quoted, exe) in cases {
            let parsed = parse_exec_start(&format!("{quoted} service run backend --port 8000"));
            assert_eq!(parsed.map(|p| p.exe), Some(exe.to_string()), "{quoted}");
        }
    }

    #[test]
    fn rejects_other_shapes() {
        let bad = [
            format!("{EXE} service run backend --port 8000"),
            format!("\"{EXE}\"service run backend --port 8000"),
            format!("\"{EXE}\" service run backend --port 8000 --user-sid S-1-5-21-1"),
            format!("\"{EXE}\" service run indexer --port 8000"),
            format!("\"{EXE}\" service run backend --port 0"),
            format!("\"{EXE}\" service run backend --port 70000"),
            format!("\"{EXE}\" service run backend --port +80"),
            format!("\"{EXE}\" service run backend"),
            "\"/a%b\" service run backend --port 8000".into(),
            "\"/a$b\" service run backend --port 8000".into(),
            "\"/a\\x\" service run backend --port 8000".into(),
            format!("\"{EXE}"),
        ];
        for line in bad {
            assert_eq!(parse_exec_start(&line), None, "accepted {line}");
        }
        let good = format!("\"{EXE}\" service run backend --port 8000");
        assert_eq!(parse_unit(&golden(&good).replace(MARKER, "# mine")), None);
        let twice = golden(&good).replace(
            "Restart=on-failure",
            &format!("ExecStart={good}\nRestart=on-failure"),
        );
        assert_eq!(parse_unit(&twice), None);
        assert_eq!(parse_unit(&golden(&good).replace("[Service]", "[X]")), None);
        assert_eq!(parse_unit(""), None);
    }

    #[test]
    fn ours_requires_the_runtime_folder() {
        let root = PathBuf::from("/home/me/.local/share/rootmind");
        assert!(exe_is_ours(EXE, &root));
        assert!(exe_is_ours(
            EXE,
            &PathBuf::from("/home/me/.local/share/rootmind/")
        ));
        for exe in [
            "/home/me/.local/share/rootmind/runtime/rootmind-backend",
            "/home/me/.local/share/rootmind/runtime/a/b/rootmind-backend",
            "/home/me/.local/share/rootmind/runtime/../x/rootmind-backend",
            "/home/me/.local/share/rootmind/runtime//rootmind-backend",
            "/home/me/.local/share/rootmind/runtime/x/other",
            "/usr/lib/RootMind/backend/rootmind-backend",
            "/home/me/.local/share/rootmindx/runtime/a/rootmind-backend",
        ] {
            assert!(!exe_is_ours(exe, &root), "accepted {exe}");
        }
    }

    #[test]
    fn unit_names() {
        assert_eq!(unit_name(Role::Backend), "rootmind-backend.service");
        assert_eq!(unit_name(Role::McpHttp), "rootmind-mcp-http.service");
    }
}
