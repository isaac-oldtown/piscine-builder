"""Checks a unit's tests without any solution, two ways:

- an independent reading: a second model call sees the student's instructions,
  never the tests, and works out what each case expects and whether each law
  holds; `items` asks the questions and `Item.check` compares the answers;
- consistency: the fixed cases, read as a table of known results, must satisfy
  the unit's laws, and two cases with the same input must expect the same
  result (`conflicts`)."""

from __future__ import annotations

import dataclasses
import itertools
import json
import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from proctor.compat import trim
from proctor.errors import Error
from proctor.unit import Target, Unit
from proctor.value import Value, ValueBool, ValueList, ValueStr

from coursecrafter import llm
from proctor import course, engine, expr, program, render, rtests, rules, yamlio

# Assignments of a law's variables tried against the cases.
MAX_COMBINATIONS = 400


@dataclass(kw_only=True)
class Item:
    """One test the independent reading answers about."""

    # What identifies it in the unit: the case line, the law's YAML, the Rust test's source.
    key: str
    # How the test writer sees it.
    label: str
    question: str
    # The answer -> `None` when it agrees with the test, else why not.
    check: Callable[[str], str | None]


def page(c: course.CourseDir, u: Unit) -> str:
    """The student's instructions for `u` without the examples drawn from its
    tests, so the reading cannot copy their results."""
    bare = dataclasses.replace(u, cases=None, rust_tests=None)
    return render.unit_md(c, bare)


def context(c: course.CourseDir, u: Unit) -> tuple[str, list[str], str]:
    """The unit's page, the earlier stages' pages (their examples are checked
    facts by now) and RULES.md."""
    earlier = [render.unit_md(c, x) for x in c.chain(u)[:-1]]
    return page(c, u), earlier, rules.rules_md(c)


def items(c: course.CourseDir, u: Unit, laws: list[Any]) -> list[Item]:
    """Questions for the cases, tests in Rust and laws of `u`; `laws` are its
    laws as written in the unit file."""
    chain = c.chain(u)
    protos = engine.protos_for(c, chain)
    helpers = engine.helpers_for(c, chain)
    ars = engine.arities(protos + helpers)
    helper_names = {p.name for p in helpers}
    out: list[Item] = []
    for n, line in u.case_lines():
        label = f"case line {n} `{line}`"
        if u.target == Target.FUNCTION:
            try:
                e = engine.parse_check(line, ars, [])
            except Error:
                continue
            called: list[str] = []
            expr.calls(e, called)
            # Test-only helpers have code the reading never sees.
            if helper_names & set(called):
                continue
            split = render.split_eq(line)
            if split is not None and _constant(split[1]) is not None:
                out.append(
                    Item(
                        key=line,
                        label=label,
                        question=f"the value of `{split[0]}`",
                        check=_value_check(split[1]),
                    )
                )
            else:
                out.append(
                    Item(key=line, label=label, question=f"true or false: `{line}`", check=_truth)
                )
        else:
            try:
                case = program.parse_case(line)
            except Error:
                continue
            out.append(
                Item(key=line, label=label, question=_run_question(case), check=_run_check(case))
            )
    if u.rust_tests is not None and u.has_rust_tests():
        for t in rtests.parse(u.rust_tests):
            out.append(
                Item(
                    key=t.text,
                    label=f"Rust test `{t.name}`",
                    question=f"true or false: this test passes against every correct solution:\n```rust\n{trim(t.text)}\n```",
                    check=_truth,
                )
            )
    for i, law in enumerate(laws):
        text = yamlio.dump(law)
        name = law.get("name") if isinstance(law, dict) else None
        out.append(
            Item(
                key=text,
                label=f"law `{name}`" if isinstance(name, str) else f"law #{i + 1}",
                question=f"true or false: this property holds for every correct solution and every generated input:\n```yaml\n{trim(text)}\n```",
                check=_truth,
            )
        )
    return out


def questions(its: list[Item]) -> str:
    return "\n".join(f"{i + 1}. {it.question}" for i, it in enumerate(its))


