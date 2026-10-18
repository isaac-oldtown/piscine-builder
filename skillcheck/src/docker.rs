//! Units marked `platform: linux` need Linux APIs (fork, signals, /proc).
//! Elsewhere, skillcheck reruns the same command in an Ubuntu container: it
//! builds the image itself, from its own source, the first time it is needed
//! and again whenever skillcheck changes. The course folder is mounted, so the
//! student keeps working in their own editor and terminal.

use crate::course::{CourseDir, strip_verbatim};
use crate::selftest::UnitReport;
use crate::unit::Unit;
use std::fs;
use std::io::IsTerminal;
use std::path::Path;
use std::process::{Command, Stdio};

/// Set inside the container, so a command there never delegates again.
const INSIDE: &str = "SKILLCHECK_IN_CONTAINER";

/// skillcheck's own source, where `cargo install --path` built it from.
const SOURCE: &str = env!("CARGO_MANIFEST_DIR");

const DOCKERFILE: &str = r#"FROM ubuntu:24.04
RUN apt-get update \
 && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
    build-essential ca-certificates curl procps python3 \
 && rm -rf /var/lib/apt/lists/*
ENV RUSTUP_HOME=/usr/local/rustup CARGO_HOME=/usr/local/cargo PATH=/usr/local/cargo/bin:$PATH
RUN curl -sSf https://sh.rustup.rs | sh -s -- -y --profile minimal \
    --default-toolchain stable --component clippy,rustfmt
COPY src /src
RUN cargo install --path /src/skillcheck --root /usr/local \
 && rm -rf /src /usr/local/cargo/registry/cache
"#;

/// The container for this unit: on another OS than Linux, unless already inside.
pub fn needed(u: &Unit) -> bool {
    u.platform.as_deref() == Some("linux")
        && !cfg!(target_os = "linux")
        && std::env::var_os(INSIDE).is_none()
}

fn fnv(h: &mut u64, bytes: &[u8]) {
    for b in bytes {
        *h ^= u64::from(*b);
        *h = h.wrapping_mul(0x100000001b3);
    }
}

/// Files of the image's build context (relative path, content).
fn context() -> Result<Vec<(String, Vec<u8>)>, String> {
    let crate_dir = Path::new(SOURCE);
    let workspace = crate_dir.parent().unwrap_or(crate_dir);
    let missing = || {
        format!(
            "skillcheck's source is no longer at {SOURCE}, and the Linux image is built from it: reinstall skillcheck with `cargo install --path skillcheck` from the levelup folder"
        )
    };
    let mut out = vec![("Dockerfile".to_string(), DOCKERFILE.as_bytes().to_vec())];
    // A workspace of skillcheck alone: the other tools are not needed.
    let ws = fs::read_to_string(workspace.join("Cargo.toml")).map_err(|_| missing())?;
    let ws: String = ws
        .lines()
        .map(|l| {
            if l.trim_start().starts_with("members") {
                "members = [\"skillcheck\"]"
            } else {
                l
            }
        })
        .collect::<Vec<_>>()
        .join("\n");
    out.push(("src/Cargo.toml".into(), ws.into_bytes()));
    if let Ok(lock) = fs::read(workspace.join("Cargo.lock")) {
        out.push(("src/Cargo.lock".into(), lock));
    }
    out.push((
        "src/skillcheck/Cargo.toml".into(),
        fs::read(crate_dir.join("Cargo.toml")).map_err(|_| missing())?,
    ));
    let mut stack = vec![crate_dir.join("src")];
    let mut sources = Vec::new();
    while let Some(d) = stack.pop() {
        for e in fs::read_dir(&d).map_err(|_| missing())?.flatten() {
            let p = e.path();
            if p.is_dir() {
                stack.push(p);
            } else if let Ok(rel) = p.strip_prefix(crate_dir) {
                let rel = rel.to_string_lossy().replace('\\', "/");
                sources.push((
                    format!("src/skillcheck/{rel}"),
                    fs::read(&p).map_err(|e| e.to_string())?,
                ));
            }
        }
    }
    sources.sort();
    out.extend(sources);
    Ok(out)
}

fn quiet(cmd: &mut Command) -> bool {
    cmd.stdout(Stdio::null())
        .stderr(Stdio::null())
        .status()
        .is_ok_and(|s| s.success())
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Status {
    /// No `docker` command.
    Missing,
    /// Installed, but the engine does not answer.
    Stopped,
    /// Docker Desktop is set to Windows containers.
    WindowsContainers,
    Ready,
}

pub fn status() -> Status {
    let Ok(o) = Command::new("docker")
        .args(["info", "--format", "{{.OSType}}"])
        .stdin(Stdio::null())
        .output()
    else {
        return Status::Missing;
    };
    if !o.status.success() {
        Status::Stopped
    } else if String::from_utf8_lossy(&o.stdout).trim() != "linux" {
        Status::WindowsContainers
    } else {
        Status::Ready
    }
}

fn check_docker() -> Result<(), String> {
    match status() {
        Status::Ready => Ok(()),
        Status::Missing => Err("this unit runs on Linux, in a container, and `docker` was not found: install Docker Desktop (`taskmaster setup` can do it), start it, then run the command again".into()),
        Status::Stopped => Err("this unit runs on Linux, in a container, and Docker is not running: start Docker Desktop, wait until it is ready, then run the command again".into()),
        Status::WindowsContainers => Err(
            "Docker runs Windows containers: switch Docker Desktop to Linux containers".into(),
        ),
    }
}

/// The image tag, building the image first if this skillcheck has none yet.
pub fn image() -> Result<String, String> {
    check_docker()?;
    let files = context()?;
    let mut h = 0xcbf29ce484222325u64;
    for (p, body) in &files {
        fnv(&mut h, p.as_bytes());
        fnv(&mut h, body);
    }
    let tag = format!("levelup-skillcheck:{h:016x}");
    if quiet(Command::new("docker").args(["image", "inspect", &tag])) {
        return Ok(tag);
    }
    eprintln!(
        "Building the Linux image for Linux units (first time, or after a skillcheck update; a few minutes)..."
    );
    let dir = std::env::temp_dir().join(format!("levelup-image-{h:016x}"));
    let _ = fs::remove_dir_all(&dir);
    for (p, body) in &files {
        let path = dir.join(p);
        if let Some(d) = path.parent() {
            fs::create_dir_all(d).map_err(|e| e.to_string())?;
        }
        fs::write(&path, body).map_err(|e| format!("{}: {e}", path.display()))?;
    }
    let ok = Command::new("docker")
        .args(["build", "-t", &tag])
        .arg(&dir)
        .stdout(Stdio::from(std::io::stderr()))
        .status()
        .is_ok_and(|s| s.success());
    let _ = fs::remove_dir_all(&dir);
    if !ok {
        return Err("building the Linux image failed (see the output above); check your internet connection and run the command again".into());
    }
    Ok(tag)
}

/// `docker run` in the course folder, at the same place as the current directory.
fn run_command(
    c: &CourseDir,
    args: &[String],
    tty: bool,
    port: Option<u16>,
) -> Result<Command, String> {
    let tag = image()?;
    let root = strip_verbatim(&c.root.canonicalize().unwrap_or_else(|_| c.root.clone()));
    let cwd = std::env::current_dir().map_err(|e| e.to_string())?;
    let cwd = strip_verbatim(&cwd.canonicalize().unwrap_or(cwd));
    let rel = cwd
        .strip_prefix(&root)
        .map(Path::to_path_buf)
        .unwrap_or_default();
    let mut h = 0xcbf29ce484222325u64;
    fnv(&mut h, root.to_string_lossy().as_bytes());
    let mut cmd = Command::new("docker");
    cmd.args(["run", "--rm", "--init", "-i"]);
    if tty {
        cmd.arg("-t");
    }
    if let Some(p) = port {
        cmd.arg("-p").arg(format!("127.0.0.1:{p}:{p}"));
    }
    cmd.arg("-v").arg(format!("{}:/course", root.display()));
    // Builds stay in fast Linux storage, apart from the Windows ones.
    cmd.args(["-v", "levelup-cargo-registry:/usr/local/cargo/registry"]);
    cmd.arg("-v")
        .arg(format!("levelup-cache-{h:016x}:/course/.skillcheck/cache"));
    let rel = rel.to_string_lossy().replace('\\', "/");
    cmd.arg("-w")
        .arg(format!("/course/{rel}").trim_end_matches('/'));
    cmd.args(["-e", &format!("{INSIDE}=1")]);
    for k in ["NO_COLOR", "SKILLCHECK_ASCII"] {
        if std::env::var_os(k).is_some() {
            cmd.args(["-e", k]);
        }
    }
    cmd.arg(tag).args(args);
    Ok(cmd)
}

fn interactive() -> bool {
    std::io::stdin().is_terminal() && std::io::stdout().is_terminal()
}

/// Runs `args` in the container with the terminal attached; returns the exit code.
pub fn delegate(c: &CourseDir, args: &[String]) -> Result<u8, String> {
    let mut full = vec!["skillcheck".to_string()];
    full.extend(args.iter().cloned());
    let status = run_command(c, &full, interactive(), None)?
        .status()
        .map_err(|e| format!("docker: {e}"))?;
    Ok(status.code().map_or(2, |c| c.clamp(0, 255) as u8))
}

/// An interactive shell in the container, in `dir` (relative to the course).
pub fn shell(c: &CourseDir, dir: &Path, port: Option<u16>) -> Result<u8, String> {
    let rel = dir.strip_prefix(&c.root).unwrap_or(dir);
    let rel = rel.to_string_lossy().replace('\\', "/");
    let script = format!(
        "cd \"/course/{rel}\" 2>/dev/null; echo 'Linux container: your course folder is /course (exit to leave)'; exec bash"
    );
    let args: Vec<String> = ["bash", "-c", &script].map(String::from).to_vec();
    let status = run_command(c, &args, interactive(), port)?
        .status()
        .map_err(|e| format!("docker: {e}"))?;
    Ok(status.code().map_or(2, |c| c.clamp(0, 255) as u8))
}

/// Self-tests `units` in the container with `seed`.
pub fn selftest(c: &CourseDir, units: &[&Unit], seed: u64) -> Result<Vec<UnitReport>, String> {
    let mut args: Vec<String> = [
        "skillcheck",
        "selftest",
        "--json",
        "--no-validate",
        "--seed",
    ]
    .map(String::from)
    .to_vec();
    args.push(format!("{seed:016x}"));
    args.extend(units.iter().map(|u| u.id.clone()));
    let o = run_command(c, &args, false, None)?
        .stdin(Stdio::null())
        .stderr(Stdio::piped())
        .output()
        .map_err(|e| format!("docker: {e}"))?;
    let stdout = String::from_utf8_lossy(&o.stdout);
    let reports: Vec<UnitReport> = stdout
        .lines()
        .filter_map(|l| serde_json::from_str::<serde_json::Value>(l).ok())
        .filter_map(|v| UnitReport::from_json(&v))
        .collect();
    if reports.len() != units.len() {
        let err = String::from_utf8_lossy(&o.stderr);
        return Err(format!(
            "the self-test in the Linux container did not report: {}",
            crate::report::truncate(err.trim(), 300)
        ));
    }
    Ok(reports)
}
