//! Controlled end-to-end tests for the serve-time catalog-sync hook.
//!
//! Each scenario points `CCP_SYNC_SCRIPT` at a tiny stub script that appends a
//! line to a marker file and exits with a configurable code, then runs the real
//! binary on a free port with `serve --no-monitor` (or `models`) and asserts the
//! hook's external behavior. The stub replaces the kit's real `sync.py`, so the
//! suite never touches the network, cargo, or the installed binary.

#![cfg(unix)]

use std::fs;
use std::io::{Read, Write};
use std::net::{SocketAddr, TcpListener, TcpStream};
use std::path::{Path, PathBuf};
use std::process::{Child, Command, Output, Stdio};
use std::thread;
use std::time::{Duration, Instant};

use tempfile::TempDir;

const BIN: &str = env!("CARGO_BIN_EXE_claude-code-proxy");

/// A stub sync script plus the marker file it appends to.
struct Stub {
    script: PathBuf,
    marker: PathBuf,
}

/// Write a Python stub that records each run and exits with `code`.
fn stub(dir: &TempDir, code: i32) -> Stub {
    let marker = dir.path().join("marker");
    let script = dir.path().join("stub_sync.py");
    let body = format!(
        "import sys\nwith open({:?}, \"a\") as handle:\n    handle.write(\"run\\n\")\nsys.exit({code})\n",
        marker.display().to_string()
    );
    fs::write(&script, body).unwrap();
    Stub { script, marker }
}

fn marker_runs(stub: &Stub) -> Vec<String> {
    match fs::read_to_string(&stub.marker) {
        Ok(content) => content.lines().map(str::to_string).collect(),
        Err(_) => Vec::new(),
    }
}

fn free_port() -> u16 {
    TcpListener::bind("127.0.0.1:0")
        .unwrap()
        .local_addr()
        .unwrap()
        .port()
}

/// A `serve` process under test, cleaned up on drop.
struct Serve {
    child: Child,
    port: u16,
}

impl Serve {
    fn spawn(stub: &Stub, repo: &Path, config: &Path, extra_env: &[(&str, &str)]) -> Serve {
        let port = free_port();
        let mut command = Command::new(BIN);
        command
            .args(["serve", "--no-monitor", "--port", &port.to_string()])
            .env("CCP_CONFIG_DIR", config)
            // Isolate the state log (`src/paths.rs` reads `XDG_STATE_HOME`) so the
            // suite never appends `service:"sync"` lines to the real proxy.log.
            .env("XDG_STATE_HOME", config)
            .env("CCP_SYNC_SCRIPT", &stub.script)
            .env("CCP_SYNC_REPO", repo)
            .env_remove("CCP_SYNC_KIT")
            .env_remove("CCP_SYNC_ON_SERVE")
            .env_remove("CCP_SYNC_JUST_EXECED")
            .stdin(Stdio::null())
            .stdout(Stdio::piped())
            .stderr(Stdio::piped());
        for (key, value) in extra_env {
            command.env(key, value);
        }
        let child = command.spawn().expect("spawn claude-code-proxy serve");
        Serve { child, port }
    }

    fn wait_until_healthy(&mut self) -> bool {
        let deadline = Instant::now() + Duration::from_secs(20);
        while Instant::now() < deadline {
            if health_ok(self.port) {
                return true;
            }
            if let Ok(Some(status)) = self.child.try_wait() {
                panic!("serve exited before becoming healthy: {status}");
            }
            thread::sleep(Duration::from_millis(50));
        }
        false
    }

    /// Kill the process and collect its output.
    fn finish(mut self) -> Output {
        let _ = self.child.kill();
        self.child.wait_with_output().expect("collect serve output")
    }
}

fn health_ok(port: u16) -> bool {
    let address = SocketAddr::from(([127, 0, 0, 1], port));
    let Ok(mut stream) = TcpStream::connect_timeout(&address, Duration::from_millis(500)) else {
        return false;
    };
    let _ = stream.set_read_timeout(Some(Duration::from_millis(500)));
    if stream
        .write_all(b"GET /healthz HTTP/1.1\r\nHost: localhost\r\nConnection: close\r\n\r\n")
        .is_err()
    {
        return false;
    }
    let mut response = String::new();
    let _ = stream.read_to_string(&mut response);
    response.starts_with("HTTP/1.1 200")
}

fn stdout_of(output: &Output) -> String {
    String::from_utf8_lossy(&output.stdout).into_owned()
}

fn stderr_of(output: &Output) -> String {
    String::from_utf8_lossy(&output.stderr).into_owned()
}

#[test]
fn no_change_serves_and_runs_script_once() {
    let dir = TempDir::new().unwrap();
    let stub = stub(&dir, 0);
    let config = TempDir::new().unwrap();

    let mut serve = Serve::spawn(&stub, dir.path(), config.path(), &[]);
    assert!(serve.wait_until_healthy(), "server must be serving");
    let output = serve.finish();

    assert_eq!(
        marker_runs(&stub),
        vec!["run"],
        "script must run exactly once"
    );
    assert!(
        stdout_of(&output).contains("ccp-sync: catalog up to date (no change)"),
        "stdout: {}",
        stdout_of(&output)
    );
}

