from __future__ import annotations

import pytest
from proctor.compat import parse_int, split_whitespace
from proctor.errors import Err, Error, Ok, Result
from proctor.expr import Args, Arity, EvalError, EvalErrorStudent, ExprField, Host
from proctor.value import NULL, Value, ValueBool, ValueChar, ValueInt, ValueList, ValueStr

from proctor import expr


def test_constants_and_builtins() -> None:
    def c(s: str) -> Value:
        return expr.constant(s)

    assert c("1 + 2 * 3") == ValueInt(7)
    assert c(r'join(split("a,,b", ","), "-")') == ValueStr("a--b")
    assert c(r'nonempty(split("a,,b", ","))') == expr.constant(r'["a", "b"]')
    assert c(r'len("héllo")') == ValueInt(5)
    assert c(r'bytes("héllo")') == ValueInt(6)
    assert c("sorted([3, 1, 2]) == [1, 2, 3]") == ValueBool(True)
    assert c(r'{"a": 1}.a') == ValueInt(1)
    assert c("[1, 2, 3][-1]") == ValueInt(3)
    assert c("!(1 < 2) || 2 >= 2") == ValueBool(True)
    assert c(r'permutation("abc", "cab")') == ValueBool(True)
    assert c("','") == ValueChar(",")
    assert c("Ok(80) == 80") == ValueBool(True)
    assert c("Some([1, 2])") == expr.constant("[1, 2]")
    with pytest.raises(Error):
        expr.constant("1 +")
    with pytest.raises(Error):
        expr.constant("nope(1)")


def test_dotted_names_lex_as_one_ident() -> None:
    e = expr.parse("logstat.count_levels([]) == {}")
    names: list[str] = []
    expr.calls(e, names)
    assert names == ["logstat.count_levels"]
    e = expr.parse('run(stdin="x").out')
    assert isinstance(e, ExprField) and e.name == "out"


class Fake(Host):
    """Student functions for the corpus: an `Err` is a raise, as in harnesses."""

    def var(self, name: str) -> Value | None:
        return ValueStr("one  two") if name == "s" else None

    def call(self, name: str, args: Args) -> Value | None:
        a = [v for _, v in args]

        def txt(i: int) -> str:
            t = a[i].text()
            return t if t is not None else ""

        match name:
            case "ft_parse_port":
                n = parse_int(txt(0))
                if n is not None and n > 0:
                    return ValueInt(n)
                raise EvalErrorStudent("port")
            case "ft_find":
                try:
                    lst = expr.list_(a[0], name)
                except EvalError:
                    lst = []
                pos = next((i for i, x in enumerate(lst) if x == a[1]), None)
                return NULL if pos is None else ValueInt(pos)
            case "ft_split":
                return ValueList([ValueStr(w) for w in split_whitespace(txt(0))])
            case "ft_pair":
                return ValueList([NULL, ValueList([])])
        return None

    def is_external(self, name: str) -> bool:
        return name.startswith("ft_")


def fns() -> list[Arity]:
    return [
        (n, k, k)
        for n, k in [
            ("ft_parse_port", 1),
            ("ft_find", 2),
            ("ft_split", 1),
            ("ft_pair", 0),
        ]
    ]


def holds(src: str) -> Result[bool]:
    try:
        e = expr.resolve_methods(expr.parse(src), fns())
        expr.static_check(e, fns(), ["s"])
    except Error as err:
        return Err(err.msg)
    try:
        v = expr.eval(e, Fake())
    except EvalError as err:
        return Err(err.msg)
    return Ok(v.truthy())


