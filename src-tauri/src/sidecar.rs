use serde::{Deserialize, Serialize};
use std::collections::VecDeque;
use std::ffi::OsStr;
use std::fmt;
use std::fs;
use std::io::{BufRead, BufReader, Read, Write};
use std::net::{IpAddr, Ipv4Addr, SocketAddr, TcpStream};
use std::path::{Path, PathBuf};
use std::process::{Child, ChildStdin, Command, ExitStatus, Stdio};
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{mpsc, Arc, Condvar, Mutex};
use std::thread;
use std::time::{Duration, Instant};

#[cfg(unix)]
use std::os::unix::fs::PermissionsExt;
#[cfg(unix)]
use std::os::unix::process::CommandExt;

#[cfg(windows)]
use std::os::windows::process::CommandExt;

pub const SIDECAR_PROTOCOL_VERSION: u32 = 1;
pub const FORBIDDEN_DEV_PORT: u16 = 8008;
pub const STARTUP_TIMEOUT: Duration = Duration::from_secs(60);
pub const GRACEFUL_SHUTDOWN_TIMEOUT: Duration = Duration::from_secs(8);
pub const FALLBACK_TERM_TIMEOUT: Duration = Duration::from_secs(2);
const DIAGNOSTIC_TAIL_LIMIT: usize = 40;
const HEALTH_BODY_MAX_BYTES: usize = 64 * 1024;

#[derive(Debug, Clone, PartialEq, Eq, Deserialize, Serialize)]
pub struct SidecarHandshake {
    pub protocol_version: u32,
    pub status: String,
    #[serde(default)]
    pub host: Option<String>,
    pub port: u16,
    pub pid: u32,
    pub launch_token: String,
    #[serde(default)]
    pub app_version: Option<String>,
    #[serde(default)]
    pub app_mode: Option<String>,
    #[serde(default)]
    pub data_dir: Option<String>,
    #[serde(default)]
    pub db_path: Option<String>,
    #[serde(default)]
    pub log_file: Option<String>,
    #[serde(default)]
    pub log_dir: Option<String>,
}

#[derive(Debug, Clone)]
pub enum SidecarError {
    BinaryMissing(String),
    SingleInstanceActive(String),
    StartupFailed(String),
    Timeout(String),
}

impl SidecarError {
    pub fn user_message(&self) -> &str {
        match self {
            Self::BinaryMissing(msg)
            | Self::SingleInstanceActive(msg)
            | Self::StartupFailed(msg)
            | Self::Timeout(msg) => msg.as_str(),
        }
    }

    pub fn is_single_instance(&self) -> bool {
        matches!(self, Self::SingleInstanceActive(_))
    }
}

impl fmt::Display for SidecarError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str(self.user_message())
    }
}

impl std::error::Error for SidecarError {}

fn expected_sidecar_filename() -> &'static str {
    if cfg!(windows) {
        "krushidhan-sidecar.exe"
    } else {
        "krushidhan-sidecar"
    }
}

fn validate_sidecar_binary(path: &Path) -> Result<(), String> {
    let meta = fs::symlink_metadata(path)
        .map_err(|err| format!("Unable to inspect sidecar binary at {}: {err}", path.display()))?;
    if meta.file_type().is_symlink() {
        return Err(format!(
            "Refusing to execute symlinked sidecar binary at {}",
            path.display()
        ));
    }
    if !meta.file_type().is_file() {
        return Err(format!(
            "Sidecar path is not a regular file: {}",
            path.display()
        ));
    }
    #[cfg(unix)]
    {
        let mode = meta.permissions().mode();
        if mode & 0o111 == 0 {
            return Err(format!(
                "Bundled sidecar binary lacks execute permissions: {}",
                path.display()
            ));
        }
    }
    Ok(())
}

/// Resolve the bundled `krushidhan-sidecar` binary adjacent to the current executable.
///
/// Inside a macOS `.app` bundle, both executables live under `<App>.app/Contents/MacOS/`.
/// During `cargo test`, test binaries reside under `target/<profile>/deps/` while `tauri-build`
/// copies `externalBin` into `target/<profile>/krushidhan-sidecar`.
pub fn resolve_sidecar_binary() -> Result<PathBuf, SidecarError> {
    let current_exe = std::env::current_exe().map_err(|err| {
        SidecarError::BinaryMissing(format!("Could not locate desktop executable path: {err}"))
    })?;
    let exe_dir = current_exe.parent().ok_or_else(|| {
        SidecarError::BinaryMissing("Could not determine desktop executable directory.".into())
    })?;

    let sidecar_name = expected_sidecar_filename();
    let mut candidates = vec![exe_dir.join(sidecar_name)];
    if exe_dir.file_name() == Some(OsStr::new("deps")) {
        if let Some(profile_dir) = exe_dir.parent() {
            candidates.push(profile_dir.join(sidecar_name));
        }
    }

    for candidate in &candidates {
        if candidate.exists() {
            validate_sidecar_binary(candidate).map_err(SidecarError::BinaryMissing)?;
            return Ok(candidate.clone());
        }
    }

    Err(SidecarError::BinaryMissing(format!(
        "The packaged Krushidhan ERP backend sidecar ('{sidecar_name}') was not found next to the application executable.\n\nExpected location: {}",
        candidates[0].display()
    )))
}

