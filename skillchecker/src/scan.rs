//! Source scanning: comment/string stripping, the Allowed (whitelist) check
//! and the prototype finder. Text-based by design: fast, no toolchain needed,
//! and good enough for students who play the game.

use crate::sig::{self, Proto};
use regex::Regex;
use std::collections::{BTreeMap, BTreeSet};
use std::fs;
use std::path::{Path, PathBuf};

pub struct Source {
    pub rel: String,
    pub raw: String,
    /// Comments and literals blanked, newlines and byte offsets preserved.
    pub code: String,
}

pub struct Violation {
    pub what: String,
    pub at: String,
}

pub const IGNORED_DIRS: &[&str] = &[
    ".git",
    "target",
    "node_modules",
    "__pycache__",
    ".skillchecker",
    ".vscode",
    ".idea",
];

/// Every file under `dir`, as `/`-separated relative paths, sorted.
pub fn walk(dir: &Path) -> Vec<String> {
    fn go(base: &Path, dir: &Path, out: &mut Vec<String>) {
        let Ok(rd) = fs::read_dir(dir) else { return };
        for e in rd.flatten() {
            let p = e.path();
            let name = e.file_name().to_string_lossy().into_owned();
            if p.is_dir() {
                if !IGNORED_DIRS.contains(&name.as_str()) {
                    go(base, &p, out);
                }
            } else if let Ok(rel) = p.strip_prefix(base) {
                out.push(rel.to_string_lossy().replace('\\', "/"));
            }
        }
    }
    let mut out = Vec::new();
    go(dir, dir, &mut out);
    out.sort();
    out
}

pub fn sources(lang: &str, ws: &Path) -> Vec<Source> {
    sources_ext(
        lang,
        ws,
        &SrcOpts::from_config(&crate::defaults::preset(lang)),
    )
}

/// Source settings from the course's `languages:` block.
#[derive(Default, Clone)]
pub struct SrcOpts {
    /// Extra source extensions, without the dot.
    pub exts: Vec<String>,
    /// Line-comment marker for languages without a preset (`#`, `--`, `;`).
    pub comment: Option<String>,
}

impl SrcOpts {
    pub fn from_config(c: &crate::unit::LangConfig) -> SrcOpts {
        SrcOpts {
            exts: c.extensions.clone().unwrap_or_default(),
            comment: c.comment.clone(),
        }
    }
}

/// Languages with a dedicated comment/string stripper.
fn has_stripper(lang: &str) -> bool {
    matches!(
        crate::defaults::canonical(lang),
        "c" | "cpp"
            | "python"
            | "rust"
            | "bash"
            | "ruby"
            | "javascript"
            | "typescript"
            | "go"
            | "java"
    )
}

/// Source files of lang; xtra adds extensions (from the course's
/// languages: block), which matters for languages without a preset.
pub fn sources_ext(lang: &str, ws: &Path, opts: &SrcOpts) -> Vec<Source> {
    let exts: Vec<String> = if opts.exts.is_empty() {
        crate::defaults::preset(lang).extensions.unwrap_or_default()
    } else {
        opts.exts
            .iter()
            .map(|e| e.trim_start_matches('.').to_string())
            .collect()
    };
    walk(ws)
        .into_iter()
        .filter(|rel| {
            exts.is_empty()
                || Path::new(rel)
                    .extension()
                    .is_some_and(|e| exts.iter().any(|x| *x == e.to_string_lossy()))
        })
        .filter_map(|rel| {
            let raw = fs::read_to_string(ws.join(&rel)).ok()?;
            let code = match &opts.comment {
                Some(marker) if !has_stripper(lang) => strip_line_comments(&raw, marker),
                _ => strip(lang, &raw),
            };
            Some(Source { rel, raw, code })
        })
        .collect()
}

pub fn line_of(text: &str, idx: usize) -> usize {
    text[..idx.min(text.len())]
        .bytes()
        .filter(|&b| b == b'\n')
        .count()
        + 1
}

// ------------------------------------------------------------------ stripping

fn blank(b: &mut [u8], from: usize, to: usize) {
    let to = to.min(b.len());
    for x in &mut b[from.min(to)..to] {
        if *x != b'\n' {
            *x = b' ';
        }
    }
}

fn is_ident_byte(c: u8) -> bool {
    c.is_ascii_alphanumeric() || c == b'_'
}

pub fn strip(lang: &str, src: &str) -> String {
    match lang {
        "python" => strip_hash(src, true, false),
        "bash" | "sh" | "shell" => strip_hash(src, false, true),
        "ruby" => strip_hash(src, false, false),
        "rust" => strip_c_like(src, true, false),
        "javascript" | "js" | "typescript" | "ts" | "go" => strip_c_like(src, false, true),
        _ => strip_c_like(src, false, false),
    }
}

/// C, C++, Rust, Java, JS, Go: `//`, `/* */`, "strings", 'chars'.
/// Rust: raw strings and lifetimes. JS/Go: backtick strings.
fn strip_c_like(src: &str, rust: bool, backtick: bool) -> String {
    strip_c_like_ex(src, rust, backtick, false)
}

