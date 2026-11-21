"""Grader behaviour on small scratch courses and on edited copies of the demo
course. Units whose toolchain is not installed are skipped."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from common import Scratch, demo_copy, missing_tool
from proctor.compat import read_text, write_text
from proctor.unit import Unit

from proctor import course, engine, render, scaffold, selftest


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


def assert_passes(c: course.CourseDir, u: Unit, solution: Path) -> None:
    """A correct solution in `solution` passes every check of `u`."""
    opts = engine.Options(seed=1, against=engine.AgainstDir(solution), tests_only=False)
    v = engine.Engine.new(c, u, opts).check(selftest.Silent())
    assert v.ok, f"{u.id}: {v.first_fail}"


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
        "solutions/d01-rust-bytes/Cargo.toml",
        '[package]\nname = "bytes_drill"\nversion = "0.1.0"\nedition = "2024"\n\n[workspace]\n',
    ),
    (
        "solutions/d01-rust-bytes/src/lib.rs",
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
        "solutions/d02-python-bytes/pybytes/__init__.py",
        'def byte_len(b: bytes) -> int:\n    assert isinstance(b, bytes)\n    return len(b)\n\n\ndef utf8_of(s: str) -> bytes:\n    return s.encode("utf-8")\n',
    ),
]


STACK_UNIT = """id: d01-stack
title: A stack
language: rust
target: function
stubs: src/lib.rs
timeout_ms: 3000
goal: Write a stack of integers.
functions:
  - { name: "Stack::new", sig: "pub fn new() -> Self", returns: an empty stack }
  - { name: "Stack::push", sig: "pub fn push(&mut self, x: i32)" }
  - { name: "Stack::pop", sig: "pub fn pop(&mut self) -> Option<i32>", returns: the last value pushed }
  - { name: "Stack::len", sig: "pub fn len(&self) -> usize" }
  - { name: total, sig: "pub fn total(v: &[i32]) -> i32" }
cases: |
  total([1, 2, 3]) == 6
rust_tests: |
  // example
  #[test]
  fn push_then_pop() {
      let mut s = Stack::new();
      s.push(1);
      s.push(2);
      assert_eq!(s.pop(), Some(2));
  }

  #[test]
  fn counts() {
      let mut s = Stack::new();
      s.push(5);
      assert_eq!(s.len(), 1);
      assert_eq!(Stack::new().pop(), None);
  }
"""

STACK_LIB = """#[derive(Default)]
pub struct Stack {
    items: Vec<i32>,
}

impl Stack {
    pub fn new() -> Self {
        Stack { items: Vec::new() }
    }

    pub fn push(&mut self, x: i32) {
        self.items.push(x);
    }

    pub fn pop(&mut self) -> Option<i32> {
        self.items.pop()
    }

    pub fn len(&self) -> usize {
        self.items.len()
    }

    pub fn is_empty(&self) -> bool {
        self.items.is_empty()
    }
}

