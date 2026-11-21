"""The JSON line the Linux container prints and the stub workspaces."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from proctor.compat import read_text
from proctor.errors import Error

from proctor import selftest, sig


def line(r: selftest.UnitReport) -> str:
    return json.dumps(r.to_json(), separators=(",", ":"), ensure_ascii=False)


def test_json_lines_round_trip() -> None:
    ok = selftest.UnitReport(unit="d01", error=None, vacuous=[], vacuity_checked=3, notes=[])
    assert (
        line(ok)
        == '{"error":null,"notes":[],"unit":"d01","vacuity_checked":3,"vacuous":[],"wrong":[]}'
    )
    ko = selftest.UnitReport(
        unit="p01.1-é",
        error="case x: course error: unknown function `g`",
        vacuous=["`f` is never tested"],
        vacuity_checked=1,
        notes=["no stub check for perl"],
        wrong=["case x"],
    )
    assert line(ko) == (
        '{"error":"case x: course error: unknown function `g`","notes":["no stub check for perl"],'
        '"unit":"p01.1-é","vacuity_checked":1,"vacuous":["`f` is never tested"],"wrong":["case x"]}'
    )
    for r in (ok, ko):
        assert selftest.UnitReport.from_json(json.loads(line(r))) == r
    assert ok.ok() and not ko.ok()
    weak = selftest.UnitReport(unit="u", error=None, vacuous=["x"], vacuity_checked=1, notes=[])
    assert not weak.ok()


@pytest.mark.parametrize(
    "v",
    [
        [],
        {"error": None},
        {"unit": 3, "error": "e"},
        {"unit": "u", "error": 5},
    ],
)
def test_from_json_rejects(v: Any) -> None:
    assert selftest.UnitReport.from_json(v) is None


def test_from_json_is_lenient_elsewhere() -> None:
    r = selftest.UnitReport.from_json(
        {"unit": "u", "passed": [1, 2], "vacuous": ["a", 1], "notes": "x", "vacuity_checked": -1}
    )
    assert r == selftest.UnitReport(
        unit="u", error=None, vacuous=["a"], vacuity_checked=0, notes=[]
    )


def test_silent_keeps_the_log() -> None:
    s = selftest.Silent(log="a")
    s.log("b")
    s.log("c")
    assert s.log_text == "abc"


def test_python_stubs_go_to_their_module(tmp_path: Path) -> None:
    protos = [
        sig.parse("python", "def parse(s: str) -> int", "pkg.mod.parse"),
        sig.parse("python", "def helper(x, y=None)", "helper"),
    ]
    d = tmp_path / "tree"
    (d / "old").mkdir(parents=True)
    selftest.stub_workspace("python", protos, "single", d, "unused")
    assert not (d / "old").exists()
    assert read_text(d / "pkg" / "mod" / "__init__.py") == (
        "def parse(s):\n    raise NotImplementedError\n"
    )
    assert read_text(d / "single" / "__init__.py") == (
        "def helper(x, y=None):\n    raise NotImplementedError\n"
    )
    with pytest.raises(Error) as e:
        selftest.stub_workspace("python", protos[1:], None, d, "unused")
    assert e.value.msg == "python stubs need dotted names or a `module:`"


def test_c_and_rust_stubs(tmp_path: Path) -> None:
    c = [sig.parse("c", "size_t str_len(const char *s)", "str_len")]
    selftest.stub_workspace("c", c, None, tmp_path / "c", "unused")
    assert read_text(tmp_path / "c" / "stubs.c") == (
        "#include <stddef.h>\n#include <stdbool.h>\n#include <stdint.h>\n\n"
        "size_t str_len(const char *s)\n{\n    (void)s;\n    return 0;\n}\n"
    )
    rs = [sig.parse("rust", "pub fn word_count(text: &str) -> usize", "word_count")]
    selftest.stub_workspace("rust", rs, None, tmp_path / "rs", "longest_word")
    assert read_text(tmp_path / "rs" / "Cargo.toml") == (
        '[package]\nname = "longest_word"\nversion = "0.0.0"\nedition = "2024"\n\n[workspace]\n'
    )
    assert read_text(tmp_path / "rs" / "src" / "lib.rs") == (
        "/// TODO\npub fn word_count(text: &str) -> usize {\n    let _ = text;\n    todo!()\n}\n"
    )
    with pytest.raises(Error) as e:
        selftest.stub_workspace("java", rs, None, tmp_path / "j", "unused")
    assert e.value.msg == "no stub generator for java"


def test_rust_stubs_hold_the_given_types(tmp_path: Path) -> None:
    protos = [
        sig.parse("rust", "pub fn new() -> Self", "Stack::new"),
        sig.parse("rust", "pub fn top(&self) -> Option<i32>", "Stack::top"),
    ]
    types = "#[derive(Debug)]\npub struct Stack {\n    pub items: Vec<i32>,\n}\n\n"
    selftest.stub_workspace("rust", protos, None, tmp_path, "d01_stack", types)
    lib = read_text(tmp_path / "src" / "lib.rs")
    assert lib.startswith(types)
    # The given type is not declared a second time, its methods are stubbed.
    assert lib.count("pub struct Stack") == 1 and "impl Stack {" in lib
    assert selftest.crate_name("p01.2-cli") == "p01_2_cli"
