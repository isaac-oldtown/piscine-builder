//! Per-language presets: toolchain lookup, build commands, harness code
//! generation (the hidden `main` that calls the student's functions), and
//! mechanical stubs. Languages without a harness template still work with
//! `target: program`.

use crate::harness::Harness;
use crate::run::{self, split_words};
use crate::scan;
use crate::sig::{Proto, Ty};
use crate::unit::LangConfig;
use std::collections::{BTreeMap, BTreeSet};
use std::fs;
use std::path::{Path, PathBuf};
use std::process::Command;
use std::sync::OnceLock;
use std::time::Duration;

pub const BUILD_TIMEOUT: Duration = Duration::from_secs(600);

pub fn has_harness(lang: &str) -> bool {
    matches!(lang, "c" | "python" | "rust")
}

/// Rust builds happen in place (crates often inherit a cargo workspace);
/// everything else is copied to a scratch directory first.
pub fn builds_in_place(lang: &str) -> bool {
    lang == "rust"
}

pub fn exe(name: &str) -> String {
    if cfg!(windows) {
        format!("{name}.exe")
    } else {
        name.to_string()
    }
}

/// Resolves a program name the way a shell would. On Windows, `bash`/`sh`
/// would otherwise hit `System32\bash.exe` (WSL) before `PATH`, so look for
/// an msys2 / Git bash on `PATH` first.
pub fn resolve(program: &str) -> String {
    if !cfg!(windows) || !matches!(program, "bash" | "sh") {
        return program.to_string();
    }
    static BASH: OnceLock<Option<String>> = OnceLock::new();
    let found = BASH.get_or_init(|| {
        let path = std::env::var_os("PATH").unwrap_or_default();
        let on_path = std::env::split_paths(&path)
            .filter(|d| {
                let s = d.to_string_lossy().to_lowercase();
                !s.contains("system32") && !s.contains("windowsapps")
            })
            .map(|d| d.join("bash.exe"));
        let home = std::env::var("USERPROFILE").unwrap_or_default();
        let known = [
            format!("{home}\\scoop\\apps\\msys2\\current\\usr\\bin\\bash.exe"),
            format!("{home}\\scoop\\apps\\git\\current\\bin\\bash.exe"),
            r"C:\msys64\usr\bin\bash.exe".to_string(),
            r"C:\Program Files\Git\bin\bash.exe".to_string(),
        ]
        .into_iter()
        .map(PathBuf::from);
        on_path
            .chain(known)
            .find(|p| p.is_file())
            .map(|p| p.to_string_lossy().into_owned())
    });
    found.clone().unwrap_or_else(|| program.to_string())
}

pub fn resolve_words(mut words: Vec<String>) -> Vec<String> {
    if let Some(first) = words.first_mut() {
        *first = resolve(first);
    }
    words
}

pub fn cc() -> Option<String> {
    static CC: OnceLock<Option<String>> = OnceLock::new();
    CC.get_or_init(|| {
        let mut candidates = Vec::new();
        if let Ok(c) = std::env::var("CC") {
            candidates.push(c);
        }
        candidates.extend(["cc", "gcc", "clang"].map(String::from));
        candidates.into_iter().find(|c| run::exists(c))
    })
    .clone()
}

pub fn python() -> Option<String> {
    static PY: OnceLock<Option<String>> = OnceLock::new();
    PY.get_or_init(|| {
        ["python3", "python"]
            .into_iter()
            .map(String::from)
            .find(|p| {
                Command::new(p).arg("--version").output().is_ok_and(|o| {
                    o.status.success() && String::from_utf8_lossy(&o.stdout).contains("Python 3")
                        || String::from_utf8_lossy(&o.stderr).contains("Python 3")
                })
            })
    })
    .clone()
}

pub struct BuildError {
    pub msg: String,
    pub log: String,
}

impl BuildError {
    fn new(msg: impl Into<String>) -> BuildError {
        BuildError {
            msg: msg.into(),
            log: String::new(),
        }
    }
}

pub struct Ctx<'a> {
    pub lang: &'a str,
    /// The tree to build (scratch copy, or the real workspace for Rust).
    pub ws: &'a Path,
    /// Scratch space for outputs (objects, executables, generated harness).
    pub work: &'a Path,
    pub env: &'a BTreeMap<String, String>,
    /// Persistent cache (cargo target dir).
    pub cache: &'a Path,
    /// Merged language settings (preset + course).
    pub cfg: &'a LangConfig,
    /// Course support files built in with the student's code.
    pub support: &'a [PathBuf],
    /// Prefix for every run of student code.
    pub wrap: Option<&'a str>,
}

impl Ctx<'_> {
    fn compiler(&self) -> Result<String, BuildError> {
        self.cfg
            .compiler
            .clone()
            .or_else(cc)
            .ok_or_else(|| BuildError::new("no C compiler found (cc, gcc or clang)"))
    }

    fn flags(&self) -> Vec<String> {
        self.cfg
            .flags
            .clone()
            .unwrap_or_else(|| C_FLAGS.iter().map(|s| s.to_string()).collect())
    }

    fn support_files(&self, ext: &str) -> Vec<String> {
        self.support
            .iter()
            .filter(|p| p.extension().is_some_and(|e| e == ext))
            .map(|p| p.to_string_lossy().into_owned())
            .collect()
    }

    fn support_dirs(&self) -> Vec<String> {
        let mut out: Vec<String> = Vec::new();
        for p in self.support {
            if let Some(d) = p.parent() {
                let d = d.to_string_lossy().into_owned();
                if !out.contains(&d) {
                    out.push(d);
                }
            }
        }
        out
    }

    /// `cmd` behind the `wrap` prefix (valgrind and the like), if any.
    pub fn wrapped(&self, cmd: Vec<String>) -> Vec<String> {
        match self.wrap {
            Some(w) if !w.trim().is_empty() => {
                let mut out = resolve_words(split_words(w));
                out.extend(cmd);
                out
            }
            _ => cmd,
        }
    }
    fn command(&self, words: &[String], cwd: &Path) -> Command {
        let mut c = Command::new(resolve(&words[0]));
        c.args(&words[1..]).current_dir(cwd).envs(self.env);
        if self.lang == "rust" {
            c.env("CARGO_TARGET_DIR", self.cache.join("target"));
        }
        c
    }

    fn exec(&self, words: &[String], cwd: &Path) -> Result<run::Out, BuildError> {
        let mut c = self.command(words, cwd);
        run::run(&mut c, None, BUILD_TIMEOUT).map_err(BuildError::new)
    }

    fn rel(&self, s: &str) -> String {
        let mut out = s.to_string();
        for base in [self.ws, self.work] {
            for form in [
                base.to_string_lossy().into_owned(),
                base.to_string_lossy().replace('\\', "/"),
            ] {
                for sep in ["\\", "/"] {
                    out = out.replace(&format!("{form}{sep}"), "");
                }
            }
        }
        out.replace(r"\\?\", "")
    }
}