pub fn redact_sensitive_text(raw: &str, known_token: Option<&str>) -> String {
    let mut cleaned = raw.trim_end_matches(&['\r', '\n'][..]).to_string();
    if let Some(tok) = known_token {
        let trimmed_tok = tok.trim();
        if !trimmed_tok.is_empty() {
            cleaned = cleaned.replace(trimmed_tok, "[REDACTED]");
        }
    }
    if let Some(key_pos) = cleaned.find("\"launch_token\"") {
        let prefix = &cleaned[..key_pos];
        let after_key = &cleaned[key_pos + "\"launch_token\"".len()..];
        if let Some(colon_pos) = after_key.find(':') {
            let after_colon = &after_key[colon_pos + 1..];
            if let Some(q1) = after_colon.find('"') {
                let value_rest = &after_colon[q1 + 1..];
                if let Some(q2) = value_rest.find('"') {
                    let tail = &value_rest[q2 + 1..];
                    cleaned = format!("{prefix}\"launch_token\": \"[REDACTED]\"{tail}");
                }
            }
        }
    }
    // Redact any KDHN-... Recovery Key tokens if ever echoed in backend stderr/stdout
    while let Some(rk_pos) = cleaned.to_ascii_uppercase().find("KDHN-") {
        let mut end = rk_pos + 5;
        let bytes = cleaned.as_bytes();
        while end < bytes.len()
            && (bytes[end].is_ascii_alphanumeric() || bytes[end] == b'-' || bytes[end] == b'_')
        {
            end += 1;
        }
        if end - rk_pos >= 15 {
            cleaned.replace_range(rk_pos..end, "[REDACTED_RECOVERY_KEY]");
        } else {
            break;
        }
    }
    cleaned
}

pub fn parse_and_validate_handshake(line: &str) -> Result<SidecarHandshake, String> {
    let trimmed = line.trim();
    if trimmed.is_empty() {
        return Err("Sidecar emitted an empty readiness line.".into());
    }
    let handshake: SidecarHandshake = serde_json::from_str(trimmed)
        .map_err(|err| format!("Sidecar emitted invalid readiness JSON: {err}"))?;

    if handshake.protocol_version != SIDECAR_PROTOCOL_VERSION {
        return Err(format!(
            "Unsupported sidecar protocol version {} (expected {}).",
            handshake.protocol_version, SIDECAR_PROTOCOL_VERSION
        ));
    }
    if handshake.status != "ready" {
        return Err(format!(
            "Unexpected sidecar readiness status '{}' (expected 'ready').",
            handshake.status
        ));
    }
    if let Some(ref host) = handshake.host {
        if host != "127.0.0.1" {
            return Err(format!(
                "Sidecar reported non-loopback host '{host}' (expected '127.0.0.1')."
            ));
        }
    }
    if handshake.port == 0 {
        return Err("Sidecar reported invalid TCP port 0.".into());
    }
    if handshake.port == FORBIDDEN_DEV_PORT {
        return Err(format!(
            "Sidecar bound forbidden shared dev port {FORBIDDEN_DEV_PORT}; expected an isolated ephemeral port."
        ));
    }
    if handshake.pid == 0 {
        return Err("Sidecar reported invalid process id 0.".into());
    }
    if handshake.launch_token.len() < 32
        || handshake.launch_token.chars().any(|c| c.is_whitespace())
    {
        return Err("Sidecar launch token failed minimum length/format validation.".into());
    }

    Ok(handshake)
}

fn push_diagnostic_line(
    tail: &Arc<Mutex<VecDeque<String>>>,
    known_token: Option<&str>,
    line: &str,
) {
    let redacted = redact_sensitive_text(line, known_token);
    if redacted.is_empty() {
        return;
    }
    let mut guard = tail.lock().unwrap_or_else(|e| e.into_inner());
    if guard.len() >= DIAGNOSTIC_TAIL_LIMIT {
        guard.pop_front();
    }
    guard.push_back(redacted);
}

fn format_diagnostic_tail(tail: &Arc<Mutex<VecDeque<String>>>) -> String {
    let guard = tail.lock().unwrap_or_else(|e| e.into_inner());
    if guard.is_empty() {
        return String::new();
    }
    let lines: Vec<&str> = guard.iter().map(String::as_str).collect();
    let start = lines.len().saturating_sub(8);
    lines[start..].join("\n")
}

#[derive(Debug)]
struct SidecarInner {
    child: Option<Child>,
    stdin: Option<ChildStdin>,
    outer_pgid: u32,
    inner_pid: Option<u32>,
    handshake_ready: bool,
    shutting_down: bool,
    shutdown_done: bool,
}

pub struct SidecarController {
    inner: Mutex<SidecarInner>,
    cvar: Condvar,
    known_token: Mutex<Option<String>>,
    diag_tail: Arc<Mutex<VecDeque<String>>>,
    shutting_down_flag: AtomicBool,
}

