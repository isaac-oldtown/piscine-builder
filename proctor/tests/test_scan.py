from __future__ import annotations

import shutil
from pathlib import Path

from proctor.scan import (
    ProtoFindingMismatch,
    ProtoFindingOk,
    Source,
    SrcOpts,
    check_allowed,
    check_rules,
    find_prototype,
    strip,
)

from proctor import sig


def tmp(base: Path, name: str, files: list[tuple[str, str]]) -> Path:
    d = base / f"sc-scan-{name}"
    shutil.rmtree(d, ignore_errors=True)
    for p, body in files:
        f = d / p
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_bytes(body.encode("utf-8"))
    return d


def allowed(v: list[str]) -> list[str]:
    return list(v)


def test_c_whitelist_checks_headers_and_calls(tmp_path: Path) -> None:
    d = tmp(
        tmp_path,
        "c",
        [
            ("inc/x.h", "size_t my_len(const char *s);\n"),
            (
                "src/a.c",
                '#include <stdlib.h>\n#include <string.h>\n#include "x.h"\n// strlen(x) in a comment\nstatic int helper(int v) { return v; }\nsize_t my_len(const char *s) { char *p = malloc(3); free(p); return strlen(s) + helper(1) + sizeof(int); }\nvoid apply(int (*f)(int)) { f(1); }\n',
            ),
        ],
    )
    r = check_allowed("c", d, allowed(["stdlib.h", "malloc", "free"]), [], SrcOpts())
    what = [v.what for v in r.violations]
    assert what == ["#include <string.h>", "strlen()"]
    assert r.violations[1].at == "src/a.c:6"
    shutil.rmtree(d, ignore_errors=True)


def test_python_whitelist(tmp_path: Path) -> None:
    d = tmp(
        tmp_path,
        "py",
        [
            ("pkg/__init__.py", "from .core import f\n"),
            (
                "pkg/core.py",
                'import re, os.path\nfrom collections import Counter, deque\nfrom typing import (\n    Iterable,\n)\nimport pkg\n"""import json"""\n',
            ),
        ],
    )
    r = check_allowed("python", d, allowed(["re", "collections.Counter", "typing"]), [], SrcOpts())
    what = [v.what for v in r.violations]
    assert what == ["import os.path", "import collections.deque"]
    shutil.rmtree(d, ignore_errors=True)


def test_rust_whitelist(tmp_path: Path) -> None:
    d = tmp(
        tmp_path,
        "rs",
        [
            (
                "Cargo.toml",
                '[package]\nname = "demo"\n\n[dependencies]\nrand = "1"\n',
            ),
            (
                "src/lib.rs",
                "mod util;\nuse std::collections::{HashMap, BTreeMap as B};\nuse crate::util::x;\nuse util::y;\nfn f<'a>(s: &'a str) -> usize { let _ = std::fs::read(\"a\"); s.len() }\n",
            ),
        ],
    )
    r = check_allowed("rust", d, allowed(["std::collections::HashMap"]), [], SrcOpts())
    what = [v.what for v in r.violations]
    assert what == [
        "crate `rand`",
        "std::collections::BTreeMap",
        "std::fs::read",
    ]
    shutil.rmtree(d, ignore_errors=True)


def test_rust_module_allows_all_inside_but_forbidden_and_tests_apart(tmp_path: Path) -> None:
    d = tmp(
        tmp_path,
        "rsforbid",
        [
            (
                "src/main.rs",
                "use std::mem;\nfn main() { let mut a = 1; let mut b = 2; mem::swap(&mut a, &mut b); let _ = std::mem::take(&mut a); }\n",
            ),
            (
                "tests/cli.rs",
                'use std::process::Command;\n#[test] fn t() { let _ = Command::new("x"); }\n',
            ),
        ],
    )
    r = check_rules(
        "rust",
        d,
        allowed(["std"]),
        allowed(["std::mem::swap"]),
        [],
        SrcOpts(),
    )
    what = [v.what for v in r.violations]
    # `mem::swap` after `use std::mem;` is std::mem::swap; std::mem::take stays
    # allowed; Command in tests/ is not checked.
    assert what == ["std::mem::swap"]
    shutil.rmtree(d, ignore_errors=True)


def test_bash_whitelist(tmp_path: Path) -> None:
    d = tmp(
        tmp_path,
        "sh",
        [
            (
                "run.sh",
                '#!/bin/bash\nf() { echo hi; }\nX=1 ls -l | grep x && f\ncase $1 in\n  a) mkdir d ;;\nesac\ncat <<EOF\nrm -rf this is text\nEOF\necho "$(wc -l < f)" # sed here\n',
            )
        ],
    )
    r = check_allowed("bash", d, allowed(["ls", "mkdir", "cat"]), [], SrcOpts())
    what = [v.what for v in r.violations]
    assert what == ["command `grep`", "command `wc`"]
    shutil.rmtree(d, ignore_errors=True)


def test_prototypes_found_and_compared() -> None:
    c = Source(
        rel="a.c",
        raw="",
        code=strip(
            "c",
            "size_t\nstr_len(const char *str)\n{\n  return 0;\n}\nint other(void) { return 1; }\n",
        ),
    )
    want = sig.parse("c", "size_t str_len(const char *s)", "str_len")
    assert isinstance(find_prototype("c", [c], want), ProtoFindingOk)
    want2 = sig.parse("c", "int str_len(char *s)", "str_len")
    assert isinstance(find_prototype("c", [c], want2), ProtoFindingMismatch)
    r = Source(
        rel="lib.rs",
        raw="",
        code=strip(
            "rust",
            "pub fn longest<'a>(t: &'a str) -> Option<&'a str> { None }",
        ),
    )
    want = sig.parse("rust", "fn longest(text: &str) -> Option<&str>", "longest")
    assert isinstance(find_prototype("rust", [r], want), ProtoFindingOk)