/// Counts compiler/linter warnings in build output.
pub fn count_warnings(text: &str) -> usize {
    text.lines()
        .filter(|l| l.contains("warning:") && !l.contains("generated") && !l.contains("emitted"))
        .count()
}

/// The first useful error line of a build log, with its location.
pub fn first_error(log: &str) -> Option<String> {
    let lines: Vec<&str> = log.lines().collect();
    let noise = |l: &str| {
        l.contains("could not compile")
            || l.contains("aborting due to")
            || l.contains("build failed")
            || l.contains("ld returned")
            || l.starts_with("error: process didn't exit")
            || l.contains("compilation terminated")
    };
    // gcc/clang: `file:line:col: error: msg`
    if let Some(l) = lines
        .iter()
        .find(|l| l.contains(": error:") || l.contains(": fatal error:"))
    {
        return Some(crate::report::truncate(l.trim(), 200));
    }
    // rustc / cargo: `error[...]: msg` then ` --> file:line:col`
    if let Some(idx) = lines
        .iter()
        .position(|l| l.trim_start().starts_with("error") && !noise(l))
    {
        let msg = lines[idx].trim();
        let loc = lines[idx + 1..]
            .iter()
            .take(3)
            .find_map(|l| l.trim().strip_prefix("--> "));
        return Some(match loc {
            Some(loc) => format!(
                "{} ({})",
                crate::report::truncate(msg, 160),
                loc.replace('\\', "/")
            ),
            None => crate::report::truncate(msg, 200),
        });
    }
    // Python tracebacks and linker messages.
    if let Some(l) = lines
        .iter()
        .rev()
        .find(|l| l.contains("Error") || l.contains("undefined reference"))
    {
        return Some(crate::report::truncate(l.trim(), 200));
    }
    lines
        .iter()
        .map(|l| l.trim())
        .find(|l| !l.is_empty())
        .map(|l| crate::report::truncate(l, 200))
}

fn c_sources(ws: &Path) -> Vec<String> {
    scan::walk(ws)
        .into_iter()
        .filter(|f| f.ends_with(".c"))
        .collect()
}

fn c_include_flags(ws: &Path) -> Vec<String> {
    let mut dirs: BTreeSet<String> = BTreeSet::new();
    dirs.insert(".".into());
    for f in scan::walk(ws) {
        if f.ends_with(".h")
            && let Some((d, _)) = f.rsplit_once('/')
        {
            dirs.insert(d.to_string());
        }
    }
    dirs.into_iter().map(|d| format!("-I{d}")).collect()
}

pub const C_FLAGS: &[&str] = &["-std=c11", "-Wall", "-Wextra", "-Werror"];

fn substitute(words: Vec<String>, map: &[(&str, String)]) -> Vec<String> {
    let mut out = Vec::new();
    for w in words {
        if w == "{sources}" {
            if let Some((_, v)) = map.iter().find(|(k, _)| *k == "{sources}") {
                out.extend(v.split('\u{1f}').map(String::from));
            }
            continue;
        }
        let mut w = w;
        for (k, v) in map {
            w = w.replace(k, v);
        }
        out.push(w);
    }
    out
}

fn python_words(cmd: &str) -> Result<Vec<String>, BuildError> {
    let mut words = split_words(cmd);
    if let Some(first) = words.first_mut()
        && matches!(first.as_str(), "python" | "python3" | "{python}")
    {
        *first = python()
            .ok_or_else(|| BuildError::new("Python 3 is not installed (python3 / python)"))?;
    }
    Ok(words)
}

pub fn cargo_package(ws: &Path) -> Result<String, BuildError> {
    let text = fs::read_to_string(ws.join("Cargo.toml"))
        .map_err(|_| BuildError::new("Cargo.toml is missing"))?;
    let mut in_pkg = false;
    for line in text.lines() {
        let t = line.trim();
        if t.starts_with('[') {
            in_pkg = t == "[package]";
        } else if in_pkg
            && let Some(v) = t
                .strip_prefix("name")
                .and_then(|r| r.trim_start().strip_prefix('='))
        {
            return Ok(v.trim().trim_matches('"').to_string());
        }
    }
    Err(BuildError::new("Cargo.toml has no [package] name"))
}

/// The executable cargo builds: the only `[[bin]]` name if there is one,
/// else the package name.
pub fn cargo_binary(ws: &Path) -> Result<String, BuildError> {
    let text = fs::read_to_string(ws.join("Cargo.toml"))
        .map_err(|_| BuildError::new("Cargo.toml is missing"))?;
    let mut in_bin = false;
    let mut bins = Vec::new();
    for line in text.lines() {
        let t = line.trim();
        if t.starts_with('[') {
            in_bin = t == "[[bin]]";
        } else if in_bin
            && let Some(v) = t
                .strip_prefix("name")
                .and_then(|r| r.trim_start().strip_prefix('='))
        {
            bins.push(v.trim().trim_matches('"').to_string());
        }
    }
    match bins.as_slice() {
        [one] => Ok(one.clone()),
        _ => cargo_package(ws),
    }
}