impl SidecarController {
    fn new(child: Child, stdin: Option<ChildStdin>, outer_pgid: u32) -> Arc<Self> {
        Arc::new(Self {
            inner: Mutex::new(SidecarInner {
                child: Some(child),
                stdin,
                outer_pgid,
                inner_pid: None,
                handshake_ready: false,
                shutting_down: false,
                shutdown_done: false,
            }),
            cvar: Condvar::new(),
            known_token: Mutex::new(None),
            diag_tail: Arc::new(Mutex::new(VecDeque::with_capacity(DIAGNOSTIC_TAIL_LIMIT))),
            shutting_down_flag: AtomicBool::new(false),
        })
    }

    pub fn is_shutting_down(&self) -> bool {
        self.shutting_down_flag.load(Ordering::SeqCst)
    }

    fn set_handshake_info(&self, handshake: &SidecarHandshake) {
        if let Ok(mut tok_guard) = self.known_token.lock() {
            *tok_guard = Some(handshake.launch_token.clone());
        }
        let mut guard = self.inner.lock().unwrap_or_else(|e| e.into_inner());
        guard.inner_pid = Some(handshake.pid);
        guard.handshake_ready = true;
    }

    fn current_known_token(&self) -> Option<String> {
        self.known_token
            .lock()
            .ok()
            .and_then(|g| g.clone())
    }

    fn try_wait_exited(&self) -> Option<Result<ExitStatus, String>> {
        let mut guard = self.inner.lock().unwrap_or_else(|e| e.into_inner());
        let child = guard.child.as_mut()?;
        match child.try_wait() {
            Ok(Some(status)) => {
                guard.child = None;
                guard.stdin = None;
                guard.shutdown_done = true;
                self.cvar.notify_all();
                Some(Ok(status))
            }
            Ok(None) => None,
            Err(err) => Some(Err(err.to_string())),
        }
    }

    pub fn diagnostic_tail(&self) -> String {
        format_diagnostic_tail(&self.diag_tail)
    }

    /// Gracefully stop the sidecar:
    /// 1. Close stdin pipe so Python backend receives EOF, checkpoints SQLite WAL, and exits cleanly
    /// 2. Wait up to `graceful_timeout` for outer PyInstaller bootloader + inner server exit
    /// 3. Fall back to POSIX process-group SIGTERM -> wait -> SIGKILL ONLY if graceful timeout expires
    pub fn shutdown_blocking(&self, graceful_timeout: Duration) -> Option<ExitStatus> {
        self.shutting_down_flag.store(true, Ordering::SeqCst);

        let (stdin_to_close, outer_pgid, inner_pid) = {
            let mut guard = self.inner.lock().unwrap_or_else(|e| e.into_inner());
            if guard.shutdown_done {
                return None;
            }
            if guard.shutting_down {
                while !guard.shutdown_done {
                    guard = self.cvar.wait(guard).unwrap_or_else(|e| e.into_inner());
                }
                return None;
            }
            guard.shutting_down = true;
            let stdin = guard.stdin.take();
            (stdin, guard.outer_pgid, guard.inner_pid)
        };

        // Close stdin write pipe outside mutex so sidecar reads EOF immediately.
        drop(stdin_to_close);

        let mut final_status = self.wait_for_exit_within(graceful_timeout);

        if final_status.is_none() {
            // Escalate ONLY after graceful timeout expires.
            terminate_process_group(outer_pgid, inner_pid, &mut || {
                self.wait_for_exit_within(Duration::from_millis(200))
            });
            // On non-Unix (Windows), ensure the direct Child handle is forcefully terminated too
            // so wait never blocks indefinitely.
            #[cfg(not(unix))]
            {
                let mut guard = self.inner.lock().unwrap_or_else(|e| e.into_inner());
                if let Some(child) = guard.child.as_mut() {
                    let _ = child.kill();
                }
            }
            final_status = self
                .wait_for_exit_within(Duration::from_secs(3))
                .or_else(|| self.wait_for_exit_blocking());
        } else {
            // Even when outer PyInstaller bootloader exits cleanly, verify no inner descendant lingered.
            ensure_inner_pid_reaped(outer_pgid, inner_pid);
        }

        let mut guard = self.inner.lock().unwrap_or_else(|e| e.into_inner());
        guard.child = None;
        guard.stdin = None;
        guard.shutdown_done = true;
        self.cvar.notify_all();
        final_status
    }

    fn wait_for_exit_within(&self, timeout: Duration) -> Option<ExitStatus> {
        let deadline = Instant::now() + timeout;
        loop {
            {
                let mut guard = self.inner.lock().unwrap_or_else(|e| e.into_inner());
                let Some(child) = guard.child.as_mut() else {
                    return None;
                };
                match child.try_wait() {
                    Ok(Some(status)) => {
                        guard.child = None;
                        return Some(status);
                    }
                    Ok(None) => {}
                    Err(_) => {
                        guard.child = None;
                        return None;
                    }
                }
            }
            if Instant::now() >= deadline {
                return None;
            }
            thread::sleep(Duration::from_millis(40));
        }
    }

