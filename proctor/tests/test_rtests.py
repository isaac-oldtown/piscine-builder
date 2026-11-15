"""Tests written in Rust: finding them, what they call, libtest's verdicts,
methods in signatures, and the stubs built for them. No cargo needed."""

from __future__ import annotations

from pathlib import Path

import pytest
from common import Scratch
from proctor.compat import read_text
from proctor.errors import Error
from proctor.scan import (
    ProtoFindingMissing,
    ProtoFindingOk,
    Source,
    find_prototype,
    impl_blocks,
    strip,
)

from proctor import lang, rtests, sig

SUITE = """use std::collections::HashMap;

// example
#[test]
fn push_then_pop() {
    let mut s = Stack::new();
    s.push(1); // a comment with fn fake() {
    assert_eq!(s.pop(), Some(1), "{}", "}");
}

#[test]
#[should_panic]
fn pop_on_empty() {
    Stack::new().pop().unwrap();
}

fn helper() -> usize {
    total(&[1, 2])
}

// not an example
#[test]
fn uses_helper() {
    assert_eq!(helper(), 3);
}

mod inner {
    #[test]
    fn nested() {}
}
"""


def test_top_level_tests_are_found_in_order() -> None:
    tests = rtests.parse(SUITE)
    assert [t.name for t in tests] == ["push_then_pop", "pop_on_empty", "uses_helper"]
    assert [t.example for t in tests] == [True, False, False]
    assert tests[0].text.startswith("#[test]\nfn push_then_pop() {")
    assert tests[0].text.endswith('Some(1), "{}", "}");\n}')
    assert tests[1].text.startswith("#[test]\n#[should_panic]\nfn pop_on_empty()")
    assert rtests.problem(SUITE, tests) == (
        "every #[test] function must be at the top level, not inside a `mod`"
    )
    top = SUITE.split("mod inner")[0]
    assert rtests.problem(top, rtests.parse(top)) is None
    assert rtests.problem("fn main() {}\n", []) == "no top-level #[test] function"
    twice = "#[test]\nfn a() {}\n#[test]\nfn a() {}\n"
    assert rtests.problem(twice, rtests.parse(twice)) == "two tests are named `a`"


def test_what_a_test_calls() -> None:
    names = ["Stack::new", "Stack::push", "Stack::pop", "Stack::len", "total"]
    first, second, third = rtests.parse(SUITE.split("mod inner")[0])
    assert rtests.calls(first.code, names) == ["Stack::new", "Stack::push", "Stack::pop"]
    assert rtests.calls(second.code, names) == ["Stack::new", "Stack::pop"]
    # `helper` calls `total`, but the test itself does not.
    assert rtests.calls(third.code, names) == []
    assert rtests.calls("{ v.total(); my_total(1); total(&v); }", ["total"]) == ["total"]
    assert rtests.calls("{ let f = Stack::len; }", names) == ["Stack::len"]


def test_libtest_failures_on_one_line() -> None:
    ws = Path(r"C:\work\d01-stack")
    assert_failed = """
running 1 test
test push_then_pop ... FAILED

failures:

---- push_then_pop stdout ----

thread 'push_then_pop' (31512) panicked at tests\\t_d01_stack.rs:15:5:
assertion `left == right` failed: one plus one
  left: 2
 right: 3
note: run with `RUST_BACKTRACE=1` environment variable to display a backtrace


failures:
    push_then_pop

test result: FAILED. 0 passed; 1 failed; 0 ignored; 0 measured; 5 filtered out; finished in 0.00s
"""
    assert rtests.failure(assert_failed, "push_then_pop", ws) == (
        "assertion `left == right` failed: one plus one; left: 2; right: 3"
    )
    in_student_code = """
---- todo_pop stdout ----

thread 'todo_pop' (25848) panicked at C:\\work\\d01-stack\\src\\lib.rs:6:44:
not yet implemented
note: run with `RUST_BACKTRACE=1` environment variable to display a backtrace


failures:
"""
    assert rtests.failure(in_student_code, "todo_pop", ws) == (
        "not yet implemented (at src/lib.rs:6:44)"
    )
    no_panic = """
---- no_panic stdout ----
note: test did not panic as expected at tests\\t_d01_stack.rs:32:4

failures:
"""
    assert rtests.failure(no_panic, "no_panic", ws) == "test did not panic as expected"
    assert rtests.failure("running 1 test\ntest overflow ... ", "overflow", ws) is None
    assert rtests.target_name("p01.2-cli") == "t_p01_2_cli"


