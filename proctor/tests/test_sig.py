from __future__ import annotations

import pytest
from proctor.errors import Error
from proctor.sig import TyChar, TyList, TyOpt, TyStr, TyStrList

from proctor import sig


def test_c_signatures_ignore_names_and_spacing() -> None:
    a = sig.parse("c", "size_t str_len(const char *s)", "str_len")
    b = sig.parse("c", "size_t   str_len ( const char*str );", "str_len")
    assert a.norm == b.norm
    assert a.norm == "size_t str_len(const char *)"
    assert a.params[0].ty == TyStr()
    s = sig.parse("c", "char **str_split(const char *s, char sep)", "str_split")
    assert s.norm == "char **str_split(const char *, char)"
    assert s.ret == TyStrList()
    assert s.params[1].ty == TyChar()
    v = sig.parse("c", "int f(void)", "f")
    assert v.norm == "int f(void)"
    assert not v.params
    arr = sig.parse("c", "int sum(int v[], size_t n)", "sum")
    assert arr.norm == "int sum(int *, size_t)"


def test_python_and_rust() -> None:
    p = sig.parse(
        "python",
        "def top(entries: Iterable[str], n: int = 3) -> list[str]",
        "logstat.top",
    )
    assert len(p.params) == 2
    assert p.params[0].ty == TyList(TyStr())
    assert p.params[1].has_default
    r = sig.parse("rust", "pub fn longest<'a>(text: &'a str) -> Option<&'a str>", "longest")
    assert r.norm == "fn longest(&str) -> Option<&str>"
    assert r.ret == TyOpt(TyStr())
    with pytest.raises(Error):
        sig.parse("rust", "fn a(x: u8)", "b")


def test_rust_lifetimes_arrows_and_where_clauses() -> None:
    r = sig.parse("rust", "fn longest<'a>(a: &'a str, b: &'a str) -> &'a str", "longest")
    assert r.norm == "fn longest(&str, &str) -> &str"
    assert [p.name for p in r.params] == ["a", "b"]
    assert r.params[1].ty == TyStr()
    w = sig.parse(
        "rust", "pub fn largest<T>(v: &[T]) -> T\nwhere\n    T: PartialOrd + Copy", "largest"
    )
    assert w.norm == "fn largest(&[T]) -> T"
    assert sig.parse("rust", "fn show<T>(x: T)\nwhere T: Debug", "show").norm == "fn show(T)"
    f = sig.parse("rust", "fn apply(f: impl Fn(i32) -> i32, x: i32) -> i32", "apply")
    assert [p.name for p in f.params] == ["f", "x"]


def test_names_need_not_be_ascii() -> None:
    assert (
        sig.parse("rust", "fn größe(s: &str) -> usize", "größe").norm == "fn größe(&str) -> usize"
    )
    assert sig.parse("python", "def größe(s: str) -> int", "größe").params[0].name == "s"
    assert sig.parse("c", "int größe(char *s)", "größe").norm == "int größe(char *)"