    fn wait_for_exit_blocking(&self) -> Option<ExitStatus> {
        let mut child_opt = {
            let mut guard = self.inner.lock().unwrap_or_else(|e| e.into_inner());
            guard.child.take()
        };
        child_opt.as_mut().and_then(|c| c.wait().ok())
    }
}

#[cfg(unix)]
fn is_pid_alive_in_pgid(pid: u32, pgid: u32) -> bool {
    if pid == 0 || pgid == 0 {
        return false;
    }
    let pid_i32 = pid as libc::pid_t;
    let pgid_i32 = pgid as libc::pid_t;
    let alive = unsafe { libc::kill(pid_i32, 0) } == 0;
    if !alive {
        return false;
    }
    let actual_pgid = unsafe { libc::getpgid(pid_i32) };
    actual_pgid == pgid_i32
}

#[cfg(unix)]
fn send_sig_to_pgid(pgid: u32, sig: libc::c_int) {
    if pgid == 0 {
        return;
    }
    let pgid_i32 = pgid as libc::pid_t;
    unsafe {
        let _ = libc::killpg(pgid_i32, sig);
    }
}

#[cfg(unix)]
fn terminate_process_group(
    outer_pgid: u32,
    inner_pid: Option<u32>,
    poll_once: &mut dyn FnMut() -> Option<ExitStatus>,
) {
    send_sig_to_pgid(outer_pgid, libc::SIGTERM);
    if let Some(ip) = inner_pid {
        if ip != outer_pgid && is_pid_alive_in_pgid(ip, outer_pgid) {
            unsafe {
                let _ = libc::kill(ip as libc::pid_t, libc::SIGTERM);
            }
        }
    }

    let term_deadline = Instant::now() + FALLBACK_TERM_TIMEOUT;
    while Instant::now() < term_deadline {
        if poll_once().is_some() {
            return;
        }
    }

    send_sig_to_pgid(outer_pgid, libc::SIGKILL);
    if let Some(ip) = inner_pid {
        if ip != outer_pgid && is_pid_alive_in_pgid(ip, outer_pgid) {
            unsafe {
                let _ = libc::kill(ip as libc::pid_t, libc::SIGKILL);
            }
        }
    }
}

#[cfg(windows)]
fn run_windows_taskkill_tree(pid: u32) {
    if pid == 0 {
        return;
    }
    const CREATE_NO_WINDOW: u32 = 0x0800_0000;
    let mut cmd = Command::new("taskkill.exe");
    cmd.args(["/PID", &pid.to_string(), "/T", "/F"])
        .stdin(Stdio::null())
        .stdout(Stdio::null())
        .stderr(Stdio::null())
        .creation_flags(CREATE_NO_WINDOW);
    let _ = cmd.status();
}

#[cfg(windows)]
fn terminate_process_group(
    outer_pgid: u32,
    inner_pid: Option<u32>,
    poll_once: &mut dyn FnMut() -> Option<ExitStatus>,
) {
    // Terminate entire PyInstaller onefile process tree (outer bootloader + inner Python server)
    // without flashing a Windows console window.
    run_windows_taskkill_tree(outer_pgid);
    if let Some(ip) = inner_pid {
        if ip != outer_pgid {
            run_windows_taskkill_tree(ip);
        }
    }
    let term_deadline = Instant::now() + FALLBACK_TERM_TIMEOUT;
    while Instant::now() < term_deadline {
        if poll_once().is_some() {
            return;
        }
    }
}

#[cfg(not(any(unix, windows)))]
fn terminate_process_group(
    _outer_pgid: u32,
    _inner_pid: Option<u32>,
    _poll_once: &mut dyn FnMut() -> Option<ExitStatus>,
) {
}

#[cfg(unix)]
fn ensure_inner_pid_reaped(outer_pgid: u32, inner_pid: Option<u32>) {
    let Some(ip) = inner_pid else {
        return;
    };
    if ip == outer_pgid {
        return;
    }
    let wait_deadline = Instant::now() + Duration::from_millis(600);
    while Instant::now() < wait_deadline {
        if !is_pid_alive_in_pgid(ip, outer_pgid) {
            return;
        }
        thread::sleep(Duration::from_millis(30));
    }
    if is_pid_alive_in_pgid(ip, outer_pgid) {
        send_sig_to_pgid(outer_pgid, libc::SIGTERM);
        thread::sleep(Duration::from_millis(300));
        if is_pid_alive_in_pgid(ip, outer_pgid) {
            send_sig_to_pgid(outer_pgid, libc::SIGKILL);
        }
    }
}

#[cfg(windows)]
fn ensure_inner_pid_reaped(outer_pgid: u32, inner_pid: Option<u32>) {
    let Some(ip) = inner_pid else {
        return;
    };
    if ip == 0 || ip == outer_pgid {
        return;
    }
    // Give PyInstaller onefile's inner Python server a brief grace window to finish
    // closing files after outer bootloader exit, then ensure no orphan remains.
    thread::sleep(Duration::from_millis(250));
    run_windows_taskkill_tree(ip);
}

