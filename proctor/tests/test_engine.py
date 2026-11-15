"""engine.rs has no test module; these cover its pure parts. The expected
test names and messages were taken from the Rust tool's output."""

from __future__ import annotations

from pathlib import Path

import pytest
from proctor.errors import Error
from proctor.unit import Unit
from proctor.value import Value, ValueInt

from proctor import course, engine, gens, schema, sig

DEMO = Path(__file__).resolve().parents[2] / "examples" / "demo-course"


def demo() -> course.CourseDir:
    return course.CourseDir.load(DEMO)


def test_the_demo_course_validates() -> None:
    assert engine.validate(demo()) == []


def test_test_names_of_the_demo_units() -> None:
    c = demo()
    u = c.unit("d01-string-basics")
    assert u is not None
    chain = c.chain(u)
    callable = engine.protos_for(c, chain) + engine.helpers_for(c, chain)
    tests = engine.tests_of(c, u, callable)
    assert [t.name for t in tests] == [
        'case str_len("") == 0',
        'case str_len("abc") == 3',
        'case str_dup("") == ""',
        'case str_dup("hello") == "hello"',
        'case str_split("a,,b", \',\') == ["a", "b"]',
        "case str_split(\"\", ',') == []",
        "case str_split(\",,,\", ',') == []",
        'case then(fail_alloc_after(0), str_dup("abc")) == null',
        "case then(fail_alloc_after(0), str_split(\"a,b\", ',')) == null",
        "case then(fail_alloc_after(2), str_split(\"a,b\", ',')) == null",
        "fuzz str_len",
        "fuzz str_dup",
        "fuzz str_split",
        "law split keeps the non-empty fields",
    ]
    assert [t.is_random() for t in tests] == [False] * 10 + [True] * 4
    fuzz = tests[12].kind
    assert isinstance(fuzz, engine.KindFuzz)
    assert [n for n, _ in fuzz.params] == ["s", "sep"]
    assert fuzz.rounds == engine.DEFAULT_ROUNDS


def test_program_case_names_are_cut_at_70_characters() -> None:
    c = demo()
    u = c.unit("p01.2-cli")
    assert u is not None
    tests = engine.tests_of(c, u, engine.protos_for(c, c.chain(u)))
    assert [t.name for t in tests] == [
        'case <<< "2026-01-02 10:00:00 INFO a\\n2026-01-02 10:00:01 ERROR b\\n2026-01-...',
        'case <<< "" => out empty exit 0',
        'case <<< "garbage\\n2026-01-02 10:00:00 WARN w\\n" => out == "WARN 1\\n" err ~...',
        "fuzz report",
        "law blank lines do not change the report",
    ]
    fuzz = tests[3].kind
    assert isinstance(fuzz, engine.KindFuzzProg)
    assert fuzz.argv is None and fuzz.stdin is not None and fuzz.rounds == 20


LAW_UNIT = """
id: u1
title: Codec
language: python
goal: encode
module: codec
functions:
  - { name: encode, sig: "def encode(s: str) -> str" }
  - { name: decode, sig: "def decode(s: str) -> str" }
  - { name: norm, sig: "def norm(s: str) -> str" }
  - { name: pair, sig: "def pair(a: int, b: int) -> int" }
random:
  laws:
"""


def law_tests(laws: str) -> list[engine.Test]:
    c = demo()
    u = schema.load(Unit, LAW_UNIT + laws)
    protos = [sig.parse("python", f.sig, f.name) for f in u.functions]
    return engine.tests_of(c, u, protos)


def law_checks(t: engine.Test) -> list[str]:
    assert isinstance(t.kind, engine.KindLaw)
    return [src for src, _ in t.kind.checks]


def test_law_shorthands_become_checks() -> None:
    tests = law_tests(
        "    - { roundtrip: [encode, decode] }\n"
        "    - { idempotent: norm }\n"
        "    - { agree: [pair, pair], rounds: 0 }\n"
        "    - { name: mine, roundtrip: [encode, decode], check: ['len(s) >= 0'] }\n"
    )
    assert [t.name for t in tests] == [
        "law roundtrip encode / decode",
        "law idempotent norm",
        "law agree pair / pair",
        "law mine",
    ]
    assert law_checks(tests[0]) == ["decode(encode(s)) == s"]
    assert law_checks(tests[1]) == ["norm(norm(s)) == norm(s)"]
    assert law_checks(tests[2]) == ["pair(a, b) == pair(a, b)"]
    assert law_checks(tests[3]) == ["len(s) >= 0", "decode(encode(s)) == s"]
    kind = tests[2].kind
    assert isinstance(kind, engine.KindLaw)
    assert [n for n, _ in kind.vars] == ["a", "b"]
    # Zero rounds still run once.
    assert kind.rounds == 1