/// Builds the student's program; returns the command that runs it.
pub fn build_program(
    ctx: &Ctx,
    run_cmd: Option<&str>,
    build_cmd: Option<&str>,
) -> Result<(Vec<String>, String), BuildError> {
    fs::create_dir_all(ctx.work).map_err(|e| BuildError::new(e.to_string()))?;
    let mut log = String::new();
    let mut vars: Vec<(&str, String)> = Vec::new();
    match ctx.lang {
        "c" => {
            let cc = ctx.compiler()?;
            let out = ctx.work.join(exe("prog"));
            let mut srcs = c_sources(ctx.ws);
            srcs.extend(ctx.support_files("c"));
            if srcs.is_empty() && build_cmd.is_none() {
                return Err(BuildError::new("no .c file to compile"));
            }
            vars.push(("{cc}", cc.clone()));
            vars.push(("{out}", out.to_string_lossy().into_owned()));
            vars.push(("{exe}", out.to_string_lossy().into_owned()));
            vars.push(("{sources}", srcs.join("\u{1f}")));
            let words = match build_cmd {
                Some(b) => substitute(split_words(b), &vars),
                None => {
                    let mut w = vec![cc];
                    w.extend(ctx.flags());
                    w.extend(c_include_flags(ctx.ws));
                    w.extend(ctx.support_dirs().into_iter().map(|d| format!("-I{d}")));
                    w.extend(srcs);
                    w.extend(["-o".into(), out.to_string_lossy().into_owned()]);
                    w
                }
            };
            let o = ctx.exec(&words, ctx.ws)?;
            log.push_str(&o.stderr);
            if !o.success {
                return Err(BuildError {
                    msg: first_error(&ctx.rel(&o.stderr)).unwrap_or("compilation failed".into()),
                    log: o.stderr,
                });
            }
        }
        "python" => {
            let py = python()
                .ok_or_else(|| BuildError::new("Python 3 is not installed (python3 / python)"))?;
            let o = ctx.exec(
                &[
                    py.clone(),
                    "-m".into(),
                    "compileall".into(),
                    "-q".into(),
                    ".".into(),
                ],
                ctx.ws,
            )?;
            if !o.success {
                let all = format!("{}{}", o.stdout, o.stderr);
                return Err(BuildError {
                    msg: first_error(&ctx.rel(&all)).unwrap_or("syntax error".into()),
                    log: all,
                });
            }
            vars.push(("{python}", py));
        }
        "rust" => {
            let o = ctx.exec(&["cargo".into(), "build".into(), "--quiet".into()], ctx.ws)?;
            log.push_str(&o.stderr);
            if !o.success {
                return Err(BuildError {
                    msg: first_error(&ctx.rel(&o.stderr)).unwrap_or("cargo build failed".into()),
                    log: o.stderr,
                });
            }
            let pkg = cargo_binary(ctx.ws)?;
            let bin = ctx.cache.join("target").join("debug").join(exe(&pkg));
            vars.push(("{cargo_bin}", bin.to_string_lossy().into_owned()));
            if run_cmd.is_none() {
                return Ok((ctx.wrapped(vec![bin.to_string_lossy().into_owned()]), log));
            }
        }
        "bash" | "sh" | "shell" => {
            for f in scan::walk(ctx.ws)
                .into_iter()
                .filter(|f| f.ends_with(".sh"))
            {
                let o = ctx.exec(&["bash".into(), "-n".into(), f.clone()], ctx.ws)?;
                if !o.success {
                    return Err(BuildError {
                        msg: first_error(&ctx.rel(&o.stderr))
                            .unwrap_or(format!("{f}: syntax error")),
                        log: o.stderr,
                    });
                }
            }
        }
        _ => {
            if let Some(b) = build_cmd {
                let o = ctx.exec(&split_words(b), ctx.ws)?;
                if !o.success {
                    let all = format!("{}{}", o.stdout, o.stderr);
                    return Err(BuildError {
                        msg: first_error(&ctx.rel(&all)).unwrap_or("build failed".into()),
                        log: all,
                    });
                }
                log.push_str(&o.stderr);
            }
        }
    }
    let cmd = match run_cmd {
        Some(r) => resolve_words(substitute(python_words(r)?, &vars)),
        None if ctx.lang == "c" => vec![ctx.work.join(exe("prog")).to_string_lossy().into_owned()],
        None => {
            return Err(BuildError::new(
                "this unit needs a `run:` command for its program",
            ));
        }
    };
    Ok((ctx.wrapped(cmd), log))
}

/// Builds a harness exposing `protos`; returns it plus the build log.
pub fn build_harness(
    ctx: &Ctx,
    protos: &[Proto],
    module: Option<&str>,
    timeout: Duration,
) -> Result<(Harness, String), BuildError> {
    fs::create_dir_all(ctx.work).map_err(|e| BuildError::new(e.to_string()))?;
    let callable: Vec<&Proto> = protos
        .iter()
        .filter(|p| harness_supports(ctx.lang, p).is_ok())
        .collect();
    let names: BTreeSet<String> = callable.iter().map(|p| p.name.clone()).collect();
    match ctx.lang {
        "c" => build_c_harness(ctx, &callable, names, timeout),
        "python" => {
            let py = python()
                .ok_or_else(|| BuildError::new("Python 3 is not installed (python3 / python)"))?;
            let o = ctx.exec(
                &[
                    py.clone(),
                    "-m".into(),
                    "compileall".into(),
                    "-q".into(),
                    ".".into(),
                ],
                ctx.ws,
            )?;
            if !o.success {
                let all = format!("{}{}", o.stdout, o.stderr);
                return Err(BuildError {
                    msg: first_error(&ctx.rel(&all)).unwrap_or("syntax error".into()),
                    log: all,
                });
            }
            let script = ctx.work.join("skillcheck_harness.py");
            fs::write(&script, PY_HARNESS).map_err(|e| BuildError::new(e.to_string()))?;
            let cmd = ctx.wrapped(vec![
                py,
                "-u".into(),
                script.to_string_lossy().into_owned(),
                ctx.ws.to_string_lossy().into_owned(),
                module.unwrap_or("").to_string(),
            ]);
            let mut env = ctx.env.clone();
            let dirs = ctx.support_dirs();
            if !dirs.is_empty() {
                let sep = if cfg!(windows) { ";" } else { ":" };
                env.insert("PYTHONPATH".into(), dirs.join(sep));
            }
            env.insert("PYTHONIOENCODING".into(), "utf-8".into());
            env.insert("PYTHONDONTWRITEBYTECODE".into(), "1".into());
            Ok((
                Harness::new(cmd, ctx.ws.to_path_buf(), env, timeout, names),
                String::new(),
            ))
        }
        "rust" => build_rust_harness(ctx, &callable, names, timeout),
        other => Err(BuildError::new(format!(
            "`target: function` has no harness for {other}; use `target: program`"
        ))),
    }
}

/// Whether the harness can pass this function's parameter and return types.
pub fn harness_supports(lang: &str, p: &Proto) -> Result<(), String> {
    if p.is_method {
        return Err(format!(
            "`{}` is a method; only free functions can be called",
            p.name
        ));
    }
    fn ok(t: &Ty, lang: &str) -> bool {
        match t {
            Ty::Other(o) => lang == "python" && (o == "untyped" || !o.is_empty()),
            Ty::List(i) | Ty::Opt(i) => lang != "c" && ok(i, lang),
            _ => true,
        }
    }
    for prm in &p.params {
        if lang == "python" {
            continue;
        }
        if !ok(&prm.ty, lang) {
            return Err(format!(
                "parameter `{}` of `{}` has type `{}`, which the {lang} harness cannot build",
                prm.name, p.name, prm.raw
            ));
        }
    }
    if lang == "c" && matches!(p.ret, Ty::Other(_)) {
        return Err(format!(
            "`{}` returns `{}`, which the C harness cannot read",
            p.name, p.ret_raw
        ));
    }
    Ok(())
}

// ------------------------------------------------------------------- C harness