#[cfg(not(any(unix, windows)))]
fn ensure_inner_pid_reaped(_outer_pgid: u32, _inner_pid: Option<u32>) {}

#[cfg(test)]
pub fn spawn_and_verify_command(
    cmd: Command,
    timeout: Duration,
) -> Result<(Arc<SidecarController>, SidecarHandshake), SidecarError> {
    spawn_and_verify_with_registration(cmd, timeout, |_| {})
}

pub fn spawn_and_verify_with_registration<F>(
    mut cmd: Command,
    timeout: Duration,
    on_spawned: F,
) -> Result<(Arc<SidecarController>, SidecarHandshake), SidecarError>
where
    F: FnOnce(Arc<SidecarController>),
{
    cmd.stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped());

    #[cfg(unix)]
    cmd.process_group(0);

    #[cfg(windows)]
    {
        const CREATE_NO_WINDOW: u32 = 0x0800_0000;
        cmd.creation_flags(CREATE_NO_WINDOW);
    }

    let mut child = cmd.spawn().map_err(|err| {
        SidecarError::StartupFailed(format!(
            "Failed to start local Krushidhan ERP backend service: {err}"
        ))
    })?;

    let outer_pgid = child.id();
    let stdin = child.stdin.take();
    let stdout = child.stdout.take().ok_or_else(|| {
        SidecarError::StartupFailed("Sidecar stdout pipe was not captured.".into())
    })?;
    let stderr = child.stderr.take().ok_or_else(|| {
        SidecarError::StartupFailed("Sidecar stderr pipe was not captured.".into())
    })?;

    let controller = SidecarController::new(child, stdin, outer_pgid);
    on_spawned(Arc::clone(&controller));

    let diag_tail_err = Arc::clone(&controller.diag_tail);
    let ctrl_for_stderr = Arc::clone(&controller);
    thread::Builder::new()
        .name("krushidhan-sidecar-stderr".into())
        .spawn(move || {
            let reader = BufReader::new(stderr);
            for line_res in reader.lines() {
                match line_res {
                    Ok(line) => {
                        let tok = ctrl_for_stderr.current_known_token();
                        push_diagnostic_line(&diag_tail_err, tok.as_deref(), &line);
                    }
                    Err(_) => break,
                }
            }
        })
        .ok();

    let (ready_tx, ready_rx) = mpsc::channel::<Result<SidecarHandshake, String>>();
    let diag_tail_out = Arc::clone(&controller.diag_tail);
    let ctrl_for_stdout = Arc::clone(&controller);
    thread::Builder::new()
        .name("krushidhan-sidecar-stdout".into())
        .spawn(move || {
            let reader = BufReader::new(stdout);
            let mut sender = Some(ready_tx);
            let mut preamble_lines = 0usize;

            for line_res in reader.lines() {
                match line_res {
                    Ok(line) => {
                        let tok = ctrl_for_stdout.current_known_token();
                        if let Some(tx) = sender.take() {
                            let trimmed = line.trim();
                            if trimmed.is_empty() {
                                sender = Some(tx);
                                continue;
                            }
                            if !trimmed.starts_with('{') {
                                push_diagnostic_line(&diag_tail_out, tok.as_deref(), &line);
                                preamble_lines += 1;
                                if preamble_lines > 12 {
                                    let _ = tx.send(Err(
                                        "Sidecar emitted unexpected non-JSON output without readiness handshake.".into(),
                                    ));
                                } else {
                                    sender = Some(tx);
                                }
                                continue;
                            }
                            let _ = tx.send(parse_and_validate_handshake(trimmed));
                        } else {
                            // Keep draining remaining stdout so OS pipe never fills and stalls child.
                            push_diagnostic_line(&diag_tail_out, tok.as_deref(), &line);
                        }
                    }
                    Err(err) => {
                        if let Some(tx) = sender.take() {
                            let _ = tx.send(Err(format!("Error reading sidecar stdout: {err}")));
                        }
                        break;
                    }
                }
            }

            if let Some(tx) = sender.take() {
                let _ = tx.send(Err(
                    "Sidecar exited before sending readiness handshake.".into(),
                ));
            }
        })
        .map_err(|err| {
            controller.shutdown_blocking(Duration::from_secs(2));
            SidecarError::StartupFailed(format!(
                "Failed to spawn sidecar stdout monitor thread: {err}"
            ))
        })?;

    let deadline = Instant::now() + timeout;
    let handshake = loop {
        if controller.is_shutting_down() {
            return Err(SidecarError::StartupFailed(
                "Application shutdown requested during startup.".into(),
            ));
        }

        let remaining = deadline.saturating_duration_since(Instant::now());
        if remaining.is_zero() {
            controller.shutdown_blocking(Duration::from_secs(2));
            let tail = controller.diagnostic_tail();
            let detail = if tail.is_empty() {
                String::new()
            } else {
                format!("\n\nRecent backend diagnostics:\n{tail}")
            };
            return Err(SidecarError::Timeout(format!(
                "Krushidhan ERP backend did not become ready within {} seconds.{detail}",
                timeout.as_secs()
            )));
        }

        match ready_rx.recv_timeout(remaining.min(Duration::from_millis(120))) {
            Ok(Ok(hs)) => break hs,
            Ok(Err(reason)) => {
                // Brief wait so child.try_wait captures exact exit code cleanly if EOF just raced exit.
                let exit_status = controller
                    .wait_for_exit_within(Duration::from_millis(1500))
                    .or_else(|| controller.wait_for_exit_blocking());
                controller.shutdown_blocking(Duration::from_secs(2));
                return Err(classify_early_failure(
                    &reason,
                    exit_status,
                    &controller.diagnostic_tail(),
                ));
            }
            Err(mpsc::RecvTimeoutError::Timeout) => {
                if let Some(res) = controller.try_wait_exited() {
                    let tail = controller.diagnostic_tail();
                    controller.shutdown_blocking(Duration::from_secs(2));
                    let status = res.ok();
                    return Err(classify_early_failure(
                        "Sidecar terminated unexpectedly during startup.",
                        status,
                        &tail,
                    ));
                }
            }
            Err(mpsc::RecvTimeoutError::Disconnected) => {
                let exit_status = controller.wait_for_exit_within(Duration::from_millis(1500));
                controller.shutdown_blocking(Duration::from_secs(2));
                return Err(classify_early_failure(
                    "Sidecar output channel disconnected unexpectedly.",
                    exit_status,
                    &controller.diagnostic_tail(),
                ));
            }
        }
    };

    controller.set_handshake_info(&handshake);

    let health_deadline = Instant::now() + timeout.min(Duration::from_secs(15)).max(Duration::from_secs(3));
    if let Err(reason) = verify_sidecar_health(
        handshake.port,
        &handshake.launch_token,
        health_deadline.min(deadline),
        &controller,
    ) {
        let tail = controller.diagnostic_tail();
        controller.shutdown_blocking(Duration::from_secs(2));
        let detail = if tail.is_empty() {
            String::new()
        } else {
            format!("\n\nRecent backend diagnostics:\n{tail}")
        };
        return Err(SidecarError::StartupFailed(format!(
            "Krushidhan ERP backend failed local health check: {reason}{detail}"
        )));
    }

    Ok((controller, handshake))
}