fn strip_c_like_ex(src: &str, rust: bool, backtick: bool, keep_strings: bool) -> String {
    let mut b = src.as_bytes().to_vec();
    let n = b.len();
    let mut i = 0;
    while i < n {
        match b[i] {
            b'/' if i + 1 < n && b[i + 1] == b'/' => {
                let s = i;
                while i < n && b[i] != b'\n' {
                    i += 1;
                }
                blank(&mut b, s, i);
            }
            b'/' if i + 1 < n && b[i + 1] == b'*' => {
                let s = i;
                let mut depth = 0;
                while i < n {
                    if b[i] == b'/' && i + 1 < n && b[i + 1] == b'*' {
                        depth += 1;
                        i += 2;
                    } else if b[i] == b'*' && i + 1 < n && b[i + 1] == b'/' {
                        depth -= 1;
                        i += 2;
                        if depth == 0 || !rust {
                            break;
                        }
                    } else {
                        i += 1;
                    }
                }
                blank(&mut b, s, i);
            }
            q @ (b'"' | b'`') if q == b'"' || backtick => {
                let s = i;
                i += 1;
                while i < n && b[i] != q {
                    if b[i] == b'\\' {
                        i += 1;
                    }
                    i += 1;
                }
                i = (i + 1).min(n);
                if !keep_strings {
                    blank(&mut b, s + 1, i.saturating_sub(1));
                }
            }
            b'r' if rust
                && (i == 0
                    || !is_ident_byte(b[i - 1])
                    || (b[i - 1] == b'b' && (i < 2 || !is_ident_byte(b[i - 2])))) =>
            {
                let mut j = i + 1;
                let mut hashes = 0;
                while j < n && b[j] == b'#' {
                    hashes += 1;
                    j += 1;
                }
                if j < n && b[j] == b'"' {
                    let s = j;
                    j += 1;
                    while j < n {
                        if b[j] == b'"'
                            && (0..hashes).all(|k| j + 1 + k < n && b[j + 1 + k] == b'#')
                        {
                            j += 1 + hashes;
                            break;
                        }
                        j += 1;
                    }
                    if !keep_strings {
                        blank(&mut b, s + 1, j.saturating_sub(1 + hashes));
                    }
                    i = j;
                } else {
                    i += 1;
                }
            }
            b'\'' => {
                let s = i;
                if rust {
                    // Char literal only if it closes within a few bytes; else lifetime.
                    let close = if i + 1 < n && b[i + 1] == b'\\' {
                        (i + 2..(i + 12).min(n)).find(|&j| b[j] == b'\'')
                    } else {
                        (i + 2..(i + 6).min(n))
                            .find(|&j| b[j] == b'\'')
                            .filter(|&j| {
                                std::str::from_utf8(&b[i + 1..j])
                                    .is_ok_and(|t| t.chars().count() == 1)
                            })
                    };
                    match close {
                        Some(j) => {
                            if !keep_strings {
                                blank(&mut b, s + 1, j);
                            }
                            i = j + 1;
                        }
                        None => i += 1,
                    }
                } else {
                    i += 1;
                    while i < n && b[i] != b'\'' && b[i] != b'\n' {
                        if b[i] == b'\\' {
                            i += 1;
                        }
                        i += 1;
                    }
                    if !keep_strings {
                        blank(&mut b, s + 1, i);
                    }
                    i = (i + 1).min(n);
                }
            }
            _ => i += 1,
        }
    }
    String::from_utf8_lossy(&b).into_owned()
}

/// Python, Ruby, shell: `#` comments, quotes; Python triple quotes; shell
/// comments only at word start, and heredoc bodies.
fn strip_hash(src: &str, triple: bool, shell: bool) -> String {
    strip_hash_ex(src, triple, shell, false)
}

fn strip_hash_ex(src: &str, triple: bool, shell: bool, keep_strings: bool) -> String {
    let mut b = src.as_bytes().to_vec();
    let n = b.len();
    let mut i = 0;
    let mut heredoc: Option<String> = None;
    while i < n {
        if let Some(delim) = heredoc.clone() {
            // Skip to the next line and blank lines until the delimiter.
            while i < n && b[i] != b'\n' {
                i += 1;
            }
            let mut start = i + 1;
            while start < n {
                let end = (start..n).find(|&j| b[j] == b'\n').unwrap_or(n);
                let line = String::from_utf8_lossy(&b[start..end]).trim().to_string();
                if line == delim {
                    blank(&mut b, i, end);
                    i = end;
                    break;
                }
                start = end + 1;
                if start >= n {
                    blank(&mut b, i, n);
                    i = n;
                }
            }
            heredoc = None;
            continue;
        }
        match b[i] {
            b'#' if !shell || i == 0 || matches!(b[i - 1], b' ' | b'\t' | b'\n' | b';') => {
                let s = i;
                while i < n && b[i] != b'\n' {
                    i += 1;
                }
                blank(&mut b, s, i);
            }
            b'\\' if shell => i += 2,
            q @ (b'"' | b'\'') => {
                let s = i;
                let is_triple = triple && i + 2 < n && b[i + 1] == q && b[i + 2] == q;
                if is_triple {
                    i += 3;
                    while i + 2 < n && !(b[i] == q && b[i + 1] == q && b[i + 2] == q) {
                        if b[i] == b'\\' {
                            i += 1;
                        }
                        i += 1;
                    }
                    i = (i + 3).min(n);
                    if !keep_strings {
                        blank(&mut b, s + 3, i.saturating_sub(3));
                    }
                } else if shell && q == b'"' {
                    // Keep $(...) inside double quotes: it runs commands.
                    i += 1;
                    let mut depth = 0;
                    while i < n && !(b[i] == b'"' && depth == 0) {
                        if b[i] == b'\\' {
                            if depth == 0 && !keep_strings {
                                blank(&mut b, i, i + 2);
                            }
                            i += 2;
                            continue;
                        }
                        if b[i] == b'$' && i + 1 < n && b[i + 1] == b'(' {
                            depth += 1;
                            i += 2;
                            continue;
                        }
                        if depth > 0 && b[i] == b')' {
                            depth -= 1;
                        } else if depth == 0 && !keep_strings && b[i] != b'\n' {
                            b[i] = b' ';
                        }
                        i += 1;
                    }
                    i = (i + 1).min(n);
                } else {
                    i += 1;
                    while i < n && b[i] != q {
                        if b[i] == b'\\' && !(shell && q == b'\'') {
                            i += 1;
                        }
                        if !shell && i < n && b[i] == b'\n' {
                            break;
                        }
                        i += 1;
                    }
                    i = (i + 1).min(n);
                    if !keep_strings {
                        blank(&mut b, s + 1, i.saturating_sub(1));
                    }
                }
            }
            b'<' if shell && i + 1 < n && b[i + 1] == b'<' && !(i + 2 < n && b[i + 2] == b'<') => {
                let mut j = i + 2;
                if j < n && b[j] == b'-' {
                    j += 1;
                }
                while j < n && b[j] == b' ' {
                    j += 1;
                }
                let s = j;
                while j < n && !matches!(b[j], b' ' | b'\n' | b';' | b'|' | b'&' | b'>') {
                    j += 1;
                }
                let word = String::from_utf8_lossy(&b[s..j])
                    .trim_matches(|c| c == '\'' || c == '"')
                    .to_string();
                i = j;
                if !word.is_empty() {
                    heredoc = Some(word);
                }
            }
            _ => i += 1,
        }
    }
    String::from_utf8_lossy(&b).into_owned()
}

