//! Built-in language presets, as data. A course's `languages:` block
//! overrides any field of these (see `LangConfig::with`); only the harness
//! generators and the dedicated Allowed scanners stay in Rust code.

use crate::unit::LangConfig;
use std::collections::BTreeMap;
use std::sync::OnceLock;

pub const PRESETS: &str = r##"
c:
  name: C
  extensions: [c, h]
  flags: [-std=c11, -Wall, -Wextra, -Werror]
python:
  name: Python 3
  extensions: [py]
  comment: "#"
rust:
  name: Rust
  extensions: [rs]
  static:
    - { name: fmt, cmd: "cargo fmt --check" }
    - { name: clippy, cmd: "cargo clippy --quiet --all-targets -- -D warnings" }
cpp:
  name: C++
  extensions: [cpp, cc, cxx, hpp, hh, h]
bash:
  name: Bash
  extensions: [sh, bash]
javascript:
  name: JavaScript
  extensions: [js, mjs, cjs]
  run: node main.js
  imports:
    - 'require\(\s*[''"]([^''"]+)[''"]\s*\)'
    - '(?m)^\s*import\s+(?:[^''"]*?\s+from\s+)?[''"]([^''"]+)[''"]'
    - 'import\(\s*[''"]([^''"]+)[''"]\s*\)'
typescript:
  name: TypeScript
  extensions: [ts, tsx, mts]
  imports:
    - 'require\(\s*[''"]([^''"]+)[''"]\s*\)'
    - '(?m)^\s*import\s+(?:[^''"]*?\s+from\s+)?[''"]([^''"]+)[''"]'
go:
  name: Go
  extensions: [go]
  imports:
    - '(?m)^\s*import\s+(?:\w+\s+)?"([^"]+)"'
    - '(?m)^\s+(?:\w+\s+)?"([^"]+)"\s*$'
java:
  name: Java
  extensions: [java]
  imports: ['(?m)^\s*import\s+(?:static\s+)?([\w.]+?)(?:\.\*)?\s*;?\s*$']
ruby:
  name: Ruby
  extensions: [rb]
  comment: "#"
  run: ruby main.rb
  imports: ['(?m)^\s*require(?:_relative)?\s*\(?\s*[''"]([^''"]+)[''"]']
"##;

/// Canonical key for a language name (`sh` -> `bash`, `js` -> `javascript`).
pub fn canonical(lang: &str) -> &str {
    match lang {
        "sh" | "shell" => "bash",
        "js" => "javascript",
        "ts" => "typescript",
        "c++" => "cpp",
        other => other,
    }
}

fn table() -> &'static BTreeMap<String, LangConfig> {
    static T: OnceLock<BTreeMap<String, LangConfig>> = OnceLock::new();
    T.get_or_init(|| yaml_serde::from_str(PRESETS).expect("built-in presets are valid YAML"))
}

/// The built-in preset for `lang` (empty for unknown languages).
pub fn preset(lang: &str) -> LangConfig {
    table().get(canonical(lang)).cloned().unwrap_or_default()
}

pub fn known() -> Vec<String> {
    table().keys().cloned().collect()
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn presets_parse_and_override() {
        assert!(known().contains(&"rust".to_string()));
        let c = preset("c");
        assert_eq!(c.flags.as_deref().map(|f| f.len()), Some(4));
        let over = LangConfig {
            flags: Some(vec!["-std=c99".into()]),
            wrap: Some("valgrind".into()),
            ..Default::default()
        };
        let m = c.with(&over);
        assert_eq!(m.flags, Some(vec!["-std=c99".to_string()]));
        assert_eq!(m.extensions.as_deref().map(|e| e.len()), Some(2));
        assert_eq!(m.wrap.as_deref(), Some("valgrind"));
        let rust = preset("rust").with(&LangConfig {
            static_checks: Some(vec![]),
            ..Default::default()
        });
        assert_eq!(
            rust.static_checks.map(|s| s.len()),
            Some(0),
            "empty list replaces the default"
        );
        assert!(preset("js").imports.is_some());
    }
}