fn classify_early_failure(reason: &str, status: Option<ExitStatus>, diag_tail: &str) -> SidecarError {
    if let Some(st) = status {
        if st.code() == Some(2) {
            return SidecarError::SingleInstanceActive(
                "Krushidhan ERP is already running on this computer.\n\nOnly one desktop instance can use the local shop database at a time. Please switch to the open Krushidhan ERP window or close it before launching again.".into(),
            );
        }
    }

    let exit_desc = match status.and_then(|s| s.code()) {
        Some(code) => format!(" (exit code {code})"),
        None => String::new(),
    };
    let tail_section = if diag_tail.trim().is_empty() {
        String::new()
    } else {
        format!("\n\nRecent backend diagnostics:\n{diag_tail}")
    };
    SidecarError::StartupFailed(format!("{reason}{exit_desc}{tail_section}"))
}

pub fn verify_sidecar_health(
    port: u16,
    launch_token: &str,
    deadline: Instant,
    controller: &SidecarController,
) -> Result<(), String> {
    let addr = SocketAddr::new(IpAddr::V4(Ipv4Addr::LOCALHOST), port);
    let mut last_error = "Health probe did not run.".to_string();

    while Instant::now() < deadline {
        if controller.is_shutting_down() {
            return Err("Shutdown requested during health check.".into());
        }
        if let Some(res) = controller.try_wait_exited() {
            let code_note = match res.ok().and_then(|s| s.code()) {
                Some(code) => format!("exit code {code}"),
                None => "terminated".into(),
            };
            return Err(format!("Sidecar process exited before passing health check ({code_note})."));
        }

        match probe_http_health_once(addr, port, launch_token) {
            Ok(()) => return Ok(()),
            Err(err) => {
                last_error = err;
                thread::sleep(Duration::from_millis(100));
            }
        }
    }

    Err(last_error)
}

