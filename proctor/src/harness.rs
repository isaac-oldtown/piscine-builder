//! A long-lived harness process that loads the student's functions and
//! answers `call` requests over stdin/stdout (see `value.rs` for the format).
//! A crash or a hang only costs the current call: the process is restarted
//! on the next one.

use crate::run::{configure, describe_status, kill_tree};
use crate::value::{self, Value};
use std::collections::{BTreeMap, BTreeSet};
use std::io::{BufRead, BufReader, Read, Write};
use std::path::PathBuf;
use std::process::{Child, ChildStdin, Command, Stdio};
use std::sync::mpsc::{self, Receiver, RecvTimeoutError};
use std::sync::{Arc, Mutex};
use std::thread;
use std::time::Duration;

#[derive(Debug, Clone)]
pub enum CallError {
    Raised(String),
    Crashed(String),
    Timeout(u64),
    Protocol(String),
}

impl CallError {
    pub fn message(&self) -> String {
        match self {
            CallError::Raised(m) => match m.strip_prefix("panic: ") {
                Some(p) => format!("panicked: {p}"),
                None => format!("raised {m}"),
            },
            CallError::Crashed(m) => m.clone(),
            CallError::Timeout(ms) => format!("timed out after {ms} ms (infinite loop?)"),
            CallError::Protocol(m) => format!("harness error: {m}"),
        }
    }
}

struct Proc {
    child: Child,
    stdin: ChildStdin,
    rx: Receiver<String>,
    stderr: Arc<Mutex<String>>,
}

pub struct Harness {
    cmd: Vec<String>,
    cwd: PathBuf,
    env: BTreeMap<String, String>,
    timeout: Duration,
    pub funcs: BTreeSet<String>,
    proc: Option<Proc>,
}

impl Harness {
    pub fn new(
        cmd: Vec<String>,
        cwd: PathBuf,
        env: BTreeMap<String, String>,
        timeout: Duration,
        funcs: BTreeSet<String>,
    ) -> Harness {
        Harness {
            cmd,
            cwd,
            env,
            timeout,
            funcs,
            proc: None,
        }
    }

    fn start(&mut self) -> Result<(), CallError> {
        let (prog, args) = self
            .cmd
            .split_first()
            .ok_or(CallError::Protocol("empty command".into()))?;
        let mut cmd = Command::new(prog);
        cmd.args(args)
            .current_dir(&self.cwd)
            .envs(&self.env)
            .stdin(Stdio::piped())
            .stdout(Stdio::piped())
            .stderr(Stdio::piped());
        configure(&mut cmd);
        let mut child = cmd
            .spawn()
            .map_err(|e| CallError::Protocol(format!("cannot start harness `{prog}`: {e}")))?;
        let stdin = child
            .stdin
            .take()
            .ok_or(CallError::Protocol("no stdin".into()))?;
        let stdout = child
            .stdout
            .take()
            .ok_or(CallError::Protocol("no stdout".into()))?;
        let mut stderr_pipe = child
            .stderr
            .take()
            .ok_or(CallError::Protocol("no stderr".into()))?;
        let (tx, rx) = mpsc::channel();
        thread::spawn(move || {
            for line in BufReader::new(stdout).lines() {
                let Ok(line) = line else { break };
                if tx.send(line).is_err() {
                    break;
                }
            }
        });
        let stderr = Arc::new(Mutex::new(String::new()));
        let sink = Arc::clone(&stderr);
        thread::spawn(move || {
            let mut buf = [0u8; 4096];
            while let Ok(n) = stderr_pipe.read(&mut buf) {
                if n == 0 {
                    break;
                }
                if let Ok(mut s) = sink.lock() {
                    s.push_str(&String::from_utf8_lossy(&buf[..n]));
                    let len = s.len();
                    if len > 8192 {
                        let cut = s
                            .char_indices()
                            .map(|(i, _)| i)
                            .find(|&i| i >= len - 4096)
                            .unwrap_or(0);
                        s.drain(..cut);
                    }
                }
            }
        });
        self.proc = Some(Proc {
            child,
            stdin,
            rx,
            stderr,
        });
        Ok(())
    }

    fn stop(&mut self) {
        if let Some(mut p) = self.proc.take() {
            kill_tree(&mut p.child);
            let _ = p.child.wait();
        }
    }

