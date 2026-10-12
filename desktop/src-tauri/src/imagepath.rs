// Parser for the ImagePath of the RootMind services.
//
// The backend's service installer writes (see ARCHITECTURE.md, interface
// contract):
//   "<PF>\RootMind Service\<ver>-<id>\rootmind-backend.exe" service run backend --port <P> --user-sid <SID>
//   "<PF>\RootMind Service\<ver>-<id>\rootmind-backend.exe" service run mcp-http --port <M>
// The app reads it unelevated from HKLM to learn the port, the account the
// service belongs to and whether it is ours at all. The parser is strict:
// anything else is treated as a service this app did not install.

/// The role a RootMind service runs.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Role {
    Backend,
    McpHttp,
}

/// A parsed ImagePath.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct ImagePath {
    /// The executable, without quotes.
    pub exe: String,
    pub role: Role,
    pub port: u16,
    /// The account whose rights the backend uses (backend role only).
    pub user_sid: Option<String>,
}

/// Parse an ImagePath value. `None` if it does not follow the contract.
pub fn parse(raw: &str) -> Option<ImagePath> {
    let raw = raw.trim();
    let rest = raw.strip_prefix('"')?;
    let close = rest.find('"')?;
    let exe = &rest[..close];
    let args = &rest[close + 1..];
    // The quoted path must be followed by whitespace and arguments.
    if exe.is_empty() || !args.starts_with([' ', '\t']) {
        return None;
    }

    let mut tokens = args.split_whitespace();
    if tokens.next()? != "service" || tokens.next()? != "run" {
        return None;
    }
    let role = match tokens.next()? {
        "backend" => Role::Backend,
        "mcp-http" => Role::McpHttp,
        _ => return None,
    };

    let mut port = None;
    let mut user_sid = None;
    while let Some(flag) = tokens.next() {
        let value = tokens.next()?;
        match flag {
            "--port" if port.is_none() => port = Some(parse_port(value)?),
            "--user-sid" if user_sid.is_none() && role == Role::Backend => {
                if !is_sid(value) {
                    return None;
                }
                user_sid = Some(value.to_string());
            }
            _ => return None,
        }
    }

    let port = port?;
    if role == Role::Backend && user_sid.is_none() {
        return None;
    }
    Some(ImagePath {
        exe: exe.to_string(),
        role,
        port,
        user_sid,
    })
}

/// Whether `exe` is a RootMind service binary: exactly
/// `<program_files>\RootMind Service\<folder>\rootmind-backend.exe`.
/// `program_files` comes from the known-folder API, never from the environment.
pub fn exe_is_ours(exe: &str, program_files: &str) -> bool {
    if program_files.is_empty() || exe.contains('/') {
        return false;
    }
    let prefix = format!(
        "{}\\rootmind service\\",
        program_files.trim_end_matches('\\').to_lowercase()
    );
    let exe = exe.to_lowercase();
    let Some(rest) = exe.strip_prefix(&prefix) else {
        return false;
    };
    let mut parts = rest.split('\\');
    let (Some(folder), Some(file), None) = (parts.next(), parts.next(), parts.next()) else {
        return false;
    };
    !folder.is_empty()
        && folder != "."
        && folder != ".."
        && !folder.contains(':')
        && file == "rootmind-backend.exe"
}

fn parse_port(value: &str) -> Option<u16> {
    if value.is_empty() || !value.bytes().all(|b| b.is_ascii_digit()) {
        return None;
    }
    match value.parse::<u16>() {
        Ok(0) | Err(_) => None,
        Ok(port) => Some(port),
    }
}

/// A string SID such as `S-1-5-21-1-2-3-1001`.
pub fn is_sid(value: &str) -> bool {
    let Some(rest) = value.strip_prefix("S-1-") else {
        return false;
    };
    value.len() <= 184
        && !rest.is_empty()
        && rest
            .split('-')
            .all(|part| !part.is_empty() && part.bytes().all(|b| b.is_ascii_digit()))
}

#[cfg(test)]
mod tests {
    use super::*;

    // The golden strings shared with the backend's service package.
    const GOLDEN_BACKEND: &str = r#""C:\Program Files\RootMind Service\1.2.0-0a1b2c3d\rootmind-backend.exe" service run backend --port 8000 --user-sid S-1-5-21-1-2-3-1001"#;
    const GOLDEN_MCP: &str = r#""C:\Program Files\RootMind Service\1.2.0-0a1b2c3d\rootmind-backend.exe" service run mcp-http --port 3000"#;
    const PF: &str = r"C:\Program Files";

    #[test]
    fn parses_golden_backend() {
        let parsed = parse(GOLDEN_BACKEND).unwrap();
        assert_eq!(
            parsed,
            ImagePath {
                exe: r"C:\Program Files\RootMind Service\1.2.0-0a1b2c3d\rootmind-backend.exe"
                    .into(),
                role: Role::Backend,
                port: 8000,
                user_sid: Some("S-1-5-21-1-2-3-1001".into()),
            }
        );
        assert!(exe_is_ours(&parsed.exe, PF));
    }