fn probe_http_health_once(addr: SocketAddr, port: u16, launch_token: &str) -> Result<(), String> {
    let mut stream = TcpStream::connect_timeout(&addr, Duration::from_secs(2))
        .map_err(|err| format!("Unable to reach loopback health port {port}: {err}"))?;
    let _ = stream.set_read_timeout(Some(Duration::from_secs(3)));
    let _ = stream.set_write_timeout(Some(Duration::from_secs(3)));

    let req = format!(
        "GET /api/health HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\nX-Krushidhan-Launch-Token: {launch_token}\r\nConnection: close\r\n\r\n"
    );
    stream
        .write_all(req.as_bytes())
        .map_err(|err| format!("Failed writing health probe request: {err}"))?;

    let mut raw_resp = Vec::with_capacity(1024);
    let mut buf = [0u8; 2048];
    loop {
        match stream.read(&mut buf) {
            Ok(0) => break,
            Ok(n) => {
                if raw_resp.len() + n > HEALTH_BODY_MAX_BYTES {
                    return Err("Health response exceeded safe byte limit.".into());
                }
                raw_resp.extend_from_slice(&buf[..n]);
            }
            Err(err) => {
                if !raw_resp.is_empty() {
                    break;
                }
                return Err(format!("Failed reading health probe response: {err}"));
            }
        }
    }

    let text = String::from_utf8_lossy(&raw_resp);
    let Some((headers_part, raw_body)) = text.split_once("\r\n\r\n") else {
        return Err("Malformed HTTP response from /api/health.".into());
    };
    let status_line = headers_part.lines().next().unwrap_or_default();
    if !(status_line.starts_with("HTTP/1.1 200") || status_line.starts_with("HTTP/1.0 200")) {
        return Err(format!("/api/health returned unexpected status line: {status_line}"));
    }

    let headers_lower = headers_part.to_ascii_lowercase();
    let body_text = if headers_lower.contains("transfer-encoding: chunked") {
        decode_chunked_body(raw_body)?
    } else {
        raw_body.trim().to_string()
    };

    let parsed: serde_json::Value = serde_json::from_str(body_text.trim())
        .map_err(|err| format!("Invalid JSON payload from /api/health: {err}"))?;

    if parsed.get("status").and_then(|v| v.as_str()) != Some("ok") {
        return Err(format!("Unexpected /api/health status payload: {parsed}"));
    }
    Ok(())
}

fn decode_chunked_body(raw: &str) -> Result<String, String> {
    let mut remaining = raw;
    let mut out = String::new();
    loop {
        let trimmed = remaining.trim_start_matches(&['\r', '\n'][..]);
        let Some((size_hex, rest)) = trimmed.split_once("\r\n") else {
            return Err("Invalid chunked HTTP encoding in health response.".into());
        };
        let size_token = size_hex.split(';').next().unwrap_or("").trim();
        let chunk_len = usize::from_str_radix(size_token, 16)
            .map_err(|_| "Invalid chunk size in health response.".to_string())?;
        if chunk_len == 0 {
            break;
        }
        if rest.len() < chunk_len {
            return Err("Truncated chunked HTTP body in health response.".into());
        }
        out.push_str(&rest[..chunk_len]);
        remaining = &rest[chunk_len..];
    }
    Ok(out)
}