/// Generic stripper: blanks `marker` to end of line when it is outside a
/// quoted string. String contents are kept: many languages import by string.
pub fn strip_line_comments(src: &str, marker: &str) -> String {
    let mut out = String::with_capacity(src.len());
    for line in src.split_inclusive('\n') {
        let mut quote: Option<char> = None;
        let mut prev = ' ';
        let mut cut = false;
        for (i, c) in line.char_indices() {
            if cut {
                out.push(if c == '\n' { '\n' } else { ' ' });
                continue;
            }
            match quote {
                Some(q) if c == q && prev != '\\' => {
                    quote = None;
                    out.push(c);
                }
                Some(_) => out.push(c),
                None if c == '"' || c == '\'' => {
                    quote = Some(c);
                    out.push(c);
                }
                None if line[i..].starts_with(marker) => {
                    cut = true;
                    out.push(' ');
                }
                None => out.push(c),
            }
            prev = c;
        }
    }
    out
}

// ------------------------------------------------------------- allowed check

fn allowed_by(path: &str, allowed: &BTreeSet<String>, seps: &[&str]) -> bool {
    allowed
        .iter()
        .any(|e| path == e || seps.iter().any(|s| path.starts_with(&format!("{e}{s}"))))
}

/// The whitelist and the forbidden items, both matched as prefixes: `std`
/// covers `std::mem::take`, and forbidding `std::mem::swap` removes just that.
struct Rules {
    allowed: BTreeSet<String>,
    forbidden: BTreeSet<String>,
}

impl Rules {
    fn ok(&self, name: &str, seps: &[&str]) -> bool {
        allowed_by(name, &self.allowed, seps) && !allowed_by(name, &self.forbidden, seps)
    }
}

pub struct AllowedReport {
    pub checked: usize,
    pub violations: Vec<Violation>,
}

pub fn check_allowed(
    lang: &str,
    ws: &Path,
    allowed: &[String],
    extra: &[String],
    opts: &SrcOpts,
) -> Result<AllowedReport, String> {
    check_rules(lang, ws, allowed, &[], extra, opts)
}

/// The student's own tests (a top-level `tests/` folder) may use anything:
/// the rules are about the exercise's code.
fn is_test_source(rel: &str) -> bool {
    rel.starts_with("tests/")
}

pub fn check_rules(
    lang: &str,
    ws: &Path,
    allowed: &[String],
    forbidden: &[String],
    extra: &[String],
    opts: &SrcOpts,
) -> Result<AllowedReport, String> {
    let set =
        |l: &[String]| -> BTreeSet<String> { l.iter().map(|s| s.trim().to_string()).collect() };
    let allowed = Rules {
        allowed: set(allowed),
        forbidden: set(forbidden),
    };
    let srcs: Vec<Source> = sources_ext(lang, ws, opts)
        .into_iter()
        .filter(|s| !is_test_source(&s.rel))
        .collect();
    let mut r = AllowedReport {
        checked: 0,
        violations: Vec::new(),
    };
    match lang {
        "c" | "cpp" | "c++" => allowed_c(ws, &srcs, &allowed, &mut r),
        "python" => allowed_python(ws, &srcs, &allowed, &mut r),
        "rust" => allowed_rust(ws, &srcs, &allowed, &mut r),
        "bash" | "sh" | "shell" => allowed_bash(ws, &srcs, &allowed, &mut r),
        _ => {}
    }
    if !extra.is_empty() {
        allowed_regex(lang, ws, &srcs, &allowed, extra, &mut r)?;
    }
    // One report per name, the first place it appears.
    let mut seen = BTreeSet::new();
    r.violations.retain(|v| seen.insert(v.what.clone()));
    Ok(r)
}

const C_KEYWORDS: &[&str] = &[
    "if",
    "while",
    "for",
    "switch",
    "return",
    "sizeof",
    "_Alignof",
    "alignof",
    "_Generic",
    "_Static_assert",
    "static_assert",
    "case",
    "do",
    "else",
    "typeof",
    "__typeof__",
    "__attribute__",
    "__asm__",
    "asm",
    "defined",
    "void",
    "char",
    "short",
    "int",
    "long",
    "float",
    "double",
    "signed",
    "unsigned",
    "_Bool",
    "bool",
    "const",
    "volatile",
    "struct",
    "union",
    "enum",
    "goto",
    "__declspec",
];

struct CTok {
    text: String,
    line: usize,
}

fn c_toks(code: &str) -> Vec<CTok> {
    let mut out = Vec::new();
    let mut line = 1;
    let cs: Vec<char> = code.chars().collect();
    let mut i = 0;
    while i < cs.len() {
        let c = cs[i];
        if c == '\n' {
            line += 1;
            i += 1;
        } else if c.is_whitespace() {
            i += 1;
        } else if c.is_alphanumeric() || c == '_' {
            let s = i;
            while i < cs.len() && (cs[i].is_alphanumeric() || cs[i] == '_') {
                i += 1;
            }
            out.push(CTok {
                text: cs[s..i].iter().collect(),
                line,
            });
        } else {
            let two: String = cs[i..(i + 2).min(cs.len())].iter().collect();
            if two == "->" || two == "::" {
                out.push(CTok { text: two, line });
                i += 2;
            } else {
                out.push(CTok {
                    text: c.to_string(),
                    line,
                });
                i += 1;
            }
        }
    }
    out
}

/// Blanks preprocessor lines (keeps line count).
fn without_preprocessor(code: &str) -> String {
    code.lines()
        .map(|l| {
            if l.trim_start().starts_with('#') {
                ""
            } else {
                l
            }
        })
        .collect::<Vec<_>>()
        .join("\n")
}

struct CDef {
    name: String,
    norm: String,
    line: usize,
}