pub fn total(v: &[i32]) -> i32 {
    v.iter().sum()
}
"""

STACK_TOML = '[package]\nname = "mini-stack"\nversion = "0.1.0"\nedition = "2024"\n\n[workspace]\n'


def test_types_and_methods_are_graded_by_tests_in_rust() -> None:
    with Scratch("rust-tests") as s:
        s.write(".proctor/course.yaml", "title: Stack\nunits: [d01-stack]\n")
        s.write(".proctor/units/d01-stack.yaml", STACK_UNIT)
        s.write("solutions/d01-stack/Cargo.toml", STACK_TOML)
        s.write("solutions/d01-stack/src/lib.rs", STACK_LIB)
        s.write(".proctor/scaffold/d01-stack/Cargo.toml", STACK_TOML)
        c = course.CourseDir.load(s.path)
        assert engine.validate(c) == []
        u = c.unit("d01-stack")
        assert u is not None
        page = render.unit_md(c, u)
        assert "Some of the tests proctor runs against your crate" in page, page
        assert "fn push_then_pop()" in page and "fn counts()" not in page, page
        assert "**Tests in Rust:**" in page, page
        why = missing_tool(c, u)
        if why is not None:
            pytest.skip(why)
        r = selftest.unit(c, u, 1)
        assert r.error is None, r.error
        assert not r.vacuous and r.vacuity_checked == 5, (r.vacuous, r.notes)
        assert not r.notes, r.notes
        assert_passes(c, u, s.path / "solutions" / u.id)

        def check(tests_only: bool = False) -> dict[str, str]:
            opts = engine.Options(seed=1, against=engine.AgainstStudent(), tests_only=tests_only)
            v = engine.Engine.new(c, u, opts).check(selftest.Silent())
            return {i.name: "" if i.ok else i.detail for p in v.phases for i in p.items}

        # The stubs written at unlock compile, and every test fails on them.
        scaffold.reveal(c, u)
        lib = c.workspace(u) / "src" / "lib.rs"
        got = check(tests_only=True)
        assert got["compile"] == "", got
        assert got["test counts"].startswith("not yet implemented (at src/lib.rs:"), got
        assert got["case total([1, 2, 3]) == 6"] != ""

        write_text(lib, STACK_LIB.replace("self.items.pop()", "self.items.first().copied()"))
        got = check()
        assert got["case total([1, 2, 3]) == 6"] == ""
        assert got["test push_then_pop"] == (
            "assertion `left == right` failed; left: Some(1); right: Some(2)"
        ), got
        assert got["test counts"] == ""

        spin = "loop {\n            std::thread::yield_now();\n        }"
        write_text(lib, STACK_LIB.replace("self.items.len()", spin))
        got = check()
        assert got["test counts"] == "timed out after 3000 ms", got
        assert got["test push_then_pop"] == ""

        write_text(lib, STACK_LIB.replace("pub fn len", "pub fn size"))
        got = check()
        assert got["Stack::len"].startswith("not found in an `impl Stack` block"), got

        # Private: the prototype matches, but proctor's tests cannot call it.
        write_text(lib, STACK_LIB.replace("pub fn len", "fn len"))
        got = check()
        assert got["compile"].startswith(
            "proctor's tests do not compile against your crate: error[E0624]"
        ), got
        assert "tests/t_" not in got["compile"]


def test_with_tests_in_rust_only_what_cases_call_must_suit_the_harness() -> None:
    peek = '  - { name: peek_of, sig: "pub fn peek_of(s: &Stack) -> Option<i32>" }\ncases: |\n'
    with Scratch("rust-tests-harness") as s:
        s.write(".proctor/course.yaml", "title: Stack\nunits: [d01-stack]\n")
        s.write(".proctor/units/d01-stack.yaml", STACK_UNIT.replace("cases: |\n", peek))
        assert engine.validate(course.CourseDir.load(s.path)) == []
        called = STACK_UNIT.replace("cases: |\n", peek + "  peek_of(1) == 1\n")
        s.write(".proctor/units/d01-stack.yaml", called)
        assert engine.validate(course.CourseDir.load(s.path)) == [
            "d01-stack: parameter `s` of `peek_of` has type `&Stack`, which the rust harness cannot build (test it in `rust_tests`)"
        ]
        # Without tests in Rust, every function must suit the harness, as before.
        plain = STACK_UNIT.split("rust_tests:")[0]
        s.write(".proctor/units/d01-stack.yaml", plain)
        problems = engine.validate(course.CourseDir.load(s.path))
        assert (
            "d01-stack: `Stack::new` is an associated function; only free functions can be called"
            in problems
        )


POINT_TYPES = """#[derive(Debug, Clone, Copy, PartialEq)]
pub struct Point {
    pub x: i32,
    pub y: i32,
}
"""

POINT_UNIT = (
    """id: d01-point
title: Points
language: rust
target: function
stubs: src/lib.rs
goal: Measure points.
types: |
"""
    + "".join(f"  {line}\n" for line in POINT_TYPES.splitlines())
    + """functions:
  - { name: "Point::dist2", sig: "pub fn dist2(&self) -> i32", returns: the squared distance to the origin }
rust_tests: |
  #[test]
  fn three_four_five() {
      assert_eq!(Point { x: 3, y: 4 }.dist2(), 25);
  }