const C_HARNESS_HEAD: &str = r#"#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <stddef.h>
#include <stdint.h>
#include <stdbool.h>
#ifdef _WIN32
#include <io.h>
#define SC_DUP _dup
#define SC_DUP2 _dup2
#define SC_FDOPEN _fdopen
#else
#include <unistd.h>
#define SC_DUP dup
#define SC_DUP2 dup2
#define SC_FDOPEN fdopen
#endif

static FILE *sc_out;
static char *sc_cur;

static char *sc_tok(void) {
    while (*sc_cur == ' ') sc_cur++;
    if (!*sc_cur) return "n";
    char *t = sc_cur;
    while (*sc_cur && *sc_cur != ' ') sc_cur++;
    if (*sc_cur) *sc_cur++ = 0;
    return t;
}
static int sc_hexval(char c) { return c <= '9' ? c - '0' : (c | 32) - 'a' + 10; }
static char *sc_str(const char *t) {
    if (t[0] != 's') return NULL;
    size_t n = strlen(t + 1) / 2;
    char *s = malloc(n + 1);
    for (size_t i = 0; i < n; i++) s[i] = (char)(sc_hexval(t[1 + 2 * i]) * 16 + sc_hexval(t[2 + 2 * i]));
    s[n] = 0;
    return s;
}
static long long sc_int(const char *t) {
    if (t[0] == 'i' || t[0] == 'c') return strtoll(t + 1, NULL, 10);
    if (t[0] == 'b') return t[1] == '1';
    if (t[0] == 'f') return (long long)strtod(t + 1, NULL);
    return 0;
}
static unsigned long long sc_uint(const char *t) {
    if (t[0] == 'i' && t[1] != '-') return strtoull(t + 1, NULL, 10);
    return (unsigned long long)sc_int(t);
}
static double sc_float(const char *t) { return t[0] == 'f' ? strtod(t + 1, NULL) : (double)sc_int(t); }
static char sc_char(const char *t) {
    if (t[0] == 's') { char *s = sc_str(t); char c = s[0]; free(s); return c; }
    return (char)sc_int(t);
}
static char **sc_strlist(const char *t) {
    if (t[0] != 'l') return NULL;
    long n = strtol(t + 1, NULL, 10);
    char **v = malloc((size_t)(n + 1) * sizeof *v);
    for (long i = 0; i < n; i++) v[i] = sc_str(sc_tok());
    v[n] = NULL;
    return v;
}
static void sc_put_str(const char *s) {
    if (!s) { fputs("n", sc_out); return; }
    fputc('s', sc_out);
    for (const unsigned char *p = (const unsigned char *)s; *p; p++) fprintf(sc_out, "%02x", *p);
}
static void sc_put_int(long long v) { fprintf(sc_out, "i%lld", v); }
static void sc_put_uint(unsigned long long v) { fprintf(sc_out, "i%llu", v); }
static void sc_put_float(double v) { fprintf(sc_out, "f%.17g", v); }
static void sc_put_char(char c) { fprintf(sc_out, "c%d", (int)(unsigned char)c); }
static void sc_put_bool(int b) { fputs(b ? "b1" : "b0", sc_out); }
static void sc_put_strlist(char **v) {
    if (!v) { fputs("n", sc_out); return; }
    unsigned long long n = 0;
    while (v[n]) n++;
    fprintf(sc_out, "l%llu", n);
    for (unsigned long long i = 0; i < n; i++) { fputc(' ', sc_out); sc_put_str(v[i]); }
}
static char *sc_line(void) {
    static char *buf = NULL;
    static size_t cap = 0;
    size_t len = 0;
    int c;
    while ((c = getchar()) != EOF && c != '\n') {
        if (len + 2 > cap) { cap = cap ? cap * 2 : 4096; buf = realloc(buf, cap); }
        if (c != '\r') buf[len++] = (char)c;
    }
    if (c == EOF && len == 0) return NULL;
    if (!buf) { cap = 16; buf = malloc(cap); }
    buf[len] = 0;
    return buf;
}

"#;

fn c_decode(t: &Ty, raw: &str, tok: &str) -> String {
    match t {
        Ty::Int { unsigned: true } => format!("({raw})sc_uint({tok})"),
        Ty::Int { unsigned: false } => format!("({raw})sc_int({tok})"),
        Ty::Float => format!("({raw})sc_float({tok})"),
        Ty::Bool => format!("({raw})(sc_int({tok}) != 0)"),
        Ty::Char => format!("sc_char({tok})"),
        Ty::Str => format!("({raw})sc_str({tok})"),
        Ty::StrList => format!("({raw})sc_strlist({tok})"),
        _ => format!("({raw})0"),
    }
}

fn c_encode(t: &Ty, expr: &str) -> String {
    match t {
        Ty::Int { unsigned: true } => format!("sc_put_uint((unsigned long long)({expr}));"),
        Ty::Int { unsigned: false } => format!("sc_put_int((long long)({expr}));"),
        Ty::Float => format!("sc_put_float((double)({expr}));"),
        Ty::Bool => format!("sc_put_bool(({expr}) != 0);"),
        Ty::Char => format!("sc_put_char({expr});"),
        Ty::Str => format!("sc_put_str({expr});"),
        Ty::StrList => format!("sc_put_strlist((char **)({expr}));"),
        _ => "fputs(\"n\", sc_out);".into(),
    }
}

/// Index of the first writable string parameter (char *, not const).
pub fn mutable_c_buffer(p: &Proto) -> Option<usize> {
    p.params
        .iter()
        .position(|x| x.ty == Ty::Str && !x.raw.split_whitespace().any(|w| w == "const"))
}