fn c_definitions(code: &str) -> Vec<CDef> {
    let toks = c_toks(&without_preprocessor(code));
    let texts: Vec<String> = toks.iter().map(|t| t.text.clone()).collect();
    let mut defs = Vec::new();
    let mut depth = 0i32;
    let mut i = 0;
    while i < toks.len() {
        let t = texts[i].as_str();
        match t {
            "{" => depth += 1,
            "}" => depth -= 1,
            "(" if depth == 0
                && i > 0
                && is_ident(&texts[i - 1])
                && !C_KEYWORDS.contains(&texts[i - 1].as_str()) =>
            {
                if let Some(close) = sig::matching(&texts, i)
                    && texts.get(close + 1).map(String::as_str) == Some("{")
                {
                    let name = texts[i - 1].clone();
                    let mut start = i - 1;
                    while start > 0 && !matches!(texts[start - 1].as_str(), ";" | "}" | "{" | ")") {
                        start -= 1;
                    }
                    let p = sig::c_from_tokens(&texts[start..i - 1], &name, &texts[i + 1..close]);
                    defs.push(CDef {
                        name,
                        norm: p.norm,
                        line: toks[i].line,
                    });
                    i = close;
                }
            }
            _ => {}
        }
        i += 1;
    }
    defs
}

fn is_ident(t: &str) -> bool {
    t.chars()
        .next()
        .is_some_and(|c| c.is_alphabetic() || c == '_')
}

fn allowed_c(ws: &Path, srcs: &[Source], allowed: &Rules, r: &mut AllowedReport) {
    let own_files: BTreeSet<String> = walk(ws)
        .iter()
        .map(|p| p.rsplit('/').next().unwrap_or(p).to_string())
        .collect();
    let mut defined: BTreeSet<String> = BTreeSet::new();
    let mut fnptrs: BTreeSet<String> = BTreeSet::new();
    let define_re = Regex::new(r"^\s*#\s*define\s+([A-Za-z_]\w*)").expect("regex");
    for s in srcs {
        defined.extend(c_definitions(&s.code).into_iter().map(|d| d.name));
        for line in s.raw.lines() {
            if let Some(c) = define_re.captures(line) {
                defined.insert(c[1].to_string());
            }
        }
        let toks = c_toks(&s.code);
        for w in toks.windows(4) {
            if w[0].text == "(" && w[1].text == "*" && is_ident(&w[2].text) && w[3].text == ")" {
                fnptrs.insert(w[2].text.clone());
            }
        }
    }
    let include_re = Regex::new(r#"^\s*#\s*include\s*([<"])([^>"]+)[>"]"#).expect("regex");
    for s in srcs {
        for (n, (raw, code)) in s.raw.lines().zip(s.code.lines()).enumerate() {
            if !code.trim_start().starts_with('#') {
                continue;
            }
            let Some(c) = include_re.captures(raw) else {
                continue;
            };
            let header = c[2].trim().to_string();
            let base = header.rsplit('/').next().unwrap_or(&header).to_string();
            r.checked += 1;
            // Quoted includes are the student's own headers by convention.
            let own = &c[1] == "\"" || own_files.contains(&base);
            if !own && !allowed.ok(&header, &[]) && !allowed.ok(&base, &[]) {
                r.violations.push(Violation {
                    what: format!("#include <{header}>"),
                    at: format!("{}:{}", s.rel, n + 1),
                });
            }
        }
        let toks = c_toks(&without_preprocessor(&s.code));
        for i in 0..toks.len().saturating_sub(1) {
            let t = &toks[i].text;
            if !is_ident(t) || toks[i + 1].text != "(" || C_KEYWORDS.contains(&t.as_str()) {
                continue;
            }
            if i > 0 && matches!(toks[i - 1].text.as_str(), "." | "->") {
                continue;
            }
            if defined.contains(t) || fnptrs.contains(t) {
                continue;
            }
            // A declaration like `char *foo(int);` that has no body is still a call target.
            r.checked += 1;
            if !allowed.ok(t, &[]) {
                r.violations.push(Violation {
                    what: format!("{t}()"),
                    at: format!("{}:{}", s.rel, toks[i].line),
                });
            }
        }
    }
}

fn own_python_modules(ws: &Path) -> BTreeSet<String> {
    let mut own = BTreeSet::new();
    for rel in walk(ws) {
        let parts: Vec<&str> = rel.split('/').collect();
        if let Some(stem) = rel.strip_suffix(".py") {
            own.insert(stem.rsplit('/').next().unwrap_or(stem).to_string());
        }
        if parts.len() > 1 {
            own.insert(parts[0].to_string());
        }
    }
    own
}

fn allowed_python(ws: &Path, srcs: &[Source], allowed: &Rules, r: &mut AllowedReport) {
    let own = own_python_modules(ws);
    let import_re = Regex::new(r"(?m)^[ \t]*import[ \t]+([^\n]+)").expect("regex");
    let from_re = Regex::new(r"(?m)^[ \t]*from[ \t]+([\w.]+)[ \t]+import[ \t]+(\([^)]*\)|[^\n]+)")
        .expect("regex");
    let check = |path: String, s: &Source, idx: usize, r: &mut AllowedReport| {
        let root = path.split('.').next().unwrap_or(&path).to_string();
        r.checked += 1;
        if path.starts_with('.') || own.contains(&root) || root == "__future__" {
            return;
        }
        if !allowed.ok(&path, &["."]) {
            r.violations.push(Violation {
                what: format!("import {path}"),
                at: format!("{}:{}", s.rel, line_of(&s.code, idx)),
            });
        }
    };
    for s in srcs {
        for c in import_re.captures_iter(&s.code) {
            let at = c.get(0).map_or(0, |m| m.start());
            for item in c[1].split(',') {
                let name = item.split_whitespace().next().unwrap_or("").to_string();
                if !name.is_empty() {
                    check(name, s, at, r);
                }
            }
        }
        for c in from_re.captures_iter(&s.code) {
            let at = c.get(0).map_or(0, |m| m.start());
            let module = c[1].to_string();
            if module.starts_with('.') {
                r.checked += 1;
                continue;
            }
            let names = c[2].trim_matches(|ch| ch == '(' || ch == ')');
            for item in names.split(',') {
                let name = item.split_whitespace().next().unwrap_or("");
                if name.is_empty() || name == "\\" {
                    continue;
                }
                let path = if name == "*" {
                    module.clone()
                } else {
                    format!("{module}.{name}")
                };
                check(path, s, at, r);
            }
        }
    }
}

/// Expands a `use` tree (`std::{fmt, io::Read as R}`) into full paths.
fn expand_use(prefix: &str, tree: &str, out: &mut Vec<String>) {
    let t = tree.trim().trim_start_matches("::");
    let join = |a: &str, b: &str| {
        if a.is_empty() {
            b.to_string()
        } else {
            format!("{a}::{b}")
        }
    };
    if let Some(open) = t.find('{') {
        let base = t[..open].trim_end_matches("::").trim();
        let inner = t[open + 1..].trim_end().trim_end_matches('}');
        let full = join(prefix, base);
        let mut depth = 0;
        let mut cur = String::new();
        for c in inner.chars() {
            match c {
                '{' => {
                    depth += 1;
                    cur.push(c);
                }
                '}' => {
                    depth -= 1;
                    cur.push(c);
                }
                ',' if depth == 0 => {
                    expand_use(&full, &std::mem::take(&mut cur), out);
                }
                c => cur.push(c),
            }
        }
        if !cur.trim().is_empty() {
            expand_use(&full, &cur, out);
        }
        return;
    }
    let item = t.split(" as ").next().unwrap_or(t).trim();
    if item.is_empty() {
        return;
    }
    match item {
        "self" => out.push(prefix.to_string()),
        "*" => out.push(prefix.to_string()),
        _ => out.push(join(prefix, item)),
    }
}

fn cargo_package_name(ws: &Path) -> Option<String> {
    let text = fs::read_to_string(ws.join("Cargo.toml")).ok()?;
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
            return Some(v.trim().trim_matches('"').to_string());
        }
    }
    None
}

