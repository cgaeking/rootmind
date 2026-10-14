// The RootMind service on Linux: two systemd user units of the account that
// runs the app (service.rs). Everything here runs as that account, without
// root: the unit files in ~/.config/systemd/user, `systemctl --user show` for
// the state and main PID, /proc for the listener check, and the data folder
// ~/.local/share/rootmind (owner-only) for the token. Parsing lives in
// unitfile.rs and procnet.rs so it is tested on every platform.

use std::collections::HashSet;
use std::env;
use std::fs;
use std::io;
use std::path::{Path, PathBuf};
use std::process::{Command, Stdio};

use crate::imagepath::Role;
use crate::procnet;
use crate::service::{ScmStatus, ServiceView};
use crate::tcptable::Listener;
use crate::unitfile;

/// An XDG base folder: the variable when it holds an absolute path (the
/// spec ignores a relative one), else `$HOME/<default>`.
fn xdg(name: &str, default: &str) -> Option<PathBuf> {
    match env::var_os(name).map(PathBuf::from) {
        Some(path) if path.is_absolute() => Some(path),
        _ => env::var_os("HOME")
            .map(PathBuf::from)
            .filter(|home| home.is_absolute())
            .map(|home| home.join(default)),
    }
}

/// `$XDG_DATA_HOME/rootmind` (default `~/.local/share/rootmind`).
pub fn data_root() -> io::Result<PathBuf> {
    xdg("XDG_DATA_HOME", ".local/share")
        .map(|base| base.join("rootmind"))
        .ok_or_else(|| io::Error::new(io::ErrorKind::NotFound, "HOME is not set"))
}

/// `$XDG_CONFIG_HOME/systemd/user` (default `~/.config/systemd/user`).
pub fn unit_dir() -> io::Result<PathBuf> {
    xdg("XDG_CONFIG_HOME", ".config")
        .map(|base| base.join("systemd").join("user"))
        .ok_or_else(|| io::Error::new(io::ErrorKind::NotFound, "HOME is not set"))
}

fn systemctl() -> PathBuf {
    ["/usr/bin/systemctl", "/bin/systemctl"]
        .iter()
        .map(PathBuf::from)
        .find(|path| path.is_file())
        .unwrap_or_else(|| PathBuf::from("systemctl"))
}

/// The SCM state code (service.rs, SvcState::from_scm) of a systemd
/// ActiveState: active 4 (running), activating 2, deactivating 3, inactive
/// and failed 1 (stopped), anything else 0 (other).
pub fn scm_state(active_state: &str) -> u32 {
    match active_state {
        "active" | "reloading" => 4,
        "activating" => 2,
        "deactivating" => 3,
        "inactive" | "failed" => 1,
        _ => 0,
    }
}

/// Parse `systemctl show -p ActiveState,MainPID,LoadState` output.
pub fn parse_show(text: &str) -> Option<ScmStatus> {
    let mut active = "";
    let mut pid = 0u32;
    let mut loaded = false;
    for line in text.lines() {
        match line.split_once('=') {
            Some(("ActiveState", value)) => active = value.trim(),
            Some(("MainPID", value)) => pid = value.trim().parse().unwrap_or(0),
            Some(("LoadState", value)) => loaded = !matches!(value.trim(), "" | "not-found"),
            _ => {}
        }
    }
    loaded.then(|| ScmStatus {
        state: scm_state(active),
        pid,
    })
}

/// The unit's state and main PID, `None` when systemd does not know it.
fn query(unit: &str) -> io::Result<Option<ScmStatus>> {
    let output = Command::new(systemctl())
        .args([
            "--user",
            "--no-ask-password",
            "show",
            unit,
            "--property=LoadState,ActiveState,MainPID",
        ])
        .env("SYSTEMD_PAGER", "")
        .env("LC_ALL", "C")
        .stdin(Stdio::null())
        .stderr(Stdio::null())
        .output()?;
    if !output.status.success() {
        return Err(io::Error::other("systemctl --user show failed"));
    }
    Ok(parse_show(&String::from_utf8_lossy(&output.stdout)))
}

/// What the app learns about one unit: ours when its file follows the
/// contract and runs the program copy in our data folder.
pub fn inspect(role: Role) -> ServiceView {
    let name = unitfile::unit_name(role);
    let text = unit_dir()
        .ok()
        .and_then(|dir| fs::read_to_string(dir.join(name)).ok());
    let status = if text.is_some() {
        query(name).ok().flatten()
    } else {
        None
    };
    let root = data_root().ok();
    ServiceView::from_unit(text.as_deref(), status, role, root.as_deref())
}

/// The socket inodes `pid` holds open (`socket:[<inode>]` in /proc/<pid>/fd).
fn socket_inodes(pid: u32) -> io::Result<HashSet<u64>> {
    let mut inodes = HashSet::new();
    for entry in fs::read_dir(Path::new("/proc").join(pid.to_string()).join("fd"))? {
        let Ok(entry) = entry else { continue };
        if let Ok(target) = fs::read_link(entry.path()) {
            if let Some(inode) = target.to_str().and_then(procnet::socket_inode) {
                inodes.insert(inode);
            }
        }
    }
    Ok(inodes)
}

/// The listeners that receive 127.0.0.1, with `pid` on the sockets `pid`
/// holds and PID 0 on every other (another process, or another account's).
pub fn listeners_for(pid: u32) -> io::Result<Vec<Listener>> {
    let mut sockets = procnet::parse(&fs::read_to_string("/proc/net/tcp")?);
    match fs::read_to_string("/proc/net/tcp6") {
        Ok(text) => sockets.extend(procnet::parse(&text)),
        Err(e) if e.kind() == io::ErrorKind::NotFound => {}
        Err(e) => return Err(e),
    }
    let inodes = if pid == 0 {
        HashSet::new()
    } else {
        socket_inodes(pid)?
    };
    Ok(procnet::listeners(&sockets, &inodes, pid))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn states_map_to_scm_codes() {
        assert_eq!(scm_state("active"), 4);
        assert_eq!(scm_state("activating"), 2);
        assert_eq!(scm_state("deactivating"), 3);
        assert_eq!(scm_state("failed"), 1);
        assert_eq!(scm_state("inactive"), 1);
        assert_eq!(scm_state("maintenance"), 0);
    }

    #[test]
    fn show_output() {
        let running = parse_show("LoadState=loaded\nActiveState=active\nMainPID=4242\n").unwrap();
        assert_eq!(
            running,
            ScmStatus {
                state: 4,
                pid: 4242
            }
        );
        assert_eq!(
            parse_show("LoadState=not-found\nActiveState=inactive\nMainPID=0\n"),
            None
        );
        assert_eq!(parse_show(""), None);
    }

    #[test]
    fn this_process_owns_its_listening_socket() {
        let listener = std::net::TcpListener::bind("127.0.0.1:0").unwrap();
        let port = listener.local_addr().unwrap().port();
        let pid = std::process::id();
        let table = listeners_for(pid).unwrap();
        assert!(crate::tcptable::port_owned_by(&table, port, pid));
        drop(listener);
    }
}