"""
)

POINT_IMPL = "\nimpl Point {\n    pub fn dist2(&self) -> i32 {\n        self.x * self.x + self.y * self.y\n    }\n}\n"


def test_given_types_let_tests_in_rust_build_on_stubs() -> None:
    with Scratch("rust-types") as s:
        s.write(".proctor/course.yaml", "title: Points\nunits: [d01-point]\n")
        s.write(".proctor/units/d01-point.yaml", POINT_UNIT)
        s.write(".proctor/scaffold/d01-point/Cargo.toml", STACK_TOML)
        s.write("solutions/d01-point/Cargo.toml", STACK_TOML)
        s.write("solutions/d01-point/src/lib.rs", POINT_TYPES + POINT_IMPL)
        c = course.CourseDir.load(s.path)
        assert engine.validate(c) == []
        u = c.unit("d01-point")
        assert u is not None
        page = render.unit_md(c, u)
        assert "## Expected types" in page and "pub x: i32," in page, page
        why = missing_tool(c, u)
        if why is not None:
            pytest.skip(why)
        r = selftest.unit(c, u, 1)
        assert r.error is None and not r.vacuous, (r.error, r.vacuous)
        assert_passes(c, u, s.path / "solutions" / u.id)
        # The stubs written at unlock hold the types: the tests build and fail.
        scaffold.reveal(c, u)
        lib = read_text(c.workspace(u) / "src" / "lib.rs")
        assert lib.count("pub struct Point") == 1 and "impl Point {" in lib, lib
        opts = engine.Options(seed=1, against=engine.AgainstStudent(), tests_only=True)
        v = engine.Engine.new(c, u, opts).check(selftest.Silent())
        got = {i.name: "" if i.ok else i.detail for p in v.phases for i in p.items}
        assert got["compile"] == "", got
        assert got["test three_four_five"].startswith("not yet implemented"), got
        # Without the types, the stubs cannot build the tests.
        s.write(
            ".proctor/units/d01-point.yaml",
            POINT_UNIT.split("types: |")[0] + "functions:" + POINT_UNIT.split("functions:")[1],
        )
        c = course.CourseDir.load(s.path)
        u = c.unit("d01-point")
        assert u is not None
        assert selftest.unit(c, u, 1).error is not None


LIFETIMES_UNIT = """id: d01-life
title: Lifetimes
language: rust
target: function
stubs: src/lib.rs
goal: Pick the longer text and the largest value.
functions:
  - { name: longest, sig: "pub fn longest<'a>(a: &'a str, b: &'a str) -> &'a str" }
  - { name: largest, sig: "pub fn largest<T>(v: &[T]) -> T where T: PartialOrd + Copy" }
cases: |
  longest("ab", "c") == "ab"
  longest("a", "bcd") == "bcd"
rust_tests: |
  #[test]
  fn largest_int() {
      assert_eq!(largest(&[3, 9, 2]), 9);
  }
"""

LIFETIMES_LIB = """pub fn longest<'a>(a: &'a str, b: &'a str) -> &'a str {
    if b.len() > a.len() { b } else { a }
}

