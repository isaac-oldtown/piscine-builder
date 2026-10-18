//! The environment the course will be followed in: detected on this machine
//! (coursecrafter assumes the student works where the course is built) and,
//! optionally, declared in the curriculum front matter when it differs.
//! Planning and generation only rely on tools listed here.

use proctor::lang;
use proctor::run;
use std::process::Command;
use std::thread;
use std::time::Duration;

/// (display name, program, version arguments)
const TOOLS: &[(&str, &str, &[&str])] = &[
    ("gcc", "gcc", &["--version"]),
    ("clang", "clang", &["--version"]),
    ("make", "make", &["--version"]),
    ("cmake", "cmake", &["--version"]),
    ("gdb", "gdb", &["--version"]),
    ("lldb", "lldb", &["--version"]),
    ("valgrind", "valgrind", &["--version"]),
    ("cargo", "cargo", &["--version"]),
    ("rustfmt", "rustfmt", &["--version"]),
    ("clippy", "cargo", &["clippy", "--version"]),
    ("bash", "bash", &["--version"]),
    ("perl", "perl", &["-e", "print $^V"]),
    ("node", "node", &["--version"]),
    ("npm", "npm", &["--version"]),
    ("deno", "deno", &["--version"]),
    ("go", "go", &["version"]),
    ("java", "java", &["-version"]),
    ("javac", "javac", &["-version"]),
    ("ruby", "ruby", &["--version"]),
    ("php", "php", &["--version"]),
    ("lua", "lua", &["-v"]),
    ("git", "git", &["--version"]),
    ("docker", "docker", &["--version"]),
    ("sqlite3", "sqlite3", &["--version"]),
    ("shellcheck", "shellcheck", &["--version"]),
    ("ruff", "ruff", &["--version"]),
];

#[derive(Clone)]
pub struct Env {
    pub os: String,
    pub arch: String,
    pub found: Vec<(String, String)>,
    pub missing: Vec<String>,
    pub facts: Vec<String>,
    /// Declared in the curriculum (`environment:`), if any.
    pub declared: Option<String>,
}

fn version_of(program: &str, args: &[&str]) -> Option<String> {
    let mut c = Command::new(lang::resolve(program));
    c.args(args);
    let out = match run::run(&mut c, None, Duration::from_secs(8)) {
        Ok(o) => o,
        // Windows ships some tools as .cmd shims (npm, yarn...).
        Err(_) if cfg!(windows) => {
            let mut c = Command::new(format!("{program}.cmd"));
            c.args(args);
            run::run(&mut c, None, Duration::from_secs(8)).ok()?
        }
        Err(_) => return None,
    };
    if !out.success {
        return None;
    }
    let text = if out.stdout.trim().is_empty() {
        out.stderr
    } else {
        out.stdout
    };
    let line = text
        .lines()
        .map(str::trim)
        .find(|l| !l.is_empty())?
        .to_string();
    // Keep the version-looking part.
    let dotted = regex::Regex::new(r"v?\d+(\.\d+)+").expect("regex");
    let after_version = regex::Regex::new(r"(?i)version\D*(\d+)").expect("regex");
    Some(match (dotted.find(&line), after_version.captures(&line)) {
        (Some(m), _) => m.as_str().trim_start_matches('v').to_string(),
        (None, Some(c)) => c[1].to_string(),
        _ => regex::Regex::new(r"(\d+)\s*$")
            .expect("regex")
            .captures(&line)
            .map(|c| c[1].to_string())
            .unwrap_or(line),
    })
}

/// AddressSanitizer is often installed with the compiler but unusable
/// (MinGW, some macOS setups): try it for real.
fn asan_works(cc: &str) -> bool {
    let dir = std::env::temp_dir().join(format!("piscinebuilder-asan-{}", std::process::id()));
    let _ = std::fs::create_dir_all(&dir);
    let src = dir.join("t.c");
    let bin = dir.join(lang::exe("t"));
    let ok = std::fs::write(&src, "int main(void) { return 0; }\n").is_ok()
        && Command::new(cc)
            .args([
                "-fsanitize=address",
                src.to_string_lossy().as_ref(),
                "-o",
                bin.to_string_lossy().as_ref(),
            ])
            .output()
            .is_ok_and(|o| o.status.success())
        && Command::new(&bin)
            .output()
            .is_ok_and(|o| o.status.success());
    let _ = std::fs::remove_dir_all(&dir);
    ok
}

