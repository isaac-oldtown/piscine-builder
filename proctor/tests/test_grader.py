"""Grader behaviour on small scratch courses and on edited copies of the demo
course. Units whose toolchain is not installed are skipped."""

from __future__ import annotations

import sys

from common import Scratch, demo_copy, missing_tool
from proctor.compat import read_text, write_text
from proctor.errors import Ok

from proctor import course, engine, render, selftest


def test_a_stage_inherits_the_allowed_list_of_earlier_stages() -> None:
    with demo_copy("allowed") as s:
        spec = s.path / ".proctor" / "units" / "p01.2-cli.yaml"
        yaml = read_text(spec)
        assert "allowed: [sys]" in yaml
        # A stage that only bans something lists no `allowed` of its own.
        write_text(spec, yaml.replace("allowed: [sys]", "forbidden: [os]"))
        c = course.CourseDir.load(s.path)
        assert engine.validate(c) == []
        stage = c.unit("p01.2-cli")
        assert stage is not None
        assert c.allowed(stage) == []
        page = render.unit_md(c, stage)
        assert "Only your own code" in page, page
        assert "No restriction" not in page, page
        drill = c.unit("d02-word-count")
        assert drill is not None
        assert c.allowed(drill) == []


def test_a_ban_with_nothing_allowed_anywhere_is_rejected() -> None:
    with demo_copy("forbidden") as s:
        spec = s.path / ".proctor" / "units" / "d03-longest-word.yaml"
        yaml = read_text(spec)
        write_text(spec, yaml.replace("allowed: []", "forbidden: [std::mem]"))
        c = course.CourseDir.load(s.path)
        problems = engine.validate(c)
        assert any(p.startswith("d03-longest-word: `forbidden` needs") for p in problems), problems


BYTES_COURSE = [
    (
        ".proctor/course.yaml",
        "title: Bytes\nunits: [d01-rust-bytes, d02-python-bytes]\n",
    ),
    (
        ".proctor/units/d01-rust-bytes.yaml",
        """id: d01-rust-bytes
title: Rust bytes
language: rust
target: function
stubs: src/lib.rs
goal: Count bytes.
functions:
  - { name: byte_len, sig: "pub fn byte_len(b: &[u8]) -> usize" }
  - { name: utf8_of, sig: "pub fn utf8_of(s: &str) -> Vec<u8>" }
cases: |
  byte_len("é") == 2
  byte_len("日本") == 6
  byte_len([1, 2, 3]) == 3
  utf8_of("é") == [195, 169]
""",
    ),
    (
        ".proctor/refs/d01-rust-bytes/Cargo.toml",
        '[package]\nname = "bytes_drill"\nversion = "0.1.0"\nedition = "2024"\n\n[workspace]\n',
    ),
    (
        ".proctor/refs/d01-rust-bytes/src/lib.rs",
        "pub fn byte_len(b: &[u8]) -> usize {\n    b.len()\n}\n\npub fn utf8_of(s: &str) -> Vec<u8> {\n    s.as_bytes().to_vec()\n}\n",
    ),
    (
        ".proctor/units/d02-python-bytes.yaml",
        """id: d02-python-bytes
title: Python bytes
language: python
target: function
stubs: pybytes/__init__.py
goal: Count bytes.
functions:
  - { name: pybytes.byte_len, sig: "def byte_len(b: bytes) -> int" }
  - { name: pybytes.utf8_of, sig: "def utf8_of(s: str) -> bytes" }
cases: |
  pybytes.byte_len("é") == 2
  pybytes.byte_len([1, 2, 3]) == 3
  pybytes.utf8_of("é") == [195, 169]
""",
    ),
    (
        ".proctor/refs/d02-python-bytes/pybytes/__init__.py",
        'def byte_len(b: bytes) -> int:\n    assert isinstance(b, bytes)\n    return len(b)\n\n\ndef utf8_of(s: str) -> bytes:\n    return s.encode("utf-8")\n',
    ),
]


def test_text_given_for_bytes_is_its_utf8_encoding() -> None:
    with Scratch("bytes") as s:
        for rel, body in BYTES_COURSE:
            s.write(rel, body)
        c = course.CourseDir.load(s.path)
        assert engine.validate(c) == []
        checked = 0
        for u in c.units:
            why = missing_tool(c, u)
            if why is not None:
                print(f"skipped {u.id}: {why}", file=sys.stderr)
                continue
            checked += 1
            r = selftest.unit(c, u, 1)
            assert isinstance(r.reference, Ok), f"{u.id}: {r.reference}"
            assert not r.vacuous, f"{u.id}: {r.vacuous}"
        assert checked > 0, "no unit could be checked on this machine"