#[test]
fn updated_reexecs_once_and_serves() {
    let dir = TempDir::new().unwrap();
    let stub = stub(&dir, 2);
    let config = TempDir::new().unwrap();

    let mut serve = Serve::spawn(&stub, dir.path(), config.path(), &[]);
    assert!(
        serve.wait_until_healthy(),
        "server must be serving after re-exec"
    );
    let output = serve.finish();

    assert_eq!(
        marker_runs(&stub),
        vec!["run"],
        "the loop guard must prevent a second run in the re-executed child"
    );
    let stdout = stdout_of(&output);
    assert!(
        stdout.contains("ccp-sync: catalog updated and installed; re-executing"),
        "stdout: {stdout}"
    );
    // The child's own summary proves the re-exec happened and skipped the check.
    assert!(
        stdout.contains("ccp-sync: skipped (already re-executed"),
        "stdout: {stdout}"
    );
}

#[test]
fn failure_serves_with_a_warning() {
    let dir = TempDir::new().unwrap();
    let stub = stub(&dir, 1);
    let config = TempDir::new().unwrap();

    let mut serve = Serve::spawn(&stub, dir.path(), config.path(), &[]);
    assert!(
        serve.wait_until_healthy(),
        "server must keep serving on failure"
    );
    let output = serve.finish();

    assert_eq!(
        marker_runs(&stub),
        vec!["run"],
        "script must run exactly once"
    );
    assert!(
        stdout_of(&output).contains("sync script exited with code 1"),
        "stdout: {}",
        stdout_of(&output)
    );
    assert!(
        stderr_of(&output).contains("serving the existing binary"),
        "stderr: {}",
        stderr_of(&output)
    );
}

#[test]
fn kill_switch_never_runs_the_script() {
    let dir = TempDir::new().unwrap();
    let stub = stub(&dir, 0);
    let config = TempDir::new().unwrap();

    let mut serve = Serve::spawn(
        &stub,
        dir.path(),
        config.path(),
        &[("CCP_SYNC_ON_SERVE", "0")],
    );
    assert!(serve.wait_until_healthy(), "server must be serving");
    let output = serve.finish();

    assert!(
        marker_runs(&stub).is_empty(),
        "kill switch must skip the script"
    );
    assert!(
        stdout_of(&output).contains("ccp-sync: skipped (disabled by CCP_SYNC_ON_SERVE=0)"),
        "stdout: {}",
        stdout_of(&output)
    );
}

#[test]
fn missing_repo_skips_with_a_log_line() {
    let dir = TempDir::new().unwrap();
    let stub = stub(&dir, 0);
    let config = TempDir::new().unwrap();

    let mut serve = Serve::spawn(
        &stub,
        Path::new("/nonexistent/claude-code-proxy"),
        config.path(),
        &[],
    );
    assert!(serve.wait_until_healthy(), "server must be serving");
    let output = serve.finish();

    assert!(
        marker_runs(&stub).is_empty(),
        "missing repo must skip the script"
    );
    assert!(
        stdout_of(&output).contains("working copy not found"),
        "stdout: {}",
        stdout_of(&output)
    );
}

#[test]
fn loop_guard_skips_the_check() {
    let dir = TempDir::new().unwrap();
    let stub = stub(&dir, 0);
    let config = TempDir::new().unwrap();

    let mut serve = Serve::spawn(
        &stub,
        dir.path(),
        config.path(),
        &[("CCP_SYNC_JUST_EXECED", "1")],
    );
    assert!(serve.wait_until_healthy(), "server must be serving");
    let output = serve.finish();

    assert!(
        marker_runs(&stub).is_empty(),
        "loop guard must skip the script"
    );
    assert!(
        stdout_of(&output).contains("ccp-sync: skipped (already re-executed"),
        "stdout: {}",
        stdout_of(&output)
    );
}

#[test]
fn models_never_runs_the_sync() {
    let dir = TempDir::new().unwrap();
    let stub = stub(&dir, 0);
    let config = TempDir::new().unwrap();

    let output = Command::new(BIN)
        .arg("models")
        .env("CCP_CONFIG_DIR", config.path())
        .env("XDG_STATE_HOME", config.path())
        .env("CCP_SYNC_SCRIPT", &stub.script)
        .env("CCP_SYNC_REPO", dir.path())
        .env_remove("CCP_SYNC_ON_SERVE")
        .env_remove("CCP_SYNC_JUST_EXECED")
        .stdin(Stdio::null())
        .output()
        .expect("run models");

    assert!(output.status.success(), "models must succeed");
    assert!(
        marker_runs(&stub).is_empty(),
        "non-serve subcommands must never run the sync"
    );
}