def test_methods_in_signatures() -> None:
    new = sig.parse("rust", "pub fn new() -> Self", "Stack::new")
    assert (new.name, new.short_name(), new.owner(), new.norm) == (
        "Stack::new",
        "new",
        "Stack",
        "fn new() -> Stack",
    )
    assert new.sig_text == "pub fn new() -> Self"
    push = sig.parse("rust", "pub fn push(&mut self, x: i32)", "Stack::push")
    assert push.is_method and [p.name for p in push.params] == ["x"]
    with pytest.raises(Error, match="does not declare `pop`"):
        sig.parse("rust", "pub fn push(&mut self, x: i32)", "Stack::pop")
    with pytest.raises(Error, match="is an associated function"):
        lang.harness_supports("rust", new)
    with pytest.raises(Error, match="is a method"):
        lang.harness_supports("rust", push)
    assert sig.parse("python", "def f(x)", "m.f").owner() is None


def test_a_method_is_found_only_in_its_type_impl() -> None:
    def src(text: str) -> Source:
        return Source(rel="src/lib.rs", raw=text, code=strip("rust", text))

    new = sig.parse("rust", "pub fn new() -> Stack", "Stack::new")
    good = src(
        "pub struct Stack {}\n#[derive(Debug)]\nstruct Other;\nimpl Other { pub fn new() -> Self { Other } }\n"
        "impl Stack {\n    pub fn new() -> Self { Stack {} }\n}\n"
    )
    assert isinstance(find_prototype("rust", [good], new), ProtoFindingOk)
    elsewhere = src(
        "pub struct Stack {}\nimpl Other { pub fn new() -> Stack { Stack {} } }\n"
        "pub fn new() -> Stack { Stack {} }\n"
    )
    assert isinstance(find_prototype("rust", [elsewhere], new), ProtoFindingMissing)
    generic = src(
        "impl<T: Clone> Stack<T> where T: Default {\n    fn new() -> Self { todo!() }\n}\n"
    )
    assert isinstance(find_prototype("rust", [generic], new), ProtoFindingOk)
    # `-> impl Trait` in a signature is not an impl block.
    code = strip("rust", "fn it() -> impl Iterator<Item = Stack> { std::iter::empty() }\n")
    assert impl_blocks(code, "Stack") == []


def test_stubs_put_methods_in_an_impl_of_their_type() -> None:
    protos = [
        sig.parse("rust", "pub fn total(v: &[i32]) -> i32", "total"),
        sig.parse("rust", "pub fn new() -> Self", "Stack::new"),
        sig.parse("rust", "pub fn push(&mut self, x: i32)", "Stack::push"),
    ]
    assert lang.stub_source("rust", protos) == (
        "/// TODO\npub fn total(v: &[i32]) -> i32 {\n    let _ = v;\n    todo!()\n}\n\n"
        "/// TODO\npub struct Stack {}\n\n"
        "impl Stack {\n"
        "    /// TODO\n    pub fn new() -> Self {\n        todo!()\n    }\n\n"
        "    /// TODO\n    pub fn push(&mut self, x: i32) {\n        let _ = x;\n        todo!()\n    }\n"
        "}\n"
    )
    later = lang.stub_source("rust", protos[2:], {"Stack"})
    assert later is not None and "pub struct" not in later and later.startswith("impl Stack {")


def test_the_reference_is_stubbed_where_the_functions_are() -> None:
    with Scratch("rstubs") as s:
        s.write("ref/Cargo.toml", '[package]\nname = "st"\n')
        s.write(
            "ref/src/lib.rs",
            "pub struct Stack { v: Vec<i32> }\n\n"
            "impl Stack {\n    pub fn new() -> Self {\n        Stack { v: vec![] }\n    }\n\n"
            "    pub fn len(&self) -> usize {\n        self.v.len()\n    }\n}\n\n"
            "impl Other {\n    pub fn len(&self) -> usize {\n        0\n    }\n}\n\n"
            'pub fn total(v: &[i32]) -> i32 {\n    let s = "}";\n    v.iter().sum()\n}\n',
        )
        protos = [
            sig.parse("rust", "pub fn new() -> Self", "Stack::new"),
            sig.parse("rust", "pub fn len(&self) -> usize", "Stack::len"),
            sig.parse("rust", "pub fn total(v: &[i32]) -> i32", "total"),
            sig.parse("rust", "pub fn gone(&self)", "Stack::gone"),
        ]
        missing = rtests.stub_reference(s.path / "ref", s.path / "tree", protos)
        assert missing == ["Stack::gone"]
        assert read_text(s.path / "tree" / "src" / "lib.rs") == (
            "pub struct Stack { v: Vec<i32> }\n\n"
            "impl Stack {\n    pub fn new() -> Self {\n    todo!()\n}\n\n"
            "    pub fn len(&self) -> usize {\n    todo!()\n}\n}\n\n"
            "impl Other {\n    pub fn len(&self) -> usize {\n        0\n    }\n}\n\n"
            "pub fn total(v: &[i32]) -> i32 {\n    todo!()\n}\n"
        )
        assert read_text(s.path / "tree" / "Cargo.toml") == '[package]\nname = "st"\n'