pub fn cargo_dependencies(ws: &Path) -> Vec<(String, usize)> {
    let Ok(text) = fs::read_to_string(ws.join("Cargo.toml")) else {
        return Vec::new();
    };
    let mut out = Vec::new();
    let mut in_deps = false;
    for (i, line) in text.lines().enumerate() {
        let t = line.trim();
        if t.starts_with('[') {
            let h = t.trim_matches(|c| c == '[' || c == ']').trim();
            if let Some(name) = h.split_once("dependencies.").map(|(_, n)| n) {
                out.push((name.to_string(), i + 1));
                in_deps = false;
            } else {
                in_deps = h.ends_with("dependencies");
            }
        } else if in_deps
            && !t.is_empty()
            && !t.starts_with('#')
            && let Some((k, _)) = t.split_once('=')
        {
            out.push((k.trim().trim_matches('"').to_string(), i + 1));
        }
    }
    out
}

fn allowed_rust(ws: &Path, srcs: &[Source], allowed: &Rules, r: &mut AllowedReport) {
    let mut own: BTreeSet<String> = ["crate", "self", "super", "Self"]
        .iter()
        .map(|s| s.to_string())
        .collect();
    if let Some(p) = cargo_package_name(ws) {
        own.insert(p.replace('-', "_"));
    }
    let mod_re = Regex::new(r"\bmod\s+([A-Za-z_]\w*)").expect("regex");
    for s in srcs {
        for c in mod_re.captures_iter(&s.code) {
            own.insert(c[1].to_string());
        }
    }
    for (dep, line) in cargo_dependencies(ws) {
        r.checked += 1;
        if !allowed.ok(&dep, &[]) && !allowed.ok(&dep.replace('-', "_"), &[]) {
            r.violations.push(Violation {
                what: format!("crate `{dep}`"),
                at: format!("Cargo.toml:{line}"),
            });
        }
    }
    let use_re = Regex::new(r"\buse\s+([^;]+);").expect("regex");
    let path_re = Regex::new(r"\b(?:std|core|alloc)(?:::[A-Za-z_]\w*)+").expect("regex");
    let extern_re = Regex::new(r"\bextern\s+crate\s+([A-Za-z_]\w*)").expect("regex");
    let short_re = Regex::new(r"\b([A-Za-z_]\w*)((?:::[A-Za-z_]\w*)+)").expect("regex");
    for s in srcs {
        let mut found: Vec<(String, usize)> = Vec::new();
        for c in use_re.captures_iter(&s.code) {
            let at = c.get(0).map_or(0, |m| m.start());
            let mut paths = Vec::new();
            expand_use("", &c[1], &mut paths);
            found.extend(paths.into_iter().map(|p| (p, at)));
        }
        let use_spans: Vec<(usize, usize)> = use_re
            .find_iter(&s.code)
            .map(|m| (m.start(), m.end()))
            .collect();
        for m in path_re.find_iter(&s.code) {
            if !use_spans.iter().any(|(a, b)| (*a..*b).contains(&m.start())) {
                found.push((m.as_str().to_string(), m.start()));
            }
        }
        for c in extern_re.captures_iter(&s.code) {
            found.push((c[1].to_string(), c.get(0).map_or(0, |m| m.start())));
        }
        // `use std::mem;` then `mem::swap(..)`: the short path is std::mem::swap,
        // which matters for forbidden items (the module itself was checked above).
        let aliases: Vec<(String, String)> = found
            .iter()
            .filter_map(|(p, _)| {
                let (path, name) = match p.split_once(" as ") {
                    Some((path, alias)) => (path.trim(), alias.trim()),
                    None => (p.as_str(), p.rsplit("::").next().unwrap_or(p)),
                };
                (path.contains("::") && name != "self" && name != "*")
                    .then(|| (name.to_string(), path.to_string()))
            })
            .collect();
        for c in short_re.captures_iter(&s.code) {
            let start = c.get(0).map_or(0, |m| m.start());
            if use_spans.iter().any(|(a, b)| (*a..*b).contains(&start)) {
                continue;
            }
            if let Some((_, full)) = aliases.iter().find(|(n, _)| *n == c[1]) {
                let path = format!("{full}{}", &c[2]);
                if allowed_by(&path, &allowed.forbidden, &["::"]) {
                    r.violations.push(Violation {
                        what: path,
                        at: format!("{}:{}", s.rel, line_of(&s.code, start)),
                    });
                }
            }
        }
        for (path, at) in found {
            let root = path.split("::").next().unwrap_or("");
            r.checked += 1;
            if own.contains(root) {
                continue;
            }
            if !allowed.ok(&path, &["::"]) {
                r.violations.push(Violation {
                    what: path.clone(),
                    at: format!("{}:{}", s.rel, line_of(&s.code, at)),
                });
            }
        }
    }
}