@pytest.mark.parametrize(
    ("law", "msg"),
    [
        ("{ roundtrip: [encode] }", "u1: roundtrip takes [f, g]"),
        ("{ agree: [pair, pair, pair] }", "u1: agree takes [f, g]"),
        ("{ idempotent: pair }", "u1: idempotent `pair` must take exactly one argument"),
        ("{ idempotent: nope }", "u1: law uses `nope`, which is not an expected function"),
        ("{ name: empty }", "u1: law #1 has no check"),
        (
            "{ check: ['encode(x) == x'] }",
            "u1: law check `encode(x) == x`: unknown name `x`: a case has no variables, write the value itself",
        ),
    ],
)
def test_law_spec_errors(law: str, msg: str) -> None:
    with pytest.raises(Error) as e:
        law_tests(f"    - {law}\n")
    assert e.value.msg == msg


def item(ok: bool, name: str = "x", detail: str = "d") -> engine.Item:
    return engine.Item(ok=ok, name=name, detail=detail, random=False, course_error=False)


def test_phase_summaries() -> None:
    def summary(ok: int, total: int) -> str:
        return f"{ok} of {total}"

    p = engine.phase("build", [item(True), item(True)], False, summary)
    assert (p.status, p.summary) == (engine.Status.PASS, "2 of 2")
    p = engine.phase("tests", [item(True), item(False)], True, summary)
    assert (p.status, p.summary) == (engine.Status.FAIL, "1/2")
    p = engine.phase("build", [item(False, "compile", "src/a.c:1: error: x")], False, summary)
    assert p.summary == "src/a.c:1: error: x"
    p = engine.phase(
        "structure", [item(True), item(False, "a.txt", "is unexpected")], False, summary
    )
    assert p.summary == "a.txt is unexpected"
    p = engine.phase("allowed", [item(False), item(False), item(False)], False, summary)
    assert p.summary == "3 problems"
    p = engine.phase("exit", [], False, summary)
    assert (p.status, p.summary) == (engine.Status.PASS, "0 of 0")


def test_relativize() -> None:
    base = Path("/work/ws")
    b = str(base)
    log = f"{b}/src/a.rs:1 and {b}\\src\\b.rs:2 and \\\\?\\C:\\x"
    assert engine.relativize(log, base) == "src/a.rs:1 and src/b.rs:2 and C:/x"


def test_shrinking_keeps_the_smallest_failing_value() -> None:
    g = gens.GenInt(lo=0, hi=1000, width=0)
    values: list[tuple[str, Value]] = [("n", ValueInt(900))]

    def fails(trial: list[tuple[str, Value]]) -> bool:
        v = trial[0][1]
        return isinstance(v, ValueInt) and v.i >= 37

    engine.shrink([("n", g)], values, fails)
    assert values == [("n", ValueInt(37))]


def test_shrinking_stops_at_its_budget() -> None:
    g = gens.GenInt(lo=0, hi=2000, width=0)
    values: list[tuple[str, Value]] = [("n", ValueInt(1000))]
    calls = 0

    # Only one step down at a time: three candidates are tried per step.
    def fails(trial: list[tuple[str, Value]]) -> bool:
        nonlocal calls
        calls += 1
        cur, new = values[0][1], trial[0][1]
        assert isinstance(cur, ValueInt) and isinstance(new, ValueInt)
        return new.i == cur.i - 1

    engine.shrink([("n", g)], values, fails)
    assert calls == engine.SHRINK_BUDGET
    assert values == [("n", ValueInt(900))]


def verdict_of(phases: list[engine.Phase]) -> engine.Verdict:
    e = engine.Engine.__new__(engine.Engine)
    return e.verdict(phases, 2)


def test_verdict_counts_items_outside_skipped_phases() -> None:
    p1 = engine.phase("build", [item(True, "compile")], False, lambda _o, _t: "ok")
    p2 = engine.phase("tests", [item(True), item(False, "t2", "boom")], True, lambda o, t: "")
    skip = engine.Phase(
        name="regression",
        status=engine.Status.SKIP,
        summary="",
        items=[item(False)],
        streamed=False,
    )
    v = verdict_of([p1, p2, skip])
    assert (v.ok, v.passed, v.total, v.warnings) == (False, 2, 3, 2)
    assert v.first_fail is not None
    assert v.first_fail[0] == "tests" and v.first_fail[1].name == "t2"
    v = verdict_of([p1])
    assert (v.ok, v.passed, v.total, v.first_fail) == (True, 1, 1, None)
    # A failing phase without a failing item still fails the verdict.
    failed = engine.Phase(
        name="structure", status=engine.Status.FAIL, summary="", items=[], streamed=False
    )
    assert not verdict_of([failed]).ok
