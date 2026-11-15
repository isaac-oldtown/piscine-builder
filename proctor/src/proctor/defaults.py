"""Built-in language presets, as data. A course's `languages:` block
overrides any field of these (see `LangConfig.with_`); only the harness
generators and the dedicated Allowed scanners stay in code."""

from __future__ import annotations

import copy
import functools

from proctor import schema
from proctor.unit import LangConfig

PRESETS = r"""
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
"""


def canonical(lang: str) -> str:
    """Canonical key for a language name (`sh` -> `bash`, `js` -> `javascript`)."""
    match lang:
        case "sh" | "shell":
            return "bash"
        case "js":
            return "javascript"
        case "ts":
            return "typescript"
        case "c++":
            return "cpp"
        case other:
            return other


@functools.cache
def table() -> dict[str, LangConfig]:
    # The built-in presets are valid YAML.
    return schema.load(dict[str, LangConfig], PRESETS)


def preset(lang: str) -> LangConfig:
    """The built-in preset for `lang` (empty for unknown languages)."""
    c = table().get(canonical(lang))
    return copy.deepcopy(c) if c is not None else LangConfig()


def known() -> list[str]:
    return sorted(table())