const SHELL_BUILTINS: &[&str] = &[
    "echo",
    "printf",
    "read",
    "cd",
    "pwd",
    "test",
    "[",
    "[[",
    "]]",
    "export",
    "local",
    "declare",
    "typeset",
    "readonly",
    "set",
    "unset",
    "shift",
    "return",
    "exit",
    "source",
    ".",
    "eval",
    "exec",
    "trap",
    "wait",
    "true",
    "false",
    ":",
    "let",
    "getopts",
    "mapfile",
    "readarray",
    "alias",
    "unalias",
    "type",
    "command",
    "builtin",
    "hash",
    "pushd",
    "popd",
    "dirs",
    "umask",
    "ulimit",
    "times",
    "jobs",
    "fg",
    "bg",
    "disown",
    "shopt",
    "enable",
    "help",
    "history",
    "caller",
    "break",
    "continue",
    "compgen",
    "complete",
    "kill",
    "if",
    "then",
    "else",
    "elif",
    "fi",
    "for",
    "in",
    "do",
    "done",
    "while",
    "until",
    "case",
    "esac",
    "function",
    "select",
    "time",
    "coproc",
    "!",
    "{",
    "}",
    "((",
    "))",
];

fn allowed_bash(ws: &Path, srcs: &[Source], allowed: &Rules, r: &mut AllowedReport) {
    let own_files: BTreeSet<String> = walk(ws).into_iter().collect();
    let func_re =
        Regex::new(r"(?m)^\s*(?:function\s+)?([A-Za-z_][\w-]*)\s*\(\s*\)").expect("regex");
    let func_kw = Regex::new(r"(?m)\bfunction\s+([A-Za-z_][\w-]*)").expect("regex");
    let mut funcs: BTreeSet<String> = BTreeSet::new();
    for s in srcs {
        for c in func_re
            .captures_iter(&s.code)
            .chain(func_kw.captures_iter(&s.code))
        {
            funcs.insert(c[1].to_string());
        }
    }
    for s in srcs {
        // Tokenize into words and separators, tracking lines.
        let mut words: Vec<(String, usize, bool)> = Vec::new(); // (text, line, is_separator)
        let mut line = 1;
        let mut cur = String::new();
        let bytes: Vec<char> = s.code.chars().collect();
        let mut i = 0;
        let flush = |cur: &mut String, words: &mut Vec<(String, usize, bool)>, line: usize| {
            if !cur.is_empty() {
                words.push((std::mem::take(cur), line, false));
            }
        };
        while i < bytes.len() {
            let c = bytes[i];
            match c {
                '\n' => {
                    flush(&mut cur, &mut words, line);
                    words.push(("\n".into(), line, true));
                    line += 1;
                }
                ' ' | '\t' => flush(&mut cur, &mut words, line),
                // Arithmetic `$(( ))` / `(( ))`: no commands inside.
                '$' | '('
                    if bytes.get(i + 1) == Some(&'(')
                        && (c == '(' || bytes.get(i + 2) == Some(&'(')) =>
                {
                    let mut depth = 0i32;
                    while i < bytes.len() {
                        match bytes[i] {
                            '(' => depth += 1,
                            ')' => {
                                depth -= 1;
                                if depth == 0 {
                                    break;
                                }
                            }
                            '\n' => line += 1,
                            _ => {}
                        }
                        i += 1;
                    }
                    cur.push_str("$arith");
                }
                ';' | '|' | '&' | '(' | ')' | '`' => {
                    flush(&mut cur, &mut words, line);
                    words.push((c.to_string(), line, true));
                }
                '$' if bytes.get(i + 1) == Some(&'(') => {
                    flush(&mut cur, &mut words, line);
                    words.push(("$(".into(), line, true));
                    i += 1;
                }
                _ => cur.push(c),
            }
            i += 1;
        }
        flush(&mut cur, &mut words, line);
        let mut at_cmd = true;
        let mut in_case = 0;
        let mut after_in = false;
        let mut idx = 0;
        while idx < words.len() {
            let (w, ln, sep) = &words[idx];
            if *sep {
                if w != ")" || in_case == 0 {
                    at_cmd = true;
                }
                if w == ")" && in_case > 0 {
                    at_cmd = true;
                }
                idx += 1;
                continue;
            }
            match w.as_str() {
                "case" => {
                    in_case += 1;
                    after_in = false;
                }
                "esac" => in_case -= 1,
                "in" if in_case > 0 => after_in = true,
                _ => {}
            }
            if at_cmd {
                let next_is_paren = words.get(idx + 1).is_some_and(|(n, _, s)| *s && n == ")");
                let is_pattern = in_case > 0 && after_in && next_is_paren;
                let assignment = w.contains('=') && !w.starts_with('=');
                if is_pattern || assignment {
                    idx += 1;
                    continue;
                }
                let keyword_continues = matches!(
                    w.as_str(),
                    "if" | "then"
                        | "else"
                        | "elif"
                        | "do"
                        | "while"
                        | "until"
                        | "!"
                        | "time"
                        | "{"
                        | "exec"
                        | "command"
                );
                let name = w.trim_matches(|c| c == '"' || c == '\'');
                let skip = name.is_empty()
                    || SHELL_BUILTINS.contains(&name)
                    || funcs.contains(name)
                    || name.starts_with('$')
                    || name.starts_with('-')
                    || name.starts_with('[')
                    || name.starts_with('<')
                    || name.starts_with('>')
                    || name.chars().next().is_some_and(|c| c.is_ascii_digit())
                    || name.starts_with("((")
                    || name == "fi"
                    || name == "done"
                    || name == "esac"
                    || name == ";;";
                let local = name.contains('/') && {
                    let rel = name.trim_start_matches("./");
                    own_files.contains(rel)
                };
                if !skip && !local {
                    r.checked += 1;
                    let base = name.rsplit('/').next().unwrap_or(name);
                    if !allowed.ok(name, &[]) && !allowed.ok(base, &[]) {
                        r.violations.push(Violation {
                            what: format!("command `{name}`"),
                            at: format!("{}:{ln}", s.rel),
                        });
                    }
                }
                at_cmd = keyword_continues;
            }
            idx += 1;
        }
    }
}