    #[test]
    fn parses_golden_mcp() {
        let parsed = parse(GOLDEN_MCP).unwrap();
        assert_eq!(parsed.role, Role::McpHttp);
        assert_eq!(parsed.port, 3000);
        assert_eq!(parsed.user_sid, None);
        assert!(exe_is_ours(&parsed.exe, PF));
    }

    #[test]
    fn tolerates_surrounding_and_repeated_whitespace() {
        let raw = format!("  {}  ", GOLDEN_MCP.replace(" --port", "   --port"));
        assert_eq!(parse(&raw).unwrap().port, 3000);
    }

    #[test]
    fn rejects_other_shapes() {
        let bad = [
            // unquoted path
            r"C:\Program Files\RootMind Service\1.2.0-0a1b2c3d\rootmind-backend.exe service run backend --port 8000 --user-sid S-1-5-21-1",
            // another program, without arguments
            r#""C:\Program Files\RootMind Service\other-service.exe""#,
            // no arguments
            r#""C:\x\rootmind-backend.exe""#,
            // no space after the closing quote
            r#""C:\x\rootmind-backend.exe"service run mcp-http --port 3000"#,
            // unknown role
            r#""C:\x\rootmind-backend.exe" service run indexer --port 3000"#,
            // missing port
            r#""C:\x\rootmind-backend.exe" service run mcp-http"#,
            r#""C:\x\rootmind-backend.exe" service run mcp-http --port"#,
            // port out of range or not a number
            r#""C:\x\rootmind-backend.exe" service run mcp-http --port 0"#,
            r#""C:\x\rootmind-backend.exe" service run mcp-http --port 70000"#,
            r#""C:\x\rootmind-backend.exe" service run mcp-http --port +80"#,
            // backend without the account
            r#""C:\x\rootmind-backend.exe" service run backend --port 8000"#,
            // an account on the MCP role
            r#""C:\x\rootmind-backend.exe" service run mcp-http --port 3000 --user-sid S-1-5-21-1"#,
            // a malformed SID
            r#""C:\x\rootmind-backend.exe" service run backend --port 8000 --user-sid S-1-5-x"#,
            // unknown or repeated options
            r#""C:\x\rootmind-backend.exe" service run mcp-http --port 3000 --debug"#,
            r#""C:\x\rootmind-backend.exe" service run mcp-http --port 3000 --port 3001"#,
            // a different subcommand
            r#""C:\x\rootmind-backend.exe" api --port 8000"#,
        ];
        for raw in bad {
            assert_eq!(parse(raw), None, "accepted {raw}");
        }
    }

    #[test]
    fn ours_requires_the_program_files_service_folder() {
        let ok = r"C:\Program Files\RootMind Service\1.2.0-0a1b2c3d\rootmind-backend.exe";
        assert!(exe_is_ours(ok, PF));
        assert!(exe_is_ours(ok, r"C:\Program Files\"));
        assert!(exe_is_ours(&ok.to_uppercase(), PF));

        let not_ours = [
            // other folders with the same file name
            r"C:\Program Files\Other\backend\rootmind-backend.exe",
            r"C:\Program Files (x86)\RootMind Service\1.2.0-0a1b2c3d\rootmind-backend.exe",
            r"C:\Users\jane\AppData\Local\RootMind\backend\rootmind-backend.exe",
            // directly in the service folder, or nested deeper
            r"C:\Program Files\RootMind Service\rootmind-backend.exe",
            r"C:\Program Files\RootMind Service\a\b\rootmind-backend.exe",
            // path tricks
            r"C:\Program Files\RootMind Service\..\rootmind-backend.exe",
            r"C:\Program Files\RootMind Service\.\rootmind-backend.exe",
            r"C:\Program Files\RootMind Service\\rootmind-backend.exe",
            r"C:\Program Files\RootMind Service/1.2.0/rootmind-backend.exe",
            // another program
            r"C:\Program Files\RootMind Service\1.2.0-0a1b2c3d\other-service.exe",
        ];
        for exe in not_ours {
            assert!(!exe_is_ours(exe, PF), "accepted {exe}");
        }
        assert!(!exe_is_ours(ok, ""));
    }

    #[test]
    fn sid_shapes() {
        assert!(is_sid("S-1-5-21-1-2-3-1001"));
        assert!(is_sid("S-1-5-18"));
        assert!(!is_sid("S-1-"));
        assert!(!is_sid("S-1-5--18"));
        assert!(!is_sid("s-1-5-18"));
        assert!(!is_sid("S-1-5-18 "));
        assert!(!is_sid("S-1-5-18;evil"));
    }
}