/// Probes once per process (a few seconds), then reuses the result.
pub fn detect(declared: Option<String>) -> Env {
    static MACHINE: std::sync::OnceLock<Env> = std::sync::OnceLock::new();
    let mut e = MACHINE.get_or_init(probe).clone();
    e.declared = declared.filter(|d| !d.trim().is_empty());
    e
}

fn probe() -> Env {
    let handles: Vec<_> = TOOLS
        .iter()
        .map(|(name, prog, args)| {
            let (name, prog, args) = (name.to_string(), prog.to_string(), args.to_vec());
            thread::spawn(move || (name, version_of(&prog, &args)))
        })
        .collect();
    let mut found = Vec::new();
    let mut missing = Vec::new();
    for h in handles {
        if let Ok((name, v)) = h.join() {
            match v {
                Some(v) => found.push((name, v)),
                None => missing.push(name),
            }
        }
    }
    if let Some(py) = lang::python() {
        let v = version_of(&py, &["--version"]).unwrap_or_default();
        found.insert(0, (format!("python ({py})"), v));
    } else {
        missing.push("python 3".into());
    }
    let mut facts = Vec::new();
    match lang::cc() {
        Some(cc) => {
            facts.push(format!("C compiler: `{cc}`."));
            facts.push(if asan_works(&cc) {
                "AddressSanitizer works (-fsanitize=address): usable for leak and memory-error checks.".into()
            } else {
                "AddressSanitizer does NOT work with this compiler: do not use -fsanitize.".into()
            });
        }
        None => facts.push("No C compiler.".into()),
    }
    if !found.iter().any(|(n, _)| n == "valgrind") {
        facts.push("valgrind is not installed: do not wrap runs with it.".into());
    }
    if cfg!(windows) {
        let bash = lang::resolve("bash");
        facts.push(if bash.ends_with("bash.exe") && !bash.to_lowercase().contains("system32") {
            "Windows: commands run without a shell; bash comes from msys2 or Git Bash (POSIX tools like grep, sed, tr live there).".into()
        } else {
            "Windows: commands run without a shell, and no POSIX bash was found.".into()
        });
    }
    found.sort();
    missing.sort();
    Env {
        os: std::env::consts::OS.into(),
        arch: std::env::consts::ARCH.into(),
        found,
        missing,
        facts,
        declared: None,
    }
}

impl Env {
    pub fn has(&self, tool: &str) -> bool {
        self.found
            .iter()
            .any(|(n, _)| n == tool || n.starts_with(&format!("{tool} ")))
    }

    /// Compact text for prompts and for `coursecrafter env`.
    pub fn summary(&self) -> String {
        let mut s = String::new();
        if let Some(d) = &self.declared {
            s.push_str(&format!(
                "Declared by the course author (authoritative for what the student has):\n{}\n\nMachine where the course is being built and verified (detected):\n",
                d.trim()
            ));
        }
        s.push_str(&format!("OS: {} ({})\n", self.os, self.arch));
        let found: Vec<String> = self
            .found
            .iter()
            .map(|(n, v)| {
                if v.is_empty() {
                    n.clone()
                } else {
                    format!("{n} {v}")
                }
            })
            .collect();
        s.push_str(&format!("Available: {}\n", found.join(", ")));
        s.push_str(&format!("Not installed: {}\n", self.missing.join(", ")));
        for f in &self.facts {
            s.push_str(&format!("{f}\n"));
        }
        s
    }

    /// Text added to system prompts: the environment plus how to use it.
    pub fn prompt_block(&self) -> String {
        format!(
            "# Student environment\n{}\nRely only on tools listed as available. Every test runs on the build machine above \
             before it is kept, so a tool missing there makes the unit fail its quality gate.\n",
            self.summary()
        )
    }
}

/// Tools a unit's text mentions, to warn when they are missing.
pub const MENTIONABLE: &[&str] = &[
    "make",
    "cmake",
    "gdb",
    "lldb",
    "valgrind",
    "node",
    "npm",
    "deno",
    "go",
    "java",
    "ruby",
    "php",
    "lua",
    "git",
    "docker",
    "sqlite3",
    "shellcheck",
    "perl",
    "clang",
];