fn allowed_regex(
    lang: &str,
    _ws: &Path,
    srcs: &[Source],
    allowed: &Rules,
    patterns: &[String],
    r: &mut AllowedReport,
) -> Result<(), String> {
    for p in patterns {
        let re = Regex::new(p).map_err(|e| format!("bad import pattern `{p}`: {e}"))?;
        for s in srcs {
            // Imports live in strings for JS/Go, so match on comment-free raw text.
            let text = if matches!(
                lang,
                "javascript" | "js" | "typescript" | "ts" | "go" | "ruby"
            ) {
                strip_comments_only(lang, &s.raw)
            } else {
                s.code.clone()
            };
            for c in re.captures_iter(&text) {
                let Some(m) = c.get(1) else { continue };
                let name = m.as_str().trim().trim_start_matches("node:").to_string();
                r.checked += 1;
                if name.starts_with('.') || name.starts_with('/') {
                    continue;
                }
                if !allowed.ok(&name, &[".", "/", "::"]) {
                    r.violations.push(Violation {
                        what: format!("import {name}"),
                        at: format!("{}:{}", s.rel, line_of(&text, m.start())),
                    });
                }
            }
        }
    }
    Ok(())
}

fn strip_comments_only(lang: &str, raw: &str) -> String {
    match lang {
        "ruby" => strip_hash_ex(raw, false, false, true),
        _ => strip_c_like_ex(raw, false, true, true),
    }
}

// ----------------------------------------------------------- prototype finder

pub enum ProtoFinding {
    Ok,
    Missing,
    Mismatch(String),
}

pub fn find_prototype(lang: &str, srcs: &[Source], expected: &Proto) -> ProtoFinding {
    let name = expected.short_name();
    match lang {
        "c" => {
            let mut found = Vec::new();
            for s in srcs.iter().filter(|s| s.rel.ends_with(".c")) {
                for d in c_definitions(&s.code) {
                    if d.name == name {
                        found.push((d.norm, format!("{}:{}", s.rel, d.line)));
                    }
                }
            }
            judge(found, &expected.norm)
        }
        "python" => {
            let re = Regex::new(&format!(
                r"(?m)^[ \t]*(?:async[ \t]+)?def[ \t]+{}[ \t]*\(",
                regex::escape(name)
            ))
            .expect("regex");
            let mut found = Vec::new();
            for s in srcs {
                for m in re.find_iter(&s.code) {
                    let open = m.end() - 1;
                    let Some(params) = balanced(&s.code[open..]) else {
                        continue;
                    };
                    let p = sig::py_from_parts(name, params, "");
                    let compatible = p.params.len() == expected.params.len();
                    let norm = if compatible {
                        expected.norm.clone()
                    } else {
                        p.norm.clone()
                    };
                    found.push((norm, format!("{}:{}", s.rel, line_of(&s.code, m.start()))));
                }
            }
            judge(found, &expected.norm)
        }
        "rust" => {
            let re = Regex::new(&format!(r"\bfn\s+{}\b", regex::escape(name))).expect("regex");
            let mut found = Vec::new();
            for s in srcs {
                for m in re.find_iter(&s.code) {
                    let tail = &s.code[m.start()..];
                    let mut depth = 0i32;
                    let mut end = tail.len();
                    for (i, c) in tail.char_indices() {
                        match c {
                            '(' | '<' | '[' => depth += 1,
                            ')' | '>' | ']' => depth -= 1,
                            '{' | ';' if depth <= 0 => {
                                end = i;
                                break;
                            }
                            _ => {}
                        }
                    }
                    // `->` contains '>' which unbalances depth; recompute ignoring arrows.
                    let text = &tail[..end];
                    let text = if text.contains("->") {
                        cut_body(tail)
                    } else {
                        text.to_string()
                    };
                    if let Ok(p) = sig::parse("rust", &text, name) {
                        found.push((p.norm, format!("{}:{}", s.rel, line_of(&s.code, m.start()))));
                    }
                }
            }
            judge(found, &expected.norm)
        }
        _ => {
            let re = Regex::new(&format!(r"\b{}\b", regex::escape(name))).expect("regex");
            if srcs.iter().any(|s| re.is_match(&s.code)) {
                ProtoFinding::Ok
            } else {
                ProtoFinding::Missing
            }
        }
    }
}

/// Text of a Rust item header up to its body `{` or `;`, `->` aware.
fn cut_body(tail: &str) -> String {
    let cleaned = tail.replace("->", "~~");
    let mut depth = 0i32;
    for (i, c) in cleaned.char_indices() {
        match c {
            '(' | '<' | '[' => depth += 1,
            ')' | '>' | ']' => depth -= 1,
            '{' | ';' if depth <= 0 => return tail[..i].to_string(),
            _ => {}
        }
    }
    tail.to_string()
}

fn balanced(s: &str) -> Option<&str> {
    let mut depth = 0;
    for (i, c) in s.char_indices() {
        match c {
            '(' => depth += 1,
            ')' => {
                depth -= 1;
                if depth == 0 {
                    return Some(&s[1..i]);
                }
            }
            _ => {}
        }
    }
    None
}

fn judge(found: Vec<(String, String)>, expected: &str) -> ProtoFinding {
    if found.is_empty() {
        return ProtoFinding::Missing;
    }
    if found.iter().any(|(n, _)| n == expected) {
        return ProtoFinding::Ok;
    }
    let (n, at) = &found[0];
    ProtoFinding::Mismatch(format!("found `{n}` at {at}"))
}

/// Names of functions defined in C sources (for stubs and the harness).
pub fn c_defined_names(srcs: &[Source]) -> BTreeMap<String, String> {
    let mut m = BTreeMap::new();
    for s in srcs {
        for d in c_definitions(&s.code) {
            m.entry(d.name).or_insert(s.rel.clone());
        }
    }
    m
}

pub fn files_with_main(lang: &str, srcs: &[Source]) -> Vec<PathBuf> {
    srcs.iter()
        .filter(|s| match lang {
            "c" => c_definitions(&s.code).iter().any(|d| d.name == "main"),
            _ => false,
        })
        .map(|s| PathBuf::from(&s.rel))
        .collect()
}

#[cfg(test)]
mod tests {
    use super::*;

    fn tmp(name: &str, files: &[(&str, &str)]) -> PathBuf {
        let d = std::env::temp_dir().join(format!("sc-scan-{}-{name}", std::process::id()));
        let _ = fs::remove_dir_all(&d);
        for (p, body) in files {
            let f = d.join(p);
            fs::create_dir_all(f.parent().unwrap()).unwrap();
            fs::write(f, body).unwrap();
        }
        d
    }