def test_corpus_of_language_habits() -> None:
    """Lines models really wrote, in the habits of their languages. Each one
    must parse, pass the static check and hold. Add new idioms here."""
    corpus = [
        # Rust
        r'ft_parse_port("80") == Ok(80)',
        r'ft_parse_port("0") == Err("port must not be zero")',
        r'ft_parse_port("0").is_err()',
        r'ft_parse_port("8").is_ok()',
        r"ft_find(&[], 1) == None",
        r"ft_find(&vec![1, 2], 2) == Some(1)",
        r"ft_find(&[1, 2], 2).is_some()",
        r"ft_find(&mut vec![], 2).is_none()",
        r"ft_pair() == (None, vec![])",
        r"ft_pair() == (None, Vec::new())",
        r"ft_pair().1.is_empty()",
        r'ft_split(&String::from("a b")) == vec!["a".to_string(), "b".to_string()]',
        r'ft_split("a b").len() == 2usize',
        r'ft_split(r"a\b") == [r"a\b"]',
        r'ft_split(r#"x"y"#) == ["x\"y"]',
        r"all(ft_split(s), nonempty)",
        r"s.split_whitespace().len() == 2",
        r"s.len() == 8",
        r's.starts_with("one")',
        # Python
        r"ft_find([], 1) is None",
        r"ft_find([1], 1) is not None",
        r'ft_split("a b") == ["a", "b"] and True',
        r'"b" in ft_split("a b")',
        r'"z" not in ft_split("a b")',
        r'" ".join(ft_split("a  b")) == ' + "'a b'",
        r"ft_split('caf\u00e9') == " + r'["caf\u{e9}"]',
        r'ft_split("caf\xe9") == ["café"]',
        r'ft_split("\U0001F600") == ["😀"]',
        # JavaScript / Java / C
        r"ft_find([], 1) === null",
        r"ft_find([5], 5) !== undefined",
        r'ft_split("a b").length == 2',
        r'ft_split("a b").size() == 2',
        r'ft_split("A")[0].toLowerCase() == "a"',
        r"ft_find([1], 1) == 0L",
        r'ft_split("\101") == ["A"]',
        r'ft_split("\uD83D\uDE00") == ["😀"]',
        r"ft_find([1], 2) == NULL",
        r"ft_find([1], 2) == nil",
    ]
    for line in corpus:
        assert holds(line) == Ok(True), line


def test_static_check_explains() -> None:
    """Mistakes are reported before anything runs, with the fix."""

    def err(src: str) -> str:
        r = holds(src)
        assert isinstance(r, Err), src
        return r.msg

    assert "closest: `ft_split`" in err("ft_spilt(s) == []")
    assert "takes 2 argument(s)" in err("ft_find([1]) == null")
    assert "unknown name `x`" in err("ft_split(x) == []")
    assert "closest: `nonempty`" in err("all(ft_split(s), nonempty_list)")
    assert "only built-ins work as methods" in err("s.frobnicate()")
    assert "beside `==`" in err("raises(Err(1))")
    # A case has no variables at all.
    e = expr.parse("ft_split(s) == []")
    with pytest.raises(Error) as ei:
        expr.static_check(e, fns(), [])
    assert "no variables" in ei.value.msg


def test_comprehensions_state_properties_of_every_element() -> None:
    assert holds("[len(w) for w in ft_split(s)] == [3, 3]") == Ok(True)
    assert holds("[w for w in ft_split(s) if w != 'one'] == ['two']") == Ok(True)
    assert holds("all(len(w) <= 3 for w in ft_split(s))") == Ok(True)
    assert holds("any(w.upper() == 'TWO' for w in ft_split(s))") == Ok(True)
    assert holds("max([len(c) for c in chars(s)]) == 1") == Ok(True)
    assert holds("[[w, c] for w in ft_split(s) for c in w]") == Err("expected `]`")
    # The variable is bound inside its comprehension only.
    r = holds("[w for w in ft_split(s)] == w")
    assert isinstance(r, Err) and "unknown name `w`" in r.msg
    assert holds("[1, 2,] == [1, 2] and [] == []") == Ok(True)
    with pytest.raises(Error, match="a variable name after `for`"):
        expr.parse("[x for 1 in s]")
    with pytest.raises(Error, match="expected `\\)`"):
        expr.parse("all(x for x in s, 2)")


def test_err_comparisons_mean_raises() -> None:
    assert holds(r'ft_parse_port("80") == Err(1)') == Ok(False)
    assert holds(r'ft_parse_port("0") != Err(1)') == Ok(False)
    assert holds(r'ft_parse_port("80") != Err(1)') == Ok(True)