/// Spawn a lightweight background monitor thread that detects unexpected sidecar exits
/// while the desktop UI is active. During normal shutdown (`is_shutting_down()`),
/// this watcher exits quietly without showing error dialogs.
pub fn spawn_unexpected_exit_monitor<F>(controller: Arc<SidecarController>, on_unexpected_exit: F)
where
    F: FnOnce(Option<ExitStatus>, String) + Send + 'static,
{
    thread::Builder::new()
        .name("krushidhan-sidecar-watchdog".into())
        .spawn(move || {
            let mut callback = Some(on_unexpected_exit);
            loop {
                thread::sleep(Duration::from_millis(350));
                if controller.is_shutting_down() {
                    break;
                }
                if let Some(wait_res) = controller.try_wait_exited() {
                    if controller.is_shutting_down() {
                        break;
                    }
                    let tail = controller.diagnostic_tail();
                    let status = wait_res.ok();
                    if let Some(cb) = callback.take() {
                        cb(status, tail);
                    }
                    break;
                }
            }
        })
        .ok();
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::net::TcpListener;

    fn sample_valid_handshake_json(port: u16, token: &str) -> String {
        serde_json::json!({
            "protocol_version": 1,
            "status": "ready",
            "port": port,
            "launch_token": token,
            "pid": 12345,
            "app_version": "1.0.0"
        })
        .to_string()
    }

    #[test]
    fn test_parse_and_validate_handshake_accepts_valid_payload() {
        let token = "a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4e5f6a1b2";
        let raw = sample_valid_handshake_json(55123, token);
        let parsed = parse_and_validate_handshake(&raw).expect("valid handshake must parse");
        assert_eq!(parsed.protocol_version, 1);
        assert_eq!(parsed.status, "ready");
        assert_eq!(parsed.host, None);
        assert_eq!(parsed.port, 55123);
        assert_eq!(parsed.pid, 12345);
        assert_eq!(parsed.launch_token, token);
        assert_eq!(parsed.app_version.as_deref(), Some("1.0.0"));
    }

    #[test]
    fn test_parse_and_validate_handshake_rejects_invalid_fields() {
        let valid_tok = "0123456789abcdef0123456789abcdef01234567";

        // Wrong protocol_version
        let bad_proto = serde_json::json!({
            "protocol_version": 2, "status": "ready", "host": "127.0.0.1",
            "port": 55123, "pid": 100, "launch_token": valid_tok
        })
        .to_string();
        assert!(parse_and_validate_handshake(&bad_proto).is_err());

        // Forbidden port 8008
        let bad_port = sample_valid_handshake_json(8008, valid_tok);
        assert!(parse_and_validate_handshake(&bad_port).is_err());

        // Non-loopback host
        let bad_host = serde_json::json!({
            "protocol_version": 1, "status": "ready", "host": "0.0.0.0",
            "port": 55123, "pid": 100, "launch_token": valid_tok
        })
        .to_string();
        assert!(parse_and_validate_handshake(&bad_host).is_err());

        // Short launch token
        let short_tok = sample_valid_handshake_json(55123, "short_tok");
        assert!(parse_and_validate_handshake(&short_tok).is_err());

        // Whitespace in launch token
        let ws_tok = sample_valid_handshake_json(55123, "0123456789abcdef 0123456789abcdef0123456");
        assert!(parse_and_validate_handshake(&ws_tok).is_err());
    }

    #[test]
    fn test_redact_sensitive_text_never_leaks_launch_token() {
        let secret = "secret_tok_98765432109876543210987654321098";
        let line1 = format!(r#"{{"protocol_version": 1, "launch_token": "{secret}", "port": 55123}}"#);
        let red1 = redact_sensitive_text(&line1, None);
        assert!(!red1.contains(secret));
        assert!(red1.contains("[REDACTED]"));

        let line2 = format!("Request had header X-Krushidhan-Launch-Token: {secret} ok");
        let red2 = redact_sensitive_text(&line2, Some(secret));
        assert!(!red2.contains(secret));
        assert!(red2.contains("[REDACTED]"));

        let rk = "KDHN-AAAAA-BBBBB-CCCCC-DDDDD-EEEEE-FFFFF-GGGGG-HHHHH-IIIII-JJJJJ-KKKKK";
        let line3 = format!("Unexpected key value {rk} in stderr");
        let red3 = redact_sensitive_text(&line3, None);
        assert!(!red3.contains(rk));
        assert!(red3.contains("[REDACTED_RECOVERY_KEY]"));
    }

    #[test]
    fn test_supervisor_handshake_health_and_graceful_stdin_eof_shutdown() {
        let listener = TcpListener::bind("127.0.0.1:0").expect("bind loopback health mock");
        let port = listener.local_addr().unwrap().port();
        let expected_token = "mock_launch_token_1234567890abcdef1234567890abcd".to_string();
        let server_token = expected_token.clone();

        let server_thread = thread::spawn(move || {
            if let Ok((mut stream, _)) = listener.accept() {
                let mut req_buf = [0u8; 2048];
                let n = stream.read(&mut req_buf).unwrap_or(0);
                let req_str = String::from_utf8_lossy(&req_buf[..n]);
                let ok_auth = req_str.contains(&format!("X-Krushidhan-Launch-Token: {server_token}"))
                    && req_str.starts_with("GET /api/health ");
                let resp = if ok_auth {
                    "HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nConnection: close\r\n\r\n{\"status\":\"ok\"}"
                } else {
                    "HTTP/1.1 401 Unauthorized\r\nConnection: close\r\n\r\n{\"detail\":\"unauthorized\"}"
                };
                let _ = stream.write_all(resp.as_bytes());
                let _ = stream.flush();
            }
        });

        let hs_line = sample_valid_handshake_json(port, &expected_token);
        #[cfg(unix)]
        let cmd = {
            let mut c = Command::new("/bin/sh");
            c.arg("-c")
                .arg(format!("printf '%s\\n' '{}'; cat >/dev/null; exit 0", hs_line));
            c
        };
        #[cfg(windows)]
        let cmd = {
            let mut c = Command::new("python");
            c.arg("-u")
                .arg("-c")
                .arg(format!(
                    "import sys; sys.stdout.write({:?} + '\\n'); sys.stdout.flush(); sys.stdin.read()",
                    hs_line
                ));
            c
        };

        let (controller, hs) =
            spawn_and_verify_command(cmd, Duration::from_secs(5)).expect("mock sidecar must pass");
        assert_eq!(hs.port, port);
        assert_eq!(hs.launch_token, expected_token);

        let status = controller
            .shutdown_blocking(Duration::from_secs(3))
            .expect("mock process should exit cleanly on stdin EOF");
        assert_eq!(status.code(), Some(0));

        let _ = server_thread.join();
    }

    #[test]
    fn test_supervisor_detects_single_instance_exit_code_2() {
        #[cfg(unix)]
        let cmd = {
            let mut c = Command::new("/bin/sh");
            c.arg("-c")
                .arg("echo 'lock held by another process' 1>&2; exit 2");
            c
        };
        #[cfg(windows)]
        let cmd = {
            let mut c = Command::new("cmd.exe");
            c.args(["/C", "echo lock held by another process 1>&2 & exit /B 2"]);
            c
        };

        let err = spawn_and_verify_command(cmd, Duration::from_secs(5))
            .err()
            .expect("exit code 2 must fail startup");
        assert!(err.is_single_instance(), "expected SingleInstanceActive, got: {err:?}");
        assert!(err.user_message().contains("already running"));
    }

    #[test]
    fn test_resolve_sidecar_binary_finds_bundled_executable() {
        let path = resolve_sidecar_binary().expect("target/<profile>/krushidhan-sidecar must resolve");
        assert!(path.is_file());
        assert_eq!(
            path.file_name().and_then(|n| n.to_str()),
            Some(expected_sidecar_filename())
        );
    }
}