    fn allowed(v: &[&str]) -> Vec<String> {
        v.iter().map(|s| s.to_string()).collect()
    }

    #[test]
    fn c_whitelist_checks_headers_and_calls() {
        let d = tmp(
            "c",
            &[
                ("inc/x.h", "size_t my_len(const char *s);\n"),
                (
                    "src/a.c",
                    "#include <stdlib.h>\n#include <string.h>\n#include \"x.h\"\n// strlen(x) in a comment\nstatic int helper(int v) { return v; }\nsize_t my_len(const char *s) { char *p = malloc(3); free(p); return strlen(s) + helper(1) + sizeof(int); }\nvoid apply(int (*f)(int)) { f(1); }\n",
                ),
            ],
        );
        let r = check_allowed(
            "c",
            &d,
            &allowed(&["stdlib.h", "malloc", "free"]),
            &[],
            &SrcOpts::default(),
        )
        .unwrap();
        let what: Vec<&str> = r.violations.iter().map(|v| v.what.as_str()).collect();
        assert_eq!(what, vec!["#include <string.h>", "strlen()"]);
        assert_eq!(r.violations[1].at, "src/a.c:6");
        let _ = fs::remove_dir_all(d);
    }

    #[test]
    fn python_whitelist() {
        let d = tmp(
            "py",
            &[
                ("pkg/__init__.py", "from .core import f\n"),
                (
                    "pkg/core.py",
                    "import re, os.path\nfrom collections import Counter, deque\nfrom typing import (\n    Iterable,\n)\nimport pkg\n\"\"\"import json\"\"\"\n",
                ),
            ],
        );
        let r = check_allowed(
            "python",
            &d,
            &allowed(&["re", "collections.Counter", "typing"]),
            &[],
            &SrcOpts::default(),
        )
        .unwrap();
        let what: Vec<&str> = r.violations.iter().map(|v| v.what.as_str()).collect();
        assert_eq!(what, vec!["import os.path", "import collections.deque"]);
        let _ = fs::remove_dir_all(d);
    }

    #[test]
    fn rust_whitelist() {
        let d = tmp(
            "rs",
            &[
                (
                    "Cargo.toml",
                    "[package]\nname = \"demo\"\n\n[dependencies]\nrand = \"1\"\n",
                ),
                (
                    "src/lib.rs",
                    "mod util;\nuse std::collections::{HashMap, BTreeMap as B};\nuse crate::util::x;\nuse util::y;\nfn f<'a>(s: &'a str) -> usize { let _ = std::fs::read(\"a\"); s.len() }\n",
                ),
            ],
        );
        let r = check_allowed(
            "rust",
            &d,
            &allowed(&["std::collections::HashMap"]),
            &[],
            &SrcOpts::default(),
        )
        .unwrap();
        let what: Vec<&str> = r.violations.iter().map(|v| v.what.as_str()).collect();
        assert_eq!(
            what,
            vec![
                "crate `rand`",
                "std::collections::BTreeMap",
                "std::fs::read"
            ]
        );
        let _ = fs::remove_dir_all(d);
    }

    #[test]
    fn rust_module_allows_all_inside_but_forbidden_and_tests_apart() {
        let d = tmp(
            "rsforbid",
            &[
                (
                    "src/main.rs",
                    "use std::mem;\nfn main() { let mut a = 1; let mut b = 2; mem::swap(&mut a, &mut b); let _ = std::mem::take(&mut a); }\n",
                ),
                (
                    "tests/cli.rs",
                    "use std::process::Command;\n#[test] fn t() { let _ = Command::new(\"x\"); }\n",
                ),
            ],
        );
        let r = check_rules(
            "rust",
            &d,
            &allowed(&["std"]),
            &allowed(&["std::mem::swap"]),
            &[],
            &SrcOpts::default(),
        )
        .unwrap();
        let what: Vec<&str> = r.violations.iter().map(|v| v.what.as_str()).collect();
        // `mem::swap` after `use std::mem;` is std::mem::swap; std::mem::take stays
        // allowed; Command in tests/ is not checked.
        assert_eq!(what, vec!["std::mem::swap"]);
        let _ = fs::remove_dir_all(d);
    }

    #[test]
    fn bash_whitelist() {
        let d = tmp(
            "sh",
            &[(
                "run.sh",
                "#!/bin/bash\nf() { echo hi; }\nX=1 ls -l | grep x && f\ncase $1 in\n  a) mkdir d ;;\nesac\ncat <<EOF\nrm -rf this is text\nEOF\necho \"$(wc -l < f)\" # sed here\n",
            )],
        );
        let r = check_allowed(
            "bash",
            &d,
            &allowed(&["ls", "mkdir", "cat"]),
            &[],
            &SrcOpts::default(),
        )
        .unwrap();
        let what: Vec<&str> = r.violations.iter().map(|v| v.what.as_str()).collect();
        assert_eq!(what, vec!["command `grep`", "command `wc`"]);
        let _ = fs::remove_dir_all(d);
    }

    #[test]
    fn prototypes_found_and_compared() {
        let c = Source {
            rel: "a.c".into(),
            raw: String::new(),
            code: strip(
                "c",
                "size_t\nstr_len(const char *str)\n{\n  return 0;\n}\nint other(void) { return 1; }\n",
            ),
        };
        let want = sig::parse("c", "size_t str_len(const char *s)", "str_len").unwrap();
        assert!(matches!(
            find_prototype("c", std::slice::from_ref(&c), &want),
            ProtoFinding::Ok
        ));
        let want2 = sig::parse("c", "int str_len(char *s)", "str_len").unwrap();
        assert!(matches!(
            find_prototype("c", std::slice::from_ref(&c), &want2),
            ProtoFinding::Mismatch(_)
        ));
        let r = Source {
            rel: "lib.rs".into(),
            raw: String::new(),
            code: strip(
                "rust",
                "pub fn longest<'a>(t: &'a str) -> Option<&'a str> { None }",
            ),
        };
        let want = sig::parse("rust", "fn longest(text: &str) -> Option<&str>", "longest").unwrap();
        assert!(matches!(
            find_prototype("rust", std::slice::from_ref(&r), &want),
            ProtoFinding::Ok
        ));
    }
}
