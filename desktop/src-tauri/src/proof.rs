// Proof that the server on 127.0.0.1:<port> knows the service's token, before
// the app hands the token to the webview (service.rs).
//
// GET /proof?nonce=<64 hex> answers {"proof": hex(HMAC-SHA256(token,
// "rootmind-token-proof\0" + nonce))} without a token (rootmind.auth in the
// backend). A program that took the port does not know the token and cannot
// answer. Unlike the TCP listener table, this does not depend on Windows
// listing the service's socket, which some systems do not.
//
// The request is plain HTTP/1.1 over a std TcpStream to loopback with short
// timeouts: no proxy, no redirects, no HTTP client crate.

use std::io::{self, Read, Write};
use std::net::{Ipv4Addr, SocketAddr, TcpStream};
use std::time::Duration;

use sha2::{Digest, Sha256};

const LABEL: &[u8] = b"rootmind-token-proof\0";
const TIMEOUT: Duration = Duration::from_secs(5);
const MAX_RESPONSE: usize = 16 * 1024;

/// What the server on the port answered.
#[derive(Debug, PartialEq, Eq)]
pub enum Proof {
    /// It proved that it knows the token.
    Proved,
    /// It could not (or nothing answered): another program may use the port.
    Failed,
    /// A RootMind service from before /proof (404 or 401): check the listener table.
    Unknown,
}

/// HMAC-SHA256 (RFC 2104) of `message` under `key`.
pub fn hmac_sha256(key: &[u8], message: &[u8]) -> [u8; 32] {
    const BLOCK: usize = 64;
    let mut block = [0u8; BLOCK];
    if key.len() > BLOCK {
        block[..32].copy_from_slice(&Sha256::digest(key));
    } else {
        block[..key.len()].copy_from_slice(key);
    }
    let mut inner = Sha256::new();
    inner.update(block.map(|b| b ^ 0x36));
    inner.update(message);
    let mut outer = Sha256::new();
    outer.update(block.map(|b| b ^ 0x5c));
    outer.update(inner.finalize());
    outer.finalize().into()
}

fn hex(bytes: &[u8]) -> String {
    bytes.iter().map(|b| format!("{b:02x}")).collect()
}

/// The proof the holder of `token` gives for `nonce` (lowercase hex).
pub fn expected(token: &str, nonce: &str) -> String {
    let mut message = LABEL.to_vec();
    message.extend_from_slice(nonce.as_bytes());
    hex(&hmac_sha256(token.trim().as_bytes(), &message))
}

/// Compare without stopping at the first difference.
fn same(a: &str, b: &str) -> bool {
    a.len() == b.len()
        && a.bytes()
            .zip(b.bytes())
            .fold(0u8, |acc, (x, y)| acc | (x ^ y))
            == 0
}

/// Judge a /proof answer: its status and body.
pub fn judge(status: u16, body: &str, token: &str, nonce: &str) -> Proof {
    match status {
        200 => {
            let proof = serde_json::from_str::<serde_json::Value>(body)
                .ok()
                .and_then(|v| v.get("proof").and_then(|p| p.as_str()).map(str::to_owned));
            match proof {
                Some(p) if same(&p, &expected(token, nonce)) => Proof::Proved,
                _ => Proof::Failed,
            }
        }
        401 | 404 => Proof::Unknown,
        _ => Proof::Failed,
    }
}

/// Split a raw HTTP/1.x response into its status and body.
pub fn parse_response(raw: &[u8]) -> Option<(u16, String)> {
    let text = String::from_utf8_lossy(raw);
    let (head, body) = text.split_once("\r\n\r\n")?;
    let status_line = head.lines().next()?;
    let mut parts = status_line.split_whitespace();
    if !parts.next()?.starts_with("HTTP/1.") {
        return None;
    }
    let status = parts.next()?.parse().ok()?;
    Some((status, body.to_owned()))
}

fn get(port: u16, path: &str) -> io::Result<Vec<u8>> {
    let addr = SocketAddr::from((Ipv4Addr::LOCALHOST, port));
    let mut stream = TcpStream::connect_timeout(&addr, TIMEOUT)?;
    stream.set_read_timeout(Some(TIMEOUT))?;
    stream.set_write_timeout(Some(TIMEOUT))?;
    write!(
        stream,
        "GET {path} HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\nAccept: application/json\r\nConnection: close\r\n\r\n"
    )?;
    let mut raw = Vec::new();
    stream.take(MAX_RESPONSE as u64).read_to_end(&mut raw)?;
    Ok(raw)
}

fn new_nonce() -> io::Result<String> {
    let mut bytes = [0u8; 32];
    getrandom::fill(&mut bytes).map_err(|e| io::Error::other(e.to_string()))?;
    Ok(hex(&bytes))
}