fn c_harness_source(protos: &[&Proto], sigs: &BTreeMap<String, String>) -> String {
    let mut s = String::from(C_HARNESS_HEAD);
    for p in protos {
        if let Some(sig) = sigs.get(&p.name) {
            s.push_str(&format!("{};\n", sig.trim().trim_end_matches(';')));
        }
    }
    s.push_str("\nint main(void) {\n    fflush(stdout);\n    sc_out = SC_FDOPEN(SC_DUP(1), \"w\");\n    SC_DUP2(2, 1);\n    char *line;\n    while ((line = sc_line()) != NULL) {\n        sc_cur = line;\n        if (strcmp(sc_tok(), \"call\") != 0) continue;\n        const char *fn = sc_tok();\n        sc_tok();\n        fputs(\"ok \", sc_out);\n        if (0) {\n        }");
    for p in protos {
        s.push_str(&format!(" else if (strcmp(fn, \"{}\") == 0) {{\n", p.name));
        let mut args = Vec::new();
        for (i, prm) in p.params.iter().enumerate() {
            let decl_ty = if matches!(prm.ty, Ty::Char) {
                "char".to_string()
            } else {
                prm.raw.clone()
            };
            s.push_str(&format!(
                "            {decl_ty} a{i} = {};\n",
                c_decode(&prm.ty, &prm.raw, "sc_tok()")
            ));
            args.push(format!("a{i}"));
        }
        let call = format!("{}({})", p.name, args.join(", "));
        if p.ret == Ty::Void {
            // In-place functions: the value is the mutated buffer.
            let out = match mutable_c_buffer(p) {
                Some(i) => format!("sc_put_str(a{i});"),
                None => "fputs(\"n\", sc_out);".to_string(),
            };
            s.push_str(&format!("            {call};\n            {out}\n"));
        } else {
            s.push_str(&format!(
                "            {} r = {call};\n            {}\n",
                p.ret_raw,
                c_encode(&p.ret, "r")
            ));
        }
        s.push_str("        }");
    }
    s.push_str(" else {\n            fputs(\"n\", sc_out);\n        }\n        fputc('\\n', sc_out);\n        fflush(sc_out);\n    }\n    return 0;\n}\n");
    s
}

fn build_c_harness(
    ctx: &Ctx,
    protos: &[&Proto],
    names: BTreeSet<String>,
    timeout: Duration,
) -> Result<(Harness, String), BuildError> {
    let cc = ctx.compiler()?;
    let flags = ctx.flags();
    // Flags that must also reach the harness and the link (sanitizers, -pthread...).
    let link_flags: Vec<String> = flags
        .iter()
        .filter(|f| {
            f.starts_with("-fsanitize")
                || f.starts_with("-fno-omit")
                || *f == "-pthread"
                || f.starts_with("-l")
                || f.starts_with("-L")
        })
        .cloned()
        .collect();
    let obj = ctx.work.join("obj");
    let _ = fs::remove_dir_all(&obj);
    fs::create_dir_all(&obj).map_err(|e| BuildError::new(e.to_string()))?;
    let mut log = String::new();
    let mut objects = Vec::new();
    let mut includes = c_include_flags(ctx.ws);
    includes.extend(ctx.support_dirs().into_iter().map(|d| format!("-I{d}")));
    for (i, src) in c_sources(ctx.ws).iter().enumerate() {
        let o = obj.join(format!("s{i}.o"));
        let mut w = vec![cc.clone()];
        w.extend(flags.iter().cloned());
        w.extend(includes.iter().cloned());
        w.extend([
            "-Dmain=skillcheck_student_main".into(),
            "-c".into(),
            src.clone(),
            "-o".into(),
            o.to_string_lossy().into_owned(),
        ]);
        let out = ctx.exec(&w, ctx.ws)?;
        log.push_str(&out.stderr);
        if !out.success {
            return Err(BuildError {
                msg: first_error(&ctx.rel(&out.stderr)).unwrap_or("compilation failed".into()),
                log: out.stderr,
            });
        }
        objects.push(o.to_string_lossy().into_owned());
    }
    // Course support code: trusted, so no -Werror, but the same sanitizers.
    for (i, src) in ctx.support_files("c").iter().enumerate() {
        let o = obj.join(format!("support{i}.o"));
        let mut w = vec![cc.clone(), "-std=c11".into(), "-w".into()];
        w.extend(link_flags.iter().cloned());
        w.extend(includes.iter().cloned());
        w.extend([
            "-c".into(),
            src.clone(),
            "-o".into(),
            o.to_string_lossy().into_owned(),
        ]);
        let out = ctx.exec(&w, ctx.ws)?;
        if !out.success {
            return Err(BuildError {
                msg: format!(
                    "course error: support file does not compile: {}",
                    first_error(&out.stderr).unwrap_or_default()
                ),
                log: out.stderr,
            });
        }
        objects.push(o.to_string_lossy().into_owned());
    }
    let sigs: BTreeMap<String, String> = protos
        .iter()
        .map(|p| (p.name.clone(), p.sig_text.clone()))
        .collect();
    let src = ctx.work.join("skillcheck_harness.c");
    fs::write(&src, c_harness_source(protos, &sigs)).map_err(|e| BuildError::new(e.to_string()))?;
    let hobj = ctx.work.join("skillcheck_harness.o");
    let out = ctx.exec(
        &[
            cc.clone(),
            "-std=c11".into(),
            "-w".into(),
            "-c".into(),
            src.to_string_lossy().into_owned(),
            "-o".into(),
            hobj.to_string_lossy().into_owned(),
        ],
        ctx.work,
    )?;
    if !out.success {
        return Err(BuildError {
            msg: format!(
                "internal: harness does not compile: {}",
                first_error(&out.stderr).unwrap_or_default()
            ),
            log: out.stderr,
        });
    }
    let bin = ctx.work.join(exe("skillcheck_harness"));
    let mut w = vec![cc];
    w.extend(link_flags);
    w.extend(objects);
    w.extend([
        hobj.to_string_lossy().into_owned(),
        "-o".into(),
        bin.to_string_lossy().into_owned(),
    ]);
    let out = ctx.exec(&w, ctx.ws)?;
    if !out.success {
        let undefined = out
            .stderr
            .lines()
            .find_map(|l| {
                l.split("undefined reference to `")
                    .nth(1)
                    .and_then(|r| r.split('\'').next())
            })
            .map(|n| format!("function `{n}` is not defined (or is static)"));
        return Err(BuildError {
            msg: undefined
                .or_else(|| first_error(&ctx.rel(&out.stderr)))
                .unwrap_or("link failed".into()),
            log: out.stderr,
        });
    }
    Ok((
        Harness::new(
            ctx.wrapped(vec![bin.to_string_lossy().into_owned()]),
            ctx.ws.to_path_buf(),
            ctx.env.clone(),
            timeout,
            names,
        ),
        log,
    ))
}

// -------------------------------------------------------------- Python harness

const PY_HARNESS: &str = r#"import sys, os, importlib, dataclasses, enum
sys.path.insert(0, sys.argv[1])
os.chdir(sys.argv[1])
DEFAULT = sys.argv[2] if len(sys.argv) > 2 else ""
_proto = os.fdopen(os.dup(1), "w", encoding="utf-8", newline="\n")
os.dup2(2, 1)
sys.stdout = sys.stderr
sys.setrecursionlimit(20000)

def hx(s):
    return s.encode("utf-8", "replace").hex()