    fn died(&mut self) -> CallError {
        let Some(mut p) = self.proc.take() else {
            return CallError::Crashed("harness exited".into());
        };
        let status = p.child.wait().ok();
        thread::sleep(Duration::from_millis(20));
        let tail = p
            .stderr
            .lock()
            .map(|s| last_meaningful_line(&s))
            .unwrap_or_default();
        let what = status
            .map(|s| describe_status(&s))
            .unwrap_or_else(|| "harness died".into());
        let what = if what.starts_with("exit ") {
            format!("exited the process ({what})")
        } else {
            what
        };
        CallError::Crashed(if tail.is_empty() {
            what
        } else {
            format!("{what}: {tail}")
        })
    }

    pub fn call(&mut self, name: &str, args: &[Value]) -> Result<Value, CallError> {
        if !self.funcs.contains(name) {
            return Err(CallError::Protocol(format!(
                "`{name}` is not callable through the harness"
            )));
        }
        if self.proc.is_none() {
            self.start()?;
        }
        let mut line = format!("call {name} {}", args.len());
        for a in args {
            line.push(' ');
            line.push_str(&value::encode(a));
        }
        line.push('\n');
        let wrote = match self.proc.as_mut() {
            Some(p) => p
                .stdin
                .write_all(line.as_bytes())
                .and_then(|_| p.stdin.flush())
                .is_ok(),
            None => false,
        };
        if !wrote {
            return Err(self.died());
        }
        loop {
            let res = match self.proc.as_ref() {
                Some(p) => p.rx.recv_timeout(self.timeout),
                None => return Err(CallError::Crashed("harness exited".into())),
            };
            match res {
                Ok(resp) => {
                    // Harnesses that cannot silence student stdout mark their answers.
                    let resp = match resp.find("\u{1e}SC ") {
                        Some(i) => &resp[i + 4..],
                        None => resp.as_str(),
                    };
                    let resp = resp.trim_end();
                    if let Some(v) = resp.strip_prefix("ok ") {
                        return value::decode_str(v).map_err(CallError::Protocol);
                    }
                    if resp == "ok" {
                        return Ok(Value::Null);
                    }
                    if let Some(rest) = resp.strip_prefix("exc ") {
                        let mut it = rest.splitn(2, ' ');
                        let ty = value::unhex_text(it.next().unwrap_or(""));
                        let msg = value::unhex_text(it.next().unwrap_or(""));
                        return Err(CallError::Raised(if msg.is_empty() {
                            ty
                        } else {
                            format!("{ty}: {msg}")
                        }));
                    }
                    if let Some(rest) = resp.strip_prefix("err ") {
                        return Err(CallError::Protocol(value::unhex_text(rest)));
                    }
                    // Anything else is stray output; keep waiting for the answer.
                }
                Err(RecvTimeoutError::Timeout) => {
                    self.stop();
                    return Err(CallError::Timeout(self.timeout.as_millis() as u64));
                }
                Err(RecvTimeoutError::Disconnected) => return Err(self.died()),
            }
        }
    }
}

impl Harness {
    /// Ends the process politely (stdin closed) and reports a failing exit:
    /// leak checkers and sanitizers only speak when the program ends.
    pub fn finish(&mut self) -> Option<String> {
        let Proc {
            mut child,
            stdin,
            stderr,
            ..
        } = self.proc.take()?;
        drop(stdin);
        let deadline = std::time::Instant::now() + self.timeout;
        let status = loop {
            match child.try_wait() {
                Ok(Some(s)) => break s,
                Ok(None) if std::time::Instant::now() < deadline => {
                    thread::sleep(Duration::from_millis(10))
                }
                _ => {
                    kill_tree(&mut child);
                    let _ = child.wait();
                    return None;
                }
            }
        };
        if status.success() {
            return None;
        }
        thread::sleep(Duration::from_millis(50));
        let text = stderr.lock().map(|s| s.clone()).unwrap_or_default();
        let key = text
            .lines()
            .map(str::trim)
            .find(|l| l.contains("ERROR") || l.contains("definitely lost") || l.contains("leak"))
            .map(|l| crate::report::truncate(l, 200))
            .unwrap_or_else(|| last_meaningful_line(&text));
        Some(format!(
            "the process ended with {}: {key}",
            describe_status(&status)
        ))
    }
}

impl Drop for Harness {
    fn drop(&mut self) {
        self.stop();
    }
}

fn last_meaningful_line(s: &str) -> String {
    s.lines()
        .rev()
        .map(str::trim)
        .find(|l| !l.is_empty())
        .map(|l| crate::report::truncate(l, 160))
        .unwrap_or_default()
}
