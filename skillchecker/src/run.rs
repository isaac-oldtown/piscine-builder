//! Subprocess helper: captured output, optional stdin, hard timeout, process-tree kill.

use std::io::{Read, Write};
use std::path::Path;
use std::process::{Child, Command, ExitStatus, Stdio};
use std::thread::{self, JoinHandle};
use std::time::{Duration, Instant};

pub struct Out {
    pub success: bool,
    pub code: Option<i32>,
    pub timed_out: bool,
    pub stdout: String,
    pub stderr: String,
    pub status_text: String,
}

fn drain<R: Read + Send + 'static>(mut r: R) -> JoinHandle<Vec<u8>> {
    thread::spawn(move || {
        let mut buf = Vec::new();
        let _ = r.read_to_end(&mut buf);
        buf
    })
}

pub fn kill_tree(child: &mut Child) {
    let pid = child.id().to_string();
    #[cfg(windows)]
    let _ = Command::new("taskkill")
        .args(["/PID", &pid, "/T", "/F"])
        .stdout(Stdio::null())
        .stderr(Stdio::null())
        .status();
    #[cfg(unix)]
    let _ = Command::new("kill")
        .args(["-KILL", &format!("-{pid}")])
        .stderr(Stdio::null())
        .status();
    let _ = child.kill();
}

/// Human description of how a process ended ("exit 1", "segmentation fault").
pub fn describe_status(status: &ExitStatus) -> String {
    #[cfg(unix)]
    {
        use std::os::unix::process::ExitStatusExt;
        if let Some(sig) = status.signal() {
            return match sig {
                11 => "crashed: segmentation fault".into(),
                6 => "crashed: abort".into(),
                8 => "crashed: floating point exception".into(),
                7 => "crashed: bus error".into(),
                n => format!("killed by signal {n}"),
            };
        }
    }
    match status.code() {
        Some(c) => match c as u32 {
            0xC000_0005 => "crashed: segmentation fault (access violation)".into(),
            0xC000_00FD => "crashed: stack overflow".into(),
            0xC000_0094 => "crashed: integer division by zero".into(),
            0xC000_0409 => "crashed: abort (stack buffer overrun or fail-fast)".into(),
            0xC000_001D => "crashed: illegal instruction".into(),
            _ => format!("exit {c}"),
        },
        None => "terminated".into(),
    }
}

pub fn configure(cmd: &mut Command) {
    #[cfg(unix)]
    {
        use std::os::unix::process::CommandExt;
        cmd.process_group(0);
    }
    cmd.env("CARGO_TERM_COLOR", "never");
}

pub fn run(cmd: &mut Command, stdin: Option<&[u8]>, timeout: Duration) -> Result<Out, String> {
    cmd.stdin(if stdin.is_some() {
        Stdio::piped()
    } else {
        Stdio::null()
    })
    .stdout(Stdio::piped())
    .stderr(Stdio::piped());
    configure(cmd);
    let program = cmd.get_program().to_string_lossy().into_owned();
    let mut child = cmd
        .spawn()
        .map_err(|e| format!("cannot run `{program}`: {e}"))?;
    if let (Some(data), Some(mut sin)) = (stdin, child.stdin.take()) {
        let data = data.to_vec();
        thread::spawn(move || {
            let _ = sin.write_all(&data);
        });
    }
    let out = child.stdout.take().map(drain);
    let err = child.stderr.take().map(drain);
    let deadline = Instant::now() + timeout;
    let (status, timed_out) = loop {
        match child.try_wait() {
            Ok(Some(status)) => break (Some(status), false),
            Ok(None) if Instant::now() >= deadline => {
                kill_tree(&mut child);
                let _ = child.wait();
                break (None, true);
            }
            Ok(None) => thread::sleep(Duration::from_millis(5)),
            Err(e) => return Err(format!("waiting for `{program}`: {e}")),
        }
    };
    let join = |h: Option<JoinHandle<Vec<u8>>>| {
        let bytes = h.and_then(|h| h.join().ok()).unwrap_or_default();
        String::from_utf8_lossy(&bytes).into_owned()
    };
    Ok(Out {
        success: status.as_ref().is_some_and(|s| s.success()),
        code: status.as_ref().and_then(|s| s.code()),
        timed_out,
        stdout: join(out),
        stderr: join(err),
        status_text: match &status {
            Some(s) => describe_status(s),
            None => format!("timed out after {} ms", timeout.as_millis()),
        },
    })
}

/// True if `program --version` (or similar) can be spawned at all.
pub fn exists(program: &str) -> bool {
    let mut cmd = Command::new(program);
    cmd.arg("--version")
        .stdin(Stdio::null())
        .stdout(Stdio::null())
        .stderr(Stdio::null());
    cmd.status().is_ok()
}

/// Splits a command line into words: whitespace-separated, with "double" and
/// 'single' quotes grouping words. No shell is involved.
pub fn split_words(s: &str) -> Vec<String> {
    let mut words = Vec::new();
    let mut cur = String::new();
    let mut has = false;
    let mut quote: Option<char> = None;
    let mut chars = s.chars().peekable();
    while let Some(c) = chars.next() {
        match (quote, c) {
            (Some(q), c) if c == q => quote = None,
            (Some('"'), '\\') => {
                if let Some(n) = chars.next() {
                    cur.push(n);
                }
            }
            (Some(_), c) => cur.push(c),
            (None, '"' | '\'') => {
                quote = Some(c);
                has = true;
            }
            (None, c) if c.is_whitespace() => {
                if has {
                    words.push(std::mem::take(&mut cur));
                    has = false;
                }
            }
            (None, c) => {
                cur.push(c);
                has = true;
            }
        }
    }
    if has {
        words.push(cur);
    }
    words
}

/// Copies `from` into `to`, skipping version-control and build output dirs.
pub fn copy_tree(from: &Path, to: &Path) -> Result<(), String> {
    std::fs::create_dir_all(to).map_err(|e| format!("{}: {e}", to.display()))?;
    let rd = std::fs::read_dir(from).map_err(|e| format!("{}: {e}", from.display()))?;
    for entry in rd.flatten() {
        let name = entry.file_name();
        let name_s = name.to_string_lossy();
        if matches!(
            name_s.as_ref(),
            ".git" | "target" | "node_modules" | "__pycache__" | ".skillchecker"
        ) {
            continue;
        }
        let src = entry.path();
        let dst = to.join(&name);
        if src.is_dir() {
            copy_tree(&src, &dst)?;
        } else {
            std::fs::copy(&src, &dst).map_err(|e| format!("{}: {e}", src.display()))?;
        }
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn words_split_with_quotes() {
        assert_eq!(
            split_words(r#"python -m "my mod" 'a b' x\y"#),
            vec!["python", "-m", "my mod", "a b", "x\\y"]
        );
        assert_eq!(split_words(r#"echo """#), vec!["echo", ""]);
    }
}