def answers(reply: str) -> dict[int, str]:
    """Numbered answers of the reply's <answers> block."""
    body = llm.block(reply, "answers")
    if body is None:
        return {}
    out: dict[int, str] = {}
    for line in body.splitlines():
        m = re.match(r"\s*(\d+)\s*[:.)]\s?(.*)$", line)
        if m:
            out[int(m.group(1))] = trim(m.group(2))
    return out


def disputes(its: list[Item], got: dict[int, str]) -> dict[str, str | None]:
    """Each item's key -> `None` when the reading agrees, else why not."""
    out: dict[str, str | None] = {}
    for i, it in enumerate(its):
        a = got.get(i + 1)
        out[it.key] = "gave no answer" if a is None else it.check(a)
    return out


# ------------------------------------------------------------------ answers


def _undecided(a: str) -> bool:
    return a.strip("`").strip() == "?"


_OPEN = "says the instructions leave it open (two correct solutions could differ)"


def _constant(src: str) -> Value | None:
    try:
        return expr.constant(src)
    except Error:
        return None


def _value_check(expected: str) -> Callable[[str], str | None]:
    def check(a: str) -> str | None:
        a = a.strip("`").strip()
        if _undecided(a):
            return _OPEN
        same = _constant(f"({a}) == ({expected})")
        if same == ValueBool(True):
            return None
        return f"finds `{a}` where the case expects `{expected}`"

    return check


def _truth(a: str) -> str | None:
    a = a.strip("`").strip()
    low = a.lower()
    if low.startswith("true"):
        return None
    if _undecided(a):
        return _OPEN
    if low.startswith("false"):
        why = trim(a[5:].lstrip(",:;"))
        return "finds it false" + (f" ({why})" if why else "")
    return f"answered `{a}`"


def _quote(s: str) -> str:
    return json.dumps(s, ensure_ascii=False)


def _run_question(case: program.Case) -> str:
    parts = [
        f"arguments {json.dumps(case.args, ensure_ascii=False)}" if case.args else "no arguments"
    ]
    for name, body in case.files:
        parts.append(f"a file `{name}` holding {_quote(body)}")
    parts.append(f"stdin {_quote(case.stdin)}" if case.stdin is not None else "empty stdin")
    return f"run the program with {', '.join(parts)}: its stdout, stderr and exit code"


def run_answer(a: str) -> tuple[str | None, str | None, int | None]:
    """`out "..." err "..." exit 0`, with `?` for a part left open."""
    got: dict[str, str | int | None] = {}
    i = 0
    a = a.strip("`")
    while i < len(a):
        m = re.compile(r"\s*,?\s*(out|stdout|err|stderr|exit)\b\s*[:=]?\s*").match(a, i)
        if m is None:
            break
        key = {"stdout": "out", "stderr": "err"}.get(m.group(1), m.group(1))
        i = m.end()
        if a[i : i + 1] == '"':
            try:
                s, i = expr.read_string(a, i)
            except Error:
                break
            got[key] = s
        elif a[i : i + 1] == "?":
            got[key] = None
            i += 1
        else:
            n = re.compile(r"-?\d+").match(a, i)
            if n is None:
                break
            got[key] = int(n.group(0))
            i = n.end()
    out, err, code = got.get("out"), got.get("err"), got.get("exit")
    return (
        out if isinstance(out, str) else None,
        err if isinstance(err, str) else None,
        code if isinstance(code, int) else None,
    )


def _run_check(case: program.Case) -> Callable[[str], str | None]:
    def check(a: str) -> str | None:
        out, err, code = run_answer(a)
        r = program.RunResult(
            out=out or "", err=err or "", exit=code, status="exited", stopped=False
        )
        for x in case.asserts:
            match x:
                case program.AssertExit():
                    known = code is not None
                    what = "its exit code"
                case (
                    program.AssertEq(stream=s)
                    | program.AssertNe(stream=s)
                    | program.AssertRe(stream=s)
                    | program.AssertNotRe(stream=s)
                    | program.AssertEmpty(stream=s)
                ):
                    known = (out if s == program.Stream.OUT else err) is not None
                    what = "its stdout" if s == program.Stream.OUT else "its stderr"
                case _:
                    continue
            if not known:
                return f"says the instructions leave {what} open"
            why = program.judge(program.Case(args=[], stdin=None, files=[], asserts=[x]), r)
            if why is not None:
                return f"finds a different result, so the check fails: {why}"
        return None

    return check