def dec(toks):
    t = next(toks)
    tag, rest = t[0], t[1:]
    if tag == "n": return None
    if tag == "b": return rest == "1"
    if tag == "i": return int(rest)
    if tag == "f": return float(rest)
    if tag == "s": return bytes.fromhex(rest).decode("utf-8", "replace")
    if tag == "c": return chr(int(rest))
    if tag == "o": return bytes.fromhex(rest).decode("utf-8", "replace")
    if tag == "l": return [dec(toks) for _ in range(int(rest))]
    if tag == "m":
        d = {}
        for _ in range(int(rest)):
            k = dec(toks)
            if isinstance(k, list): k = tuple(k)
            d[k] = dec(toks)
        return d
    raise ValueError("bad value tag " + t)

def enc(v, out, depth=0):
    if depth > 200:
        out.append("o" + hx("<too deep>")); return
    if v is None: out.append("n")
    elif v is True: out.append("b1")
    elif v is False: out.append("b0")
    elif isinstance(v, enum.Enum): out.append("s" + hx(v.name))
    elif isinstance(v, int): out.append("i%d" % v)
    elif isinstance(v, float): out.append("f" + repr(v))
    elif isinstance(v, str): out.append("s" + hx(v))
    elif hasattr(v, "_asdict"): enc(dict(v._asdict()), out, depth + 1)
    elif dataclasses.is_dataclass(v) and not isinstance(v, type):
        enc({f.name: getattr(v, f.name) for f in dataclasses.fields(v)}, out, depth + 1)
    elif isinstance(v, (list, tuple)):
        out.append("l%d" % len(v))
        for x in v: enc(x, out, depth + 1)
    elif isinstance(v, (set, frozenset)):
        items = sorted(v, key=repr)
        out.append("l%d" % len(items))
        for x in items: enc(x, out, depth + 1)
    elif isinstance(v, dict):
        out.append("m%d" % len(v))
        for k, x in v.items():
            enc(k, out, depth + 1); enc(x, out, depth + 1)
    elif isinstance(v, (bytes, bytearray)): out.append("o" + hx(repr(v)))
    elif hasattr(v, "__next__"):
        items = []
        for x in v:
            items.append(x)
            if len(items) > 1000000: break
        enc(items, out, depth + 1)
    else: out.append("o" + hx(str(v)))

mods = {}
def resolve(name):
    mod, attr = name.rsplit(".", 1) if "." in name else (DEFAULT, name)
    if not mod:
        raise ImportError("no module given for " + name)
    if mod not in mods:
        mods[mod] = importlib.import_module(mod)
    return getattr(mods[mod], attr)

while True:
    line = sys.stdin.readline()
    if not line: break
    parts = line.split()
    if len(parts) < 3 or parts[0] != "call": continue
    try:
        toks = iter(parts[3:])
        args = [dec(toks) for _ in range(int(parts[2]))]
        r = resolve(parts[1])(*args)
        out = ["ok"]
        enc(r, out)
        resp = " ".join(out)
    except RecursionError:
        resp = "exc " + hx("RecursionError") + " " + hx("maximum recursion depth exceeded")
    except BaseException as e:
        resp = "exc " + hx(type(e).__name__) + " " + hx(str(e))
    _proto.write(resp + "\n")
    _proto.flush()
"#;

// ---------------------------------------------------------------- Rust harness

const RUST_HARNESS_HEAD: &str = r#"#![allow(clippy::all, unused)]
use std::io::{BufRead, Write};

enum W { N, B(bool), I(i128), F(f64), S(String), C(char), L(Vec<W>), M(Vec<(W, W)>), O(String) }
struct Exc(String, String);

fn hx(s: &str) -> String { s.bytes().map(|b| format!("{b:02x}")).collect() }
fn unhx(s: &str) -> String {
    let b: Vec<u8> = (0..s.len() / 2).filter_map(|i| u8::from_str_radix(&s[2 * i..2 * i + 2], 16).ok()).collect();
    String::from_utf8_lossy(&b).into_owned()
}
fn parse<'a>(it: &mut impl Iterator<Item = &'a str>) -> Result<W, Exc> {
    let t = it.next().ok_or(Exc("Protocol".into(), "truncated".into()))?;
    let (tag, rest) = t.split_at(1);
    Ok(match tag {
        "n" => W::N,
        "b" => W::B(rest == "1"),
        "i" => W::I(rest.parse().unwrap_or(0)),
        "f" => W::F(rest.parse().unwrap_or(f64::NAN)),
        "s" => W::S(unhx(rest)),
        "o" => W::O(unhx(rest)),
        "c" => W::C(rest.parse().ok().and_then(char::from_u32).unwrap_or('?')),
        "l" => { let n: usize = rest.parse().unwrap_or(0); let mut v = Vec::new(); for _ in 0..n { v.push(parse(it)?); } W::L(v) }
        "m" => { let n: usize = rest.parse().unwrap_or(0); let mut v = Vec::new(); for _ in 0..n { let k = parse(it)?; v.push((k, parse(it)?)); } W::M(v) }
        _ => return Err(Exc("Protocol".into(), format!("bad tag {t}"))),
    })
}
fn emit(w: &W, o: &mut String) {
    match w {
        W::N => o.push('n'),
        W::B(b) => o.push_str(if *b { "b1" } else { "b0" }),
        W::I(i) => o.push_str(&format!("i{i}")),
        W::F(f) => o.push_str(&format!("f{f:e}")),
        W::S(s) => { o.push('s'); o.push_str(&hx(s)); }
        W::O(s) => { o.push('o'); o.push_str(&hx(s)); }
        W::C(c) => o.push_str(&format!("c{}", *c as u32)),
        W::L(l) => { o.push_str(&format!("l{}", l.len())); for x in l { o.push(' '); emit(x, o); } }
        W::M(m) => { o.push_str(&format!("m{}", m.len())); for (k, v) in m { o.push(' '); emit(k, o); o.push(' '); emit(v, o); } }
    }
}
fn bad(what: &str) -> Exc { Exc("TypeError".into(), format!("cannot convert argument to {what}")) }

