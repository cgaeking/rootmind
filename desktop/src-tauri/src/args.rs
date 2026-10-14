// Command-line quoting for the elevated backend.
//
// ShellExecuteExW takes the arguments as one string (lpParameters). The
// backend's C runtime splits it with the rules of CommandLineToArgvW, so every
// argument is quoted to come out of that split unchanged:
// - an argument without spaces, tabs, newlines or quotes is passed as is;
// - otherwise it is wrapped in quotes, a quote inside becomes \" and the
//   backslashes in front of a quote (including the closing one) are doubled.
// Only arguments built in Rust from fixed parts and paths reach this module;
// nothing comes from the webview.

// Only the Windows build elevates; the Linux build runs the backend with an
// argument list and keeps this module for its tests.
#![cfg_attr(target_os = "linux", allow(dead_code))]

use std::fmt;

/// An argument that cannot be passed on a Windows command line.
#[derive(Debug, PartialEq, Eq)]
pub struct ArgError(String);

impl fmt::Display for ArgError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str(&self.0)
    }
}

/// Quote one argument so CommandLineToArgvW returns it unchanged.
pub fn quote_arg(arg: &str) -> Result<String, ArgError> {
    if arg.contains('\0') {
        return Err(ArgError("an argument contains a NUL character".into()));
    }
    let needs_quotes = arg.is_empty()
        || arg
            .chars()
            .any(|c| matches!(c, ' ' | '\t' | '\n' | '\x0b' | '"'));
    if !needs_quotes {
        return Ok(arg.to_string());
    }
    let mut out = String::with_capacity(arg.len() + 2);
    out.push('"');
    let mut backslashes = 0usize;
    for c in arg.chars() {
        match c {
            '\\' => backslashes += 1,
            '"' => {
                // 2n backslashes give n literal ones; the extra one escapes the quote.
                push_backslashes(&mut out, backslashes * 2 + 1);
                out.push('"');
                backslashes = 0;
            }
            _ => {
                // Backslashes not followed by a quote are literal.
                push_backslashes(&mut out, backslashes);
                out.push(c);
                backslashes = 0;
            }
        }
    }
    // Backslashes before the closing quote must be doubled, or the last one
    // would escape it ("C:\dir\" would swallow the quote).
    push_backslashes(&mut out, backslashes * 2);
    out.push('"');
    Ok(out)
}

/// Quote each argument and join them with single spaces.
pub fn join_args<S: AsRef<str>>(args: &[S]) -> Result<String, ArgError> {
    let quoted = args
        .iter()
        .map(|a| quote_arg(a.as_ref()))
        .collect::<Result<Vec<_>, _>>()?;
    Ok(quoted.join(" "))
}

fn push_backslashes(out: &mut String, n: usize) {
    out.extend(std::iter::repeat_n('\\', n));
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn plain_arguments_are_unchanged() {
        assert_eq!(quote_arg("service").unwrap(), "service");
        assert_eq!(quote_arg("--port").unwrap(), "--port");
        assert_eq!(
            quote_arg("S-1-5-21-1-2-3-1001").unwrap(),
            "S-1-5-21-1-2-3-1001"
        );
        // Backslashes without a quote after them are literal.
        assert_eq!(quote_arg(r"C:\dir\file.yaml").unwrap(), r"C:\dir\file.yaml");
        assert_eq!(quote_arg(r"C:\dir\").unwrap(), r"C:\dir\");
    }

    #[test]
    fn spaces_are_quoted() {
        assert_eq!(
            quote_arg(r"C:\Users\Jane Doe\config.yaml").unwrap(),
            r#""C:\Users\Jane Doe\config.yaml""#
        );
        assert_eq!(quote_arg("a\tb").unwrap(), "\"a\tb\"");
        assert_eq!(quote_arg("").unwrap(), r#""""#);
    }

    #[test]
    fn trailing_backslash_is_doubled_inside_quotes() {
        assert_eq!(quote_arg(r"C:\My Dir\").unwrap(), r#""C:\My Dir\\""#);
        assert_eq!(quote_arg(r"C:\My Dir\\").unwrap(), r#""C:\My Dir\\\\""#);
    }

    #[test]
    fn quotes_are_escaped() {
        assert_eq!(quote_arg(r#"say "hi""#).unwrap(), r#""say \"hi\"""#);
        // Backslashes before a quote are doubled, plus one for the quote.
        assert_eq!(quote_arg(r#"a\"b"#).unwrap(), r#""a\\\"b""#);
        assert_eq!(quote_arg(r#"""#).unwrap(), r#""\"""#);
    }

    #[test]
    fn nul_is_refused() {
        assert!(quote_arg("a\0b").is_err());
        assert!(join_args(&["ok", "a\0b"]).is_err());
    }

    #[test]
    fn join_uses_single_spaces() {
        let joined = join_args(&[
            "service",
            "install",
            "--mcp-token-from",
            r"C:\Users\Jane Doe\Documents\api_token.txt",
        ])
        .unwrap();
        assert_eq!(
            joined,
            r#"service install --mcp-token-from "C:\Users\Jane Doe\Documents\api_token.txt""#
        );
    }

    /// The real parser agrees with the quoting for awkward inputs.
    #[cfg(windows)]
    #[test]
    fn round_trips_through_command_line_to_argv() {
        let cases: &[&str] = &[
            "plain",
            "",
            "with space",
            r"C:\My Dir\",
            r"C:\My Dir\\",
            r"C:\no-space\",
            r#"quote " inside"#,
            r#"a\"b"#,
            r#"\\server\share\"#,
            "tab\there",
            "ünïcödé päth",
        ];
        let line = join_args(cases).unwrap();
        assert_eq!(split_with_command_line_to_argv(&line), cases);
    }

    /// Split `line` with CommandLineToArgvW. A dummy program name goes first,
    /// because the first token follows different rules.
    #[cfg(windows)]
    fn split_with_command_line_to_argv(line: &str) -> Vec<String> {
        use windows_sys::Win32::Foundation::LocalFree;
        use windows_sys::Win32::UI::Shell::CommandLineToArgvW;

        let full = format!("prog.exe {line}");
        let wide: Vec<u16> = full.encode_utf16().chain(Some(0)).collect();
        let mut argc = 0i32;
        // SAFETY: `wide` is NUL-terminated and outlives the call; argc is a
        // valid out pointer.
        let argv = unsafe { CommandLineToArgvW(wide.as_ptr(), &mut argc) };
        assert!(!argv.is_null(), "CommandLineToArgvW failed");
        let mut out = Vec::new();
        for i in 1..argc as usize {
            // SAFETY: argv holds argc valid, NUL-terminated wide strings.
            let arg = unsafe { *argv.add(i) };
            let mut len = 0usize;
            // SAFETY: each string is NUL-terminated, so the scan stays inside it.
            while unsafe { *arg.add(len) } != 0 {
                len += 1;
            }
            // SAFETY: `len` u16 values starting at `arg` were just read.
            let slice = unsafe { std::slice::from_raw_parts(arg, len) };
            out.push(String::from_utf16(slice).unwrap());
        }
        // SAFETY: argv was allocated by CommandLineToArgvW and is freed once.
        unsafe { LocalFree(argv as _) };
        out
    }
}
