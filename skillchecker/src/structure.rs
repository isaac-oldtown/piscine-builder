//! The "soft" project tree: `files:` rules say what must exist and what may
//! exist. When a unit lists any rule, every other file is unexpected unless a
//! `free` rule covers it (`{ path: "**", rule: free }` allows anything).

use crate::scan::walk;
use crate::unit::{FileRule, Rule};
use std::path::Path;

/// Glob match on `/`-separated paths: `*` and `?` stay inside one segment,
/// `**` spans any number of segments (including none).
pub fn glob(pattern: &str, path: &str) -> bool {
    let p: Vec<&str> = pattern.split('/').filter(|s| !s.is_empty()).collect();
    let s: Vec<&str> = path.split('/').filter(|s| !s.is_empty()).collect();
    fn segs(p: &[&str], s: &[&str]) -> bool {
        match p.first() {
            None => s.is_empty(),
            Some(&"**") => (0..=s.len()).any(|k| segs(&p[1..], &s[k..])),
            Some(seg) => {
                !s.is_empty() && one(seg.as_bytes(), s[0].as_bytes()) && segs(&p[1..], &s[1..])
            }
        }
    }
    fn one(p: &[u8], s: &[u8]) -> bool {
        match p.first() {
            None => s.is_empty(),
            Some(b'*') => (0..=s.len()).any(|k| one(&p[1..], &s[k..])),
            Some(b'?') => !s.is_empty() && one(&p[1..], &s[1..]),
            Some(c) => s.first() == Some(c) && one(&p[1..], &s[1..]),
        }
    }
    segs(&p, &s)
}

/// Build output and editor litter never count as student files.
const LITTER: &[&str] = &[
    "*.o",
    "*.obj",
    "*.exe",
    "*.pdb",
    "*.ilk",
    "a.out",
    "*.pyc",
    ".DS_Store",
    "*~",
    "*.swp",
    "Cargo.lock",
    "*.class",
    "Thumbs.db",
];

fn litter(rel: &str) -> bool {
    let name = rel.rsplit('/').next().unwrap_or(rel);
    LITTER.iter().any(|l| glob(l, name))
}

pub struct Finding {
    pub ok: bool,
    pub name: String,
    pub detail: String,
}

pub fn check(ws: &Path, rules: &[FileRule]) -> Vec<Finding> {
    let files: Vec<String> = walk(ws).into_iter().filter(|f| !litter(f)).collect();
    let mut out = Vec::new();
    for r in rules {
        let hits: Vec<&String> = files.iter().filter(|f| glob(&r.path, f)).collect();
        match r.rule {
            Rule::Required | Rule::OneOrMore => out.push(Finding {
                ok: !hits.is_empty(),
                name: r.path.clone(),
                detail: match (hits.len(), r.rule) {
                    (0, _) => "missing".into(),
                    (1, _) => "present".into(),
                    (n, _) => format!("{n} files"),
                },
            }),
            Rule::Forbidden => out.push(Finding {
                ok: hits.is_empty(),
                name: format!("no {}", r.path),
                detail: match hits.first() {
                    Some(f) => format!("{f} must not exist"),
                    None => "none".into(),
                },
            }),
            Rule::Free => {}
        }
    }
    if !rules.is_empty() {
        let stray: Vec<&String> = files
            .iter()
            .filter(|f| {
                !rules
                    .iter()
                    .any(|r| r.rule != Rule::Forbidden && glob(&r.path, f))
            })
            .collect();
        out.push(Finding {
            ok: stray.is_empty(),
            name: "no unexpected file".into(),
            detail: match stray.as_slice() {
                [] => format!("{} file(s) checked", files.len()),
                [one] => format!("{one} is not part of the expected layout"),
                [first, rest @ ..] => format!(
                    "{first} (and {} more) not part of the expected layout",
                    rest.len()
                ),
            },
        });
    }
    out
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn globbing() {
        assert!(glob("src/*.c", "src/a.c"));
        assert!(!glob("src/*.c", "src/x/a.c"));
        assert!(glob("src/**/*.c", "src/a.c"));
        assert!(glob("src/**/*.c", "src/x/y/a.c"));
        assert!(glob("**", "anything/at/all.txt"));
        assert!(glob("lib/**", "lib/a/b.rs"));
        assert!(glob("main.?s", "main.rs"));
        assert!(!glob("main.rs", "src/main.rs"));
    }
}