trait FromW: Sized { fn from_w(w: W) -> Result<Self, Exc>; }
macro_rules! ints { ($($t:ty),*) => { $(
    impl FromW for $t { fn from_w(w: W) -> Result<Self, Exc> { match w { W::I(i) => <$t>::try_from(i).map_err(|_| bad(stringify!($t))), W::C(c) => <$t>::try_from(c as u32 as i128).map_err(|_| bad(stringify!($t))), _ => Err(bad(stringify!($t))) } } }
    impl ToW for $t { fn to_w(&self) -> W { W::I(*self as i128) } }
)* } }
ints!(i8, i16, i32, i64, i128, isize, u8, u16, u32, u64, u128, usize);
impl FromW for f64 { fn from_w(w: W) -> Result<Self, Exc> { match w { W::F(f) => Ok(f), W::I(i) => Ok(i as f64), _ => Err(bad("f64")) } } }
impl FromW for f32 { fn from_w(w: W) -> Result<Self, Exc> { f64::from_w(w).map(|f| f as f32) } }
impl FromW for bool { fn from_w(w: W) -> Result<Self, Exc> { match w { W::B(b) => Ok(b), _ => Err(bad("bool")) } } }
impl FromW for char { fn from_w(w: W) -> Result<Self, Exc> { match w { W::C(c) => Ok(c), W::S(s) if s.chars().count() == 1 => Ok(s.chars().next().unwrap_or('?')), _ => Err(bad("char")) } } }
impl FromW for String { fn from_w(w: W) -> Result<Self, Exc> { match w { W::S(s) | W::O(s) => Ok(s), W::C(c) => Ok(c.to_string()), _ => Err(bad("String")) } } }
impl<T: FromW> FromW for Vec<T> { fn from_w(w: W) -> Result<Self, Exc> { match w { W::L(l) => l.into_iter().map(T::from_w).collect(), W::S(s) => s.chars().map(|c| T::from_w(W::C(c))).collect(), _ => Err(bad("Vec")) } } }
impl<T: FromW> FromW for Option<T> { fn from_w(w: W) -> Result<Self, Exc> { match w { W::N => Ok(None), w => T::from_w(w).map(Some) } } }

trait ToW { fn to_w(&self) -> W; }
impl ToW for f64 { fn to_w(&self) -> W { W::F(*self) } }
impl ToW for f32 { fn to_w(&self) -> W { W::F(*self as f64) } }
impl ToW for bool { fn to_w(&self) -> W { W::B(*self) } }
impl ToW for char { fn to_w(&self) -> W { W::C(*self) } }
impl ToW for str { fn to_w(&self) -> W { W::S(self.to_string()) } }
impl ToW for String { fn to_w(&self) -> W { W::S(self.clone()) } }
impl ToW for () { fn to_w(&self) -> W { W::N } }
impl<T: ToW> ToW for [T] { fn to_w(&self) -> W { W::L(self.iter().map(ToW::to_w).collect()) } }
impl<T: ToW> ToW for Vec<T> { fn to_w(&self) -> W { W::L(self.iter().map(ToW::to_w).collect()) } }
impl<T: ToW, const N: usize> ToW for [T; N] { fn to_w(&self) -> W { W::L(self.iter().map(ToW::to_w).collect()) } }
impl<T: ToW> ToW for Option<T> { fn to_w(&self) -> W { match self { Some(v) => v.to_w(), None => W::N } } }
impl<T: ToW + ?Sized> ToW for &T { fn to_w(&self) -> W { (**self).to_w() } }
impl<T: ToW + ?Sized> ToW for &mut T { fn to_w(&self) -> W { (**self).to_w() } }
impl<T: ToW + ?Sized> ToW for Box<T> { fn to_w(&self) -> W { (**self).to_w() } }
impl<A: ToW, B: ToW> ToW for (A, B) { fn to_w(&self) -> W { W::L(vec![self.0.to_w(), self.1.to_w()]) } }
impl<A: ToW, B: ToW, C: ToW> ToW for (A, B, C) { fn to_w(&self) -> W { W::L(vec![self.0.to_w(), self.1.to_w(), self.2.to_w()]) } }
impl<K: ToW, V: ToW> ToW for std::collections::HashMap<K, V> { fn to_w(&self) -> W { W::M(self.iter().map(|(k, v)| (k.to_w(), v.to_w())).collect()) } }
impl<K: ToW, V: ToW> ToW for std::collections::BTreeMap<K, V> { fn to_w(&self) -> W { W::M(self.iter().map(|(k, v)| (k.to_w(), v.to_w())).collect()) } }
impl<T: ToW> ToW for std::collections::BTreeSet<T> { fn to_w(&self) -> W { W::L(self.iter().map(ToW::to_w).collect()) } }

trait Out { fn out(self) -> Result<W, Exc>; }
impl<T: ToW, E: std::fmt::Debug> Out for Result<T, E> { fn out(self) -> Result<W, Exc> { match self { Ok(v) => Ok(v.to_w()), Err(e) => Err(Exc("Err".into(), format!("{e:?}"))) } } }
struct Plain<T>(T);
impl<T: ToW> Out for Plain<T> { fn out(self) -> Result<W, Exc> { Ok(self.0.to_w()) } }

fn arg<'a, T: FromW>(it: &mut impl Iterator<Item = &'a str>) -> Result<T, Exc> { T::from_w(parse(it)?) }

fn panic_text(p: Box<dyn std::any::Any + Send>) -> String {
    if let Some(s) = p.downcast_ref::<&str>() { s.to_string() } else if let Some(s) = p.downcast_ref::<String>() { s.clone() } else { "panic".into() }
}

fn main() {
    std::panic::set_hook(Box::new(|_| {}));
    let stdin = std::io::stdin();
    for line in stdin.lock().lines() {
        let Ok(line) = line else { break };
        let mut it = line.split_ascii_whitespace();
        if it.next() != Some("call") { continue; }
        let name = it.next().unwrap_or("").to_string();
        let _ = it.next();
        let res = std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| dispatch(&name, &mut it)));
        let resp = match res {
            Ok(Ok(w)) => { let mut s = String::from("ok "); emit(&w, &mut s); s }
            Ok(Err(Exc(t, m))) => format!("exc {} {}", hx(&t), hx(&m)),
            Err(p) => format!("exc {} {}", hx("panic"), hx(&panic_text(p))),
        };
        let mut out = std::io::stdout().lock();
        let _ = write!(out, "\n\u{1e}SC {resp}\n");
        let _ = out.flush();
    }
}
"#;

/// Owned Rust type to decode into, and how to pass it.
fn rust_arg(raw: &str) -> (String, &'static str) {
    let t = crate::sig::strip_lifetimes(raw);
    let t = t.trim();
    let (owned, pass) = if let Some(r) = t.strip_prefix("&mut") {
        (r.trim().to_string(), "&mut ")
    } else if let Some(r) = t.strip_prefix('&') {
        (r.trim().to_string(), "&")
    } else {
        (t.to_string(), "")
    };
    let owned = if owned == "str" {
        "String".to_string()
    } else if let Some(inner) = owned.strip_prefix('[').and_then(|r| r.strip_suffix(']')) {
        format!("Vec<{inner}>")
    } else {
        owned
    };
    (owned, pass)
}

