//! A stand-in for rootmind-backend.exe, used by the lifecycle tests in
//! src/backend.rs (built as the `fake_backend` example).
//!
//! It records its arguments and environment in `fake-launch.txt` in its
//! working directory (and appends them to `fake-launches.txt`, one record per
//! run), binds a free loopback port, prints the ready line, and
//! exits on a `shutdown` line or when stdin closes. A `fake-mode` file in the
//! working directory changes that:
//! - `exit-early`: exit with code 3 before the ready line;
//! - `ignore-shutdown`: keep running until killed;
//! - `crash-once`: on the first run, exit with code 1 right after the ready
//!   line (and remember that in `fake-crashed`).

use std::fs;
use std::io::{self, BufRead, Write};
use std::net::TcpListener;
use std::process::exit;
use std::thread::sleep;
use std::time::Duration;

fn main() {
    let args: Vec<String> = std::env::args().skip(1).collect();
    let cwd = std::env::current_dir().expect("working directory");
    let home = std::env::var("RLM_HOME").unwrap_or_default();
    let token = std::env::var("RLM_API_TOKEN").unwrap_or_default();
    let dev = std::env::var("RLM_DEV").unwrap_or_default();
    let config = std::env::var("CONFIG_PATH").unwrap_or_else(|_| "<unset>".into());
    let no_index = std::env::var("RLM_DISABLE_SCHEDULER").unwrap_or_else(|_| "<unset>".into());
    let record = format!(
        "args={}\ncwd={}\nhome={home}\ntoken={token}\ndev={dev}\nconfig_path={config}\n\
         disable_scheduler={no_index}\n",
        args.join(" "),
        cwd.display()
    );
    fs::write("fake-launch.txt", &record).expect("write fake-launch.txt");
    // Every run, for tests that start the backend more than once.
    let mut all = fs::OpenOptions::new()
        .create(true)
        .append(true)
        .open("fake-launches.txt")
        .expect("open fake-launches.txt");
    all.write_all(record.as_bytes())
        .expect("write fake-launches.txt");

    let mode = fs::read_to_string("fake-mode").unwrap_or_default();
    let mode = mode.trim();
    if mode == "exit-early" {
        eprintln!("fake backend: refusing to start");
        exit(3);
    }

    let listener = TcpListener::bind("127.0.0.1:0").expect("bind");
    let port = listener.local_addr().expect("address").port();
    println!("INFO: fake backend starting");
    println!("ROOTMIND_LISTENING {{\"port\":{port}}}");
    io::stdout().flush().expect("flush");

    if mode == "crash-once" && fs::metadata("fake-crashed").is_err() {
        fs::write("fake-crashed", b"1").expect("write fake-crashed");
        exit(1);
    }

    for line in io::stdin().lock().lines() {
        match line {
            Ok(line) if line.trim() == "shutdown" && mode != "ignore-shutdown" => break,
            Ok(_) => {}
            Err(_) => break,
        }
    }
    if mode == "ignore-shutdown" {
        loop {
            sleep(Duration::from_secs(1));
        }
    }
    eprintln!("fake backend: stopped");
}