# -------------------------------------------------------------- consistency


class _Unknown(Exception):
    """A call whose result no case gives."""


class _Table(expr.Host):
    """The student's functions and program, answered from the cases."""

    def __init__(self, names: set[str]) -> None:
        self.names = names
        self.calls: list[tuple[str, str, list[Value], Value]] = []
        self.runs: list[tuple[str, list[str], str, program.RunResult]] = []
        self.vars: dict[str, Value] = {}
        self.used: list[str] = []

    def var(self, name: str) -> Value | None:
        return self.vars.get(name)

    def call(self, name: str, args: expr.Args) -> Value | None:
        if name == "run":
            argv, stdin = _run_args(args)
            for key, a, s, r in self.runs:
                if a == argv and s == stdin:
                    self.used.append(key)
                    return r.to_value()
            raise _Unknown
        if name in self.names:
            if any(n is not None for n, _ in args):
                raise _Unknown
            vals = [v for _, v in args]
            for ckey, f, given, result in self.calls:
                if f == name and _same_list(given, vals):
                    self.used.append(ckey)
                    return result
            raise _Unknown
        return None

    def is_external(self, name: str) -> bool:
        return name == "run" or name in self.names


def _same(a: Value, b: Value) -> bool:
    return a == b or _constant_eq(a, b)


def _constant_eq(a: Value, b: Value) -> bool:
    try:
        return expr.eval(
            expr.ExprBin(expr.BinOp.EQ, expr.ExprLit(a), expr.ExprLit(b)), _Table(set())
        ) == ValueBool(True)
    except (expr.EvalError, _Unknown):
        return False


def _same_list(a: list[Value], b: list[Value]) -> bool:
    return len(a) == len(b) and all(_same(x, y) for x, y in zip(a, b, strict=True))


def _text(v: Value) -> str:
    t = v.text()
    return t if t is not None else str(v)


def _run_args(args: expr.Args) -> tuple[list[str], str]:
    """`run(args=[...], stdin="...")` as `program.Program.call` reads it."""
    from proctor import run

    argv: list[str] = []
    stdin = ""
    for i, (name, v) in enumerate(args):
        if (name in ("args", None) and i == 0) or name == "args":
            if isinstance(v, ValueList):
                argv = [_text(x) for x in v.items]
            elif isinstance(v, ValueStr):
                argv = run.split_words(v.s)
            else:
                raise _Unknown
        elif name == "stdin" or (name is None and i == 1):
            stdin = _text(v)
        else:
            raise _Unknown
    return argv, stdin


def _table(c: course.CourseDir, u: Unit) -> tuple[_Table, list[tuple[list[str], str]]]:
    """The results the cases of `u`'s chain give, and the cases of `u` that
    contradict each other (same input, different result)."""
    chain = c.chain(u)
    protos = engine.protos_for(c, chain)
    ars = engine.arities(protos + engine.helpers_for(c, chain))
    t = _Table({p.name for p in protos})
    clashes: list[tuple[list[str], str]] = []
    for x in chain:
        own = x.id == u.id
        for _n, line in x.case_lines():
            key = line if own else f"{x.id}: {line}"
            if x.target == Target.FUNCTION:
                try:
                    e = engine.parse_check(line, ars, [])
                except Error:
                    continue
                if not (
                    isinstance(e, expr.ExprBin)
                    and e.op == expr.BinOp.EQ
                    and isinstance(e.left, expr.ExprCall)
                    and e.left.name in t.names
                    and all(n is None for n, _ in e.left.args)
                ):
                    continue
                vals = [_constant_value(a) for _, a in e.left.args]
                result = _constant_value(e.right)
                if result is None or any(v is None for v in vals):
                    continue
                args = [v for v in vals if v is not None]
                for k, f, a, r in t.calls:
                    if own and f == e.left.name and _same_list(a, args) and not _same(r, result):
                        clashes.append(([k, key], "the same call expects two different results"))
                t.calls.append((key, e.left.name, args, result))
            else:
                try:
                    case = program.parse_case(line)
                except Error:
                    continue
                full = _full_result(case)
                if case.files or full is None:
                    continue
                stdin = case.stdin or ""
                for rkey, argv, given, rr in t.runs:
                    same_input = argv == case.args and given == stdin
                    if (
                        own
                        and same_input
                        and (rr.out, rr.err, rr.exit) != (full.out, full.err, full.exit)
                    ):
                        clashes.append(([rkey, key], "the same run expects two different results"))
                t.runs.append((key, case.args, stdin, full))
    return t, clashes