fn rust_harness_source(protos: &[&Proto], krate: &str) -> String {
    let mut s = String::from(RUST_HARNESS_HEAD);
    s.push_str("\nfn dispatch<'a>(name: &str, it: &mut impl Iterator<Item = &'a str>) -> Result<W, Exc> {\n    match name {\n");
    for p in protos {
        s.push_str(&format!("        \"{}\" => {{\n", p.name));
        let mut args = Vec::new();
        for (i, prm) in p.params.iter().enumerate() {
            let (owned, pass) = rust_arg(&prm.raw);
            s.push_str(&format!("            let mut a{i}: {owned} = arg(it)?;\n"));
            args.push(format!("{pass}a{i}"));
        }
        let call = format!("{krate}::{}({})", p.name, args.join(", "));
        let unit_ret = matches!(p.ret_raw.trim(), "" | "()");
        let mutated = p
            .params
            .iter()
            .position(|x| x.raw.trim_start().starts_with("&mut"));
        if let (true, Some(i)) = (unit_ret, mutated) {
            // In-place functions: the value is the mutated argument.
            s.push_str(&format!(
                "            {call};\n            Plain(&a{i}).out()\n"
            ));
        } else if p.ret_raw.trim_start().starts_with("Result") {
            s.push_str(&format!("            {call}.out()\n"));
        } else {
            s.push_str(&format!("            Plain({call}).out()\n"));
        }
        s.push_str("        }\n");
    }
    s.push_str("        _ => Err(Exc(\"Protocol\".into(), format!(\"unknown function {name}\"))),\n    }\n}\n");
    s
}

fn build_rust_harness(
    ctx: &Ctx,
    protos: &[&Proto],
    names: BTreeSet<String>,
    timeout: Duration,
) -> Result<(Harness, String), BuildError> {
    let pkg = cargo_package(ctx.ws)?;
    let dir = ctx.cache.join("harness");
    fs::create_dir_all(dir.join("src")).map_err(|e| BuildError::new(e.to_string()))?;
    let ws = ctx.ws.to_string_lossy().replace('\\', "/");
    fs::write(
        dir.join("Cargo.toml"),
        format!("[package]\nname = \"skillcheck_harness\"\nversion = \"0.0.0\"\nedition = \"2024\"\npublish = false\n\n[dependencies]\nstudent = {{ package = \"{pkg}\", path = \"{ws}\" }}\n\n[workspace]\n"),
    )
    .map_err(|e| BuildError::new(e.to_string()))?;
    fs::write(
        dir.join("src").join("main.rs"),
        rust_harness_source(protos, "student"),
    )
    .map_err(|e| BuildError::new(e.to_string()))?;
    let mut c = Command::new("cargo");
    c.args(["build", "--quiet"])
        .current_dir(&dir)
        .envs(ctx.env)
        .env("CARGO_TARGET_DIR", ctx.cache.join("target"));
    let out = run::run(&mut c, None, BUILD_TIMEOUT).map_err(BuildError::new)?;
    if !out.success {
        let rel = ctx.rel(&out.stderr);
        let msg = if rel.contains("no library targets found") || rel.contains("lib target") {
            "the crate has no library target (src/lib.rs)".to_string()
        } else if let Some(n) = rel
            .lines()
            .find_map(|l| l.split("cannot find function `").nth(1))
            .and_then(|r| r.split('`').next())
        {
            format!("function `{n}` is missing or not `pub` at the crate root")
        } else {
            first_error(&rel).unwrap_or("harness build failed".into())
        };
        return Err(BuildError {
            msg,
            log: out.stderr,
        });
    }
    let bin = ctx
        .cache
        .join("target")
        .join("debug")
        .join(exe("skillcheck_harness"));
    Ok((
        Harness::new(
            ctx.wrapped(vec![bin.to_string_lossy().into_owned()]),
            ctx.ws.to_path_buf(),
            ctx.env.clone(),
            timeout,
            names,
        ),
        String::new(),
    ))
}

// ----------------------------------------------------------------------- stubs

/// Empty bodies for the mandatory functions, so a fresh workspace compiles.
pub fn stub_source(lang: &str, protos: &[Proto]) -> Option<String> {
    let mut s = String::new();
    match lang {
        "c" => {
            for p in protos {
                let sig = p.sig_text.trim().trim_end_matches(';');
                s.push_str(sig);
                s.push_str("\n{\n");
                for prm in &p.params {
                    s.push_str(&format!("    (void){};\n", prm.name));
                }
                match &p.ret {
                    Ty::Void => {}
                    Ty::Float => s.push_str("    return 0.0;\n"),
                    _ if p.ret_raw.contains('*') => s.push_str("    return 0;\n"),
                    Ty::Other(_) => s.push_str(&format!("    return ({}){{0}};\n", p.ret_raw)),
                    _ => s.push_str("    return 0;\n"),
                }
                s.push_str("}\n\n");
            }
        }
        "python" => {
            for p in protos {
                let params: Vec<String> = p
                    .params
                    .iter()
                    .map(|x| {
                        if x.has_default {
                            format!("{}=None", x.name)
                        } else {
                            x.name.clone()
                        }
                    })
                    .collect();
                s.push_str(&format!(
                    "def {}({}):\n    raise NotImplementedError\n\n\n",
                    p.short_name(),
                    params.join(", ")
                ));
            }
        }
        "rust" => {
            for p in protos {
                let sig = p
                    .sig_text
                    .trim()
                    .trim_end_matches(';')
                    .trim_end_matches('{')
                    .trim();
                let sig = sig.strip_prefix("pub ").unwrap_or(sig);
                let names: Vec<&str> = p.params.iter().map(|x| x.name.as_str()).collect();
                let unused = match names.len() {
                    0 => String::new(),
                    1 => format!("    let _ = {};\n", names[0]),
                    _ => format!("    let _ = ({});\n", names.join(", ")),
                };
                s.push_str(&format!(
                    "/// TODO\npub {sig} {{\n{unused}    todo!()\n}}\n\n"
                ));
            }
        }
        _ => return None,
    }
    Some(s.trim_end().to_string() + "\n")
}

/// A C header declaring `protos`, with an include guard.
pub fn c_header(path: &str, protos: &[Proto]) -> String {
    let guard: String = path
        .rsplit('/')
        .next()
        .unwrap_or(path)
        .chars()
        .map(|c| {
            if c.is_ascii_alphanumeric() {
                c.to_ascii_uppercase()
            } else {
                '_'
            }
        })
        .collect();
    let mut s = format!("#ifndef {guard}\n#define {guard}\n\n#include <stddef.h>\n\n");
    for p in protos {
        s.push_str(&format!("{};\n", p.sig_text.trim().trim_end_matches(';')));
    }
    s.push_str("\n#endif\n");
    s
}

pub fn work_paths(cache_root: &Path, key: &str) -> (PathBuf, PathBuf, PathBuf) {
    let base = cache_root.join(key);
    (base.join("ws"), base.join("build"), base)
}