/// Ask the server on 127.0.0.1:`port` to prove that it knows `token`. The
/// token itself is not sent.
pub fn ask(port: u16, token: &str) -> Proof {
    let Ok(nonce) = new_nonce() else {
        return Proof::Failed;
    };
    match get(port, &format!("/proof?nonce={nonce}")) {
        Ok(raw) => match parse_response(&raw) {
            Some((status, body)) => judge(status, &body, token, &nonce),
            None => Proof::Failed,
        },
        Err(_) => Proof::Failed,
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::net::TcpListener;
    use std::thread;

    #[test]
    fn hmac_matches_rfc_4231_test_case_2() {
        let mac = hmac_sha256(b"Jefe", b"what do ya want for nothing?");
        assert_eq!(
            hex(&mac),
            // pragma: allowlist nextline secret (RFC 4231 test vector)
            "5bdcc146bf60754e6a042426089575c75a003f089d2739839dec58b964ec3843"
        );
    }

    #[test]
    fn hmac_hashes_a_long_key() {
        // RFC 4231 test case 6: a 131-byte key.
        let mac = hmac_sha256(
            &[0xaa; 131],
            b"Test Using Larger Than Block-Size Key - Hash Key First",
        );
        assert_eq!(
            hex(&mac),
            // pragma: allowlist nextline secret (RFC 4231 test vector)
            "60e431591ee0b67f0d8a26aacbf5b77f8e0bc6213728c5140546040f0ee37f54"
        );
    }

    #[test]
    fn the_backend_and_the_app_compute_the_same_proof() {
        // Computed with rootmind.auth.token_proof(b"rest-test-token", "ab" * 32).
        let nonce = "ab".repeat(32);
        // pragma: allowlist nextline secret (an HMAC of a test token)
        let python = "ffd65b6048de801f62ca635f1b8469136fd2428eaf001411c1a383d6ba8c0bb8";
        assert_eq!(expected("rest-test-token", &nonce), python);
        assert_eq!(expected(" rest-test-token\n", &nonce), python);
    }

    #[test]
    fn judges_answers() {
        let nonce = "cd".repeat(32);
        let good = format!(r#"{{"proof":"{}"}}"#, expected("tok", &nonce));
        assert_eq!(judge(200, &good, "tok", &nonce), Proof::Proved);
        assert_eq!(judge(200, &good, "other", &nonce), Proof::Failed);
        assert_eq!(
            judge(200, r#"{"proof":"00"}"#, "tok", &nonce),
            Proof::Failed
        );
        assert_eq!(judge(200, "<html>404</html>", "tok", &nonce), Proof::Failed);
        assert_eq!(judge(404, "", "tok", &nonce), Proof::Unknown);
        assert_eq!(judge(401, "", "tok", &nonce), Proof::Unknown);
        assert_eq!(judge(500, "", "tok", &nonce), Proof::Failed);
    }

    #[test]
    fn parses_responses() {
        let raw = b"HTTP/1.1 200 OK\r\ncontent-type: application/json\r\n\r\n{\"proof\":\"x\"}";
        assert_eq!(parse_response(raw), Some((200, "{\"proof\":\"x\"}".into())));
        assert_eq!(parse_response(b"garbage"), None);
        assert_eq!(parse_response(b"SSH-2.0-x\r\n\r\n"), None);
    }

    /// A one-request server that answers with `answer(nonce)`.
    fn serve_once(answer: impl FnOnce(&str) -> String + Send + 'static) -> u16 {
        let listener = TcpListener::bind("127.0.0.1:0").unwrap();
        let port = listener.local_addr().unwrap().port();
        thread::spawn(move || {
            let (mut stream, _) = listener.accept().unwrap();
            // The whole request head, however it arrives.
            let mut raw = Vec::new();
            let mut buf = [0u8; 1024];
            while !raw.windows(4).any(|w| w == b"\r\n\r\n") {
                let n = stream.read(&mut buf).unwrap();
                assert!(n > 0, "the request ended early");
                raw.extend_from_slice(&buf[..n]);
            }
            let request = String::from_utf8_lossy(&raw).to_string();
            assert!(!request.to_lowercase().contains("authorization"));
            let nonce = request
                .split("nonce=")
                .nth(1)
                .and_then(|r| r.split(' ').next())
                .unwrap_or("")
                .to_owned();
            let body = answer(&nonce);
            let response = format!(
                "HTTP/1.1 200 OK\r\ncontent-length: {}\r\ncontent-type: application/json\r\n\r\n{}",
                body.len(),
                body
            );
            stream.write_all(response.as_bytes()).unwrap();
        });
        port
    }

    #[test]
    fn asks_a_server_that_knows_the_token() {
        let port = serve_once(|nonce| format!(r#"{{"proof":"{}"}}"#, expected("secret", nonce)));
        assert_eq!(ask(port, "secret"), Proof::Proved);
    }

    #[test]
    fn a_program_without_the_token_fails() {
        let port = serve_once(|_| r#"{"status":"ok"}"#.to_owned());
        assert_eq!(ask(port, "secret"), Proof::Failed);
    }

    #[test]
    fn a_server_that_hangs_up_fails() {
        // Kept listening for the whole test: a freed port could go to another
        // test's server meanwhile.
        let listener = TcpListener::bind("127.0.0.1:0").unwrap();
        let port = listener.local_addr().unwrap().port();
        let server = thread::spawn(move || drop(listener.accept().unwrap()));
        assert_eq!(ask(port, "secret"), Proof::Failed);
        server.join().unwrap();
    }
}