pub fn largest<T>(v: &[T]) -> T
where
    T: PartialOrd + Copy,
{
    let mut m = v[0];
    for &x in v {
        if x > m {
            m = x;
        }
    }
    m
}
"""


def test_lifetimes_and_where_clauses_in_prototypes() -> None:
    with Scratch("lifetimes") as s:
        s.write(".proctor/course.yaml", "title: Lifetimes\nunits: [d01-life]\n")
        s.write(".proctor/units/d01-life.yaml", LIFETIMES_UNIT)
        s.write("solutions/d01-life/Cargo.toml", STACK_TOML)
        s.write("solutions/d01-life/src/lib.rs", LIFETIMES_LIB)
        c = course.CourseDir.load(s.path)
        assert engine.validate(c) == []
        u = c.unit("d01-life")
        assert u is not None
        why = missing_tool(c, u)
        if why is not None:
            pytest.skip(why)
        r = selftest.unit(c, u, 1)
        assert r.error is None, r.error
        assert not r.vacuous and not r.notes, (r.vacuous, r.notes)
        assert_passes(c, u, s.path / "solutions" / u.id)


def test_an_empty_command_is_a_course_error() -> None:
    unit = 'id: d01-p\ntitle: P\nlanguage: python\ntarget: program\nrun: ""\nbuild: "  "\ngoal: G.\ncases: |\n  => exit 0\n'
    with Scratch("empty-command") as s:
        s.write(".proctor/course.yaml", "title: T\nunits: [d01-p]\n")
        s.write(".proctor/units/d01-p.yaml", unit)
        assert engine.validate(course.CourseDir.load(s.path)) == [
            "d01-p: `run:` is empty; remove it or give a command",
            "d01-p: `build:` is empty; remove it or give a command",
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
            assert r.error is None, f"{u.id}: {r.error}"
            assert not r.vacuous, f"{u.id}: {r.vacuous}"
            assert_passes(c, u, s.path / "solutions" / u.id)
        assert checked > 0, "no unit could be checked on this machine"


def test_a_reading_unit_has_nothing_to_hand_in() -> None:
    with demo_copy("reading") as s:
        s.write(
            ".proctor/units/d00-intro.yaml",
            "id: d00-intro\ntitle: Intro\nlanguage: python\nreading: true\ntarget: program\n"
            "scaffold: none\ngoal: Read the introduction.\nlesson: |\n  ### Welcome\n\n  Read this.\n",
        )
        index = s.path / ".proctor" / "course.yaml"
        write_text(index, read_text(index).replace("units:\n", "units:\n  - d00-intro\n", 1))
        c = course.CourseDir.load(s.path)
        assert engine.validate(c) == []
        u = c.unit("d00-intro")
        assert u is not None
        page = render.unit_md(c, u)
        assert "Read this." in page and "nothing to hand in" in page, page
        assert "## Allowed" not in page and "Expected" not in page, page
        opts = engine.Options(seed=1, against=engine.AgainstStudent(), tests_only=False)
        v = engine.Engine.new(c, u, opts).check(selftest.Silent())
        assert v.ok and (v.passed, v.total) == (1, 1)
        spec = s.path / ".proctor" / "units" / "d00-intro.yaml"
        write_text(spec, read_text(spec) + "functions:\n  - { name: f, sig: 'def f() -> int' }\n")
        problems = engine.validate(course.CourseDir.load(s.path))
        assert any("a reading unit has no functions" in p for p in problems), problems


READING_INTRO_COURSE = [
    (
        ".proctor/course.yaml",
        "title: Intro\nunits: [p01.1-intro, p01.2-words]\n"
        "projects:\n  p01-words: { title: Words, goal: Count words. }\n",
    ),
    (
        ".proctor/units/p01.1-intro.yaml",
        "id: p01.1-intro\ntitle: Intro\nproject: p01-words\nlanguage: python\nreading: true\n"
        "target: program\nscaffold: none\ngoal: Read the introduction.\n"
        "lesson: |\n  ### Welcome\n\n  Read this.\n",
    ),
    (
        ".proctor/units/p01.2-words.yaml",
        """id: p01.2-words
title: Words
project: p01-words
language: python
target: function
module: wordfreq
stubs: wordfreq.py
goal: Split words.
functions:
  - { name: words, sig: "def words(text: str) -> list[str]" }
cases: |
  words("a b") == ["a", "b"]
  words("") == []
""",
    ),
    ("solutions/p01.2-words/wordfreq.py", "def words(text):\n    return text.split()\n"),
]


def test_a_function_stage_after_a_reading_introduction_needs_no_program() -> None:
    with Scratch("intro") as s:
        for rel, body in READING_INTRO_COURSE:
            s.write(rel, body)
        c = course.CourseDir.load(s.path)
        assert engine.validate(c) == []
        u = c.unit("p01.2-words")
        assert u is not None
        if (why := missing_tool(c, u)) is not None:
            pytest.skip(why)
        r = selftest.unit(c, u, 1)
        assert r.error is None, r.error
        assert not r.vacuous, r.vacuous
        assert_passes(c, u, s.path / "solutions" / u.id)


LAW_UNIT = """id: d01-twice
title: Twice
language: python
target: function
module: m
stubs: m.py
goal: Double a number.
functions:
  - { name: double, sig: "def double(n: int) -> int" }
cases: |
  double(2) == 4
random:
  laws:
"""


def test_a_law_must_use_what_it_generates() -> None:
    with Scratch("laws") as s:
        s.write(".proctor/course.yaml", "title: Laws\nunits: [d01-twice]\n")
        spec = ".proctor/units/d01-twice.yaml"
        law = "    - { name: twice, gen: { n: int 0..9, k: int 0..9 }, check: ['double(n) == n + n'] }\n"
        s.write(spec, LAW_UNIT + law)
        problems = engine.validate(course.CourseDir.load(s.path))
        assert any("generates `k` but no check uses it" in p for p in problems), problems
        s.write(spec, LAW_UNIT + "    - { name: fixed, check: ['double(1) == 2'] }\n")
        problems = engine.validate(course.CourseDir.load(s.path))
        assert any("generates nothing" in p for p in problems), problems
        law = "    - { name: twice, gen: { n: int 0..9 }, check: ['all(double(x) == x + x for x in [n])'] }\n"
        s.write(spec, LAW_UNIT + law)
        assert engine.validate(course.CourseDir.load(s.path)) == []