def _constant_value(e: expr.Expr) -> Value | None:
    names: set[str] = set()
    expr.free_names(e, names)
    called: list[str] = []
    expr.calls(e, called)
    if names or any(n != "Ok" and n != "Some" for n in called):
        return None
    try:
        return expr.eval(e, _Table(set()))
    except (expr.EvalError, _Unknown):
        return None


def _full_result(case: program.Case) -> program.RunResult | None:
    """The run a case pins down completely: exact stdout, stderr and exit code."""
    out = err = None
    code: int | None = None
    for a in case.asserts:
        match a:
            case program.AssertEq(stream=program.Stream.OUT, text=s):
                out = s
            case program.AssertEq(stream=program.Stream.ERR, text=s):
                err = s
            case program.AssertEmpty(stream=program.Stream.OUT):
                out = ""
            case program.AssertEmpty(stream=program.Stream.ERR):
                err = ""
            case program.AssertExit(code=n):
                code = n
    if out is None or err is None or code is None:
        return None
    return program.RunResult(out=out, err=err, exit=code, status=f"exit {code}", stopped=False)


def conflicts(c: course.CourseDir, u: Unit, laws: list[Any]) -> list[tuple[list[str], str]]:
    """Tests that cannot all be right, as (keys, why): two cases of `u` with
    the same input, or a law of `u` that fails on inputs and results its cases
    (and the earlier stages' cases) give. A law failing with no case involved
    is wrong whatever the solution. `laws` are the laws as written."""
    t, out = _table(c, u)
    chain = c.chain(u)
    protos = engine.protos_for(c, chain) + engine.helpers_for(c, chain)
    try:
        tests = [x for x in engine.tests_of(c, u, protos) if isinstance(x.kind, engine.KindLaw)]
    except Error:
        return out
    pool: list[Value] = []

    def add(v: Value) -> None:
        if not any(_same(v, p) for p in pool):
            pool.append(v)

    for _, _, args, result in t.calls:
        for v in args:
            add(v)
        add(result)
    for _, argv, stdin, _ in t.runs:
        add(ValueStr(stdin))
        add(ValueList([ValueStr(a) for a in argv]))
        for a in argv:
            add(ValueStr(a))
    for test, law in zip(tests, laws, strict=False):
        kind = test.kind
        assert isinstance(kind, engine.KindLaw)
        choices = [[v for v in pool if g.admits(v) is True] for _, g in kind.vars]
        if not all(choices):
            continue
        names = [n for n, _ in kind.vars]
        for combo in itertools.islice(itertools.product(*choices), MAX_COMBINATIONS):
            t.vars = dict(zip(names, combo, strict=True))
            t.used = []
            failed: str | None = None
            try:
                for src, e in kind.checks:
                    if expr.eval(e, t) != ValueBool(True):
                        failed = src
                        break
            except (_Unknown, expr.EvalError):
                continue
            if failed is None:
                continue
            given = ", ".join(f"{n} = {v.short(60)}" for n, v in t.vars.items())
            key = yamlio.dump(law)
            used = list(dict.fromkeys(t.used))
            if used:
                lines = ", ".join(f"`{k}`" for k in used)
                why = f"`{test.name}` fails its check `{failed}` with {given}, using the results of the cases {lines}: the law or one of these cases is wrong"
            else:
                why = f"`{test.name}` fails its check `{failed}` with {given} whatever the solution does"
            out.append(([key, *used], why))
            break
    return out
