"""`target: program`: running the student's whole program, and the one-line
case grammar
  [args...] [@file="content"] [<<< "stdin"] => out == "..." err~"regex" exit 0"""

from __future__ import annotations

import enum
import os
import re
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from proctor import expr, report, run
from proctor.command import Command
from proctor.compat import io_error, is_whitespace, lines, parse_int, regex, trim, write_text
from proctor.errors import Error
from proctor.value import NULL, Value, ValueInt, ValueList, ValueMap, ValueStr

_I32_MIN = -(1 << 31)
_I32_MAX = (1 << 31) - 1


@dataclass(kw_only=True)
class Program:
    cmd: list[str]
    cwd: Path
    env: dict[str, str]
    timeout: float

    def run(self, args: list[str], stdin: str | None, files: list[tuple[str, str]]) -> RunResult:
        created: list[Path] = []
        for name, body in files:
            p = self.cwd / name
            if os.path.exists(p):
                raise Error(f"input file `{name}` would overwrite a file of the workspace")
            try:
                p.parent.mkdir(parents=True, exist_ok=True)
            except OSError:
                pass
            try:
                write_text(p, body)
            except OSError as e:
                raise Error(f"{name}: {io_error(e)}") from None
            created.append(p)
        if not self.cmd:
            raise Error("empty run command")
        prog, base = self.cmd[0], self.cmd[1:]
        c = Command(prog)
        c.args(base).args(args).current_dir(self.cwd).envs(self.env)
        try:
            out = run.run(c, (stdin if stdin is not None else "").encode("utf-8"), self.timeout)
        finally:
            for p in created:
                try:
                    os.remove(p)
                except OSError:
                    pass
        return RunResult(
            out=out.stdout.replace("\r\n", "\n"),
            err=out.stderr.replace("\r\n", "\n"),
            exit=out.code,
            status=out.status_text,
            stopped=out.stopped,
        )

    def call(self, args: list[tuple[str | None, Value]]) -> Value:
        """`run(args=[...], stdin="...")` from the expression language."""
        argv: list[str] = []
        stdin = ""
        for i, (name, v) in enumerate(args):
            if (name in ("args", None) and i == 0) or name == "args":
                match v:
                    case ValueList(items):
                        argv = [_text_or_display(x) for x in items]
                    case ValueStr(s):
                        argv = run.split_words(s)
                    case other:
                        raise expr.EvalErrorSpec(
                            f"run: args must be a list, got {other.type_name()}"
                        )
            elif name == "stdin" or (name is None and i == 1):
                stdin = _text_or_display(v)
            elif name is not None:
                raise expr.EvalErrorSpec(f"run: unknown argument `{name}`")
            else:
                raise expr.EvalErrorSpec("run(args, stdin) takes at most 2 arguments")
        try:
            r = self.run(argv, stdin, [])
        except Error as e:
            raise expr.EvalErrorSpec(e.msg) from None
        if r.stopped:
            raise expr.EvalErrorStudent(f"program {r.status}")
        return r.to_value()


def _text_or_display(v: Value) -> str:
    """`v.text().unwrap_or_else(|| v.to_string())`."""
    t = v.text()
    return t if t is not None else str(v)


@dataclass(kw_only=True)
class RunResult:
    out: str
    err: str
    exit: int | None
    status: str
    # Stopped by the time or output limit; `status` says which.
    stopped: bool

    def to_value(self) -> Value:
        return ValueMap(
            [
                (ValueStr("out"), ValueStr(self.out)),
                (ValueStr("err"), ValueStr(self.err)),
                (ValueStr("exit"), NULL if self.exit is None else ValueInt(self.exit)),
                (ValueStr("status"), ValueStr(self.status)),
            ]
        )


class Stream(enum.Enum):
    OUT = "out"
    ERR = "err"


class Assert:
    """Base of the assertion variants (`AssertExit`, `AssertEq`, ...)."""


@dataclass(frozen=True)
class AssertExit(Assert):
    code: int


@dataclass(frozen=True)
class AssertEq(Assert):
    stream: Stream
    text: str


@dataclass(frozen=True)
class AssertNe(Assert):
    stream: Stream
    text: str


@dataclass(frozen=True)
class AssertRe(Assert):
    stream: Stream
    pattern: str


@dataclass(frozen=True)
class AssertNotRe(Assert):
    stream: Stream
    pattern: str


@dataclass(frozen=True)
class AssertEmpty(Assert):
    stream: Stream


@dataclass(kw_only=True)
class Case:
    args: list[str]
    stdin: str | None
    files: list[tuple[str, str]]
    asserts: list[Assert]


def find_arrow(line: str) -> int | None:
    quote = False
    i = 0
    while i + 1 < len(line):
        c = line[i]
        if c == "\\" and quote:
            i += 1
        elif c == '"':
            quote = not quote
        elif c == "=" and not quote and line[i + 1] == ">":
            return i
        i += 1
    return None


def words(s: str) -> list[str]:
    """Words of the left side; "quoted" words use the expression escapes."""
    cs = s
    out: list[str] = []
    i = 0
    while i < len(cs):
        if is_whitespace(cs[i]):
            i += 1
            continue
        w: list[str] = []
        while i < len(cs) and not is_whitespace(cs[i]):
            if cs[i] == '"':
                t, i = expr.read_string(cs, i)
                w.append(t)
            elif cs[i] == "'":
                i += 1
                while i < len(cs) and cs[i] != "'":
                    w.append(cs[i])
                    i += 1
                i += 1
            else:
                w.append(cs[i])
                i += 1
        out.append("".join(w))
    return out


def _take_while(cs: str, i: int, f: Callable[[str], bool]) -> str:
    """`cs[i..].iter().take_while(f).collect()`."""
    j = i
    while j < len(cs) and f(cs[j]):
        j += 1
    return cs[i:j]


def parse_case(line: str) -> Case:
    arrow = find_arrow(line)
    if arrow is None:
        raise Error("a program case needs `=>` followed by checks")
    left, right = line[:arrow], line[arrow + 2 :]
    c = Case(args=[], stdin=None, files=[], asserts=[])
    # `<<<` and `@name=` must be recognised before quote removal.
    cs = left
    i = 0
    rest: list[str] = []
    while i < len(cs):
        if cs.startswith("<<<", i):
            i += 3
            while i < len(cs) and is_whitespace(cs[i]):
                i += 1
            if cs[i : i + 1] != '"':
                raise Error('`<<<` must be followed by a "quoted string"')
            c.stdin, i = expr.read_string(cs, i)
        elif cs[i] == "@" and (i == 0 or is_whitespace(cs[i - 1])):
            start = i + 1
            while i < len(cs) and cs[i] != "=":
                i += 1
            name = cs[start:i]
            i += 1
            if cs[i : i + 1] != '"':
                raise Error(f'@{name}= must be followed by a "quoted string"')
            body, i = expr.read_string(cs, i)
            c.files.append((name, body))
        else:
            rest.append(cs[i])
            i += 1
    c.args = words("".join(rest))
    rc = right
    i = 0

    def skip_ws(i: int) -> int:
        while i < len(rc) and (is_whitespace(rc[i]) or rc[i] == ","):
            i += 1
        return i

    while True:
        i = skip_ws(i)
        if i >= len(rc):
            break
        word = _take_while(rc, i, str.isalpha)
        i += len(word)
        match word:
            case "exit":
                i = skip_ws(i)
                num = _take_while(rc, i, lambda ch: "0" <= ch <= "9" or ch == "-")
                i += len(num)
                code = parse_int(num, _I32_MIN, _I32_MAX)
                if code is None:
                    raise Error("exit needs a number")
                c.asserts.append(AssertExit(code))
                continue
            case "out" | "stdout":
                stream = Stream.OUT
            case "err" | "stderr":
                stream = Stream.ERR
            case other:
                raise Error(f"unknown check `{other}` (use out, err or exit)")
        i = skip_ws(i)
        op = _take_while(rc, i, lambda ch: ch in ("=", "!", "~"))
        i += len(op)
        i = skip_ws(i)
        if op == "":
            w = _take_while(rc, i, str.isalpha)
            if w == "empty":
                i += 5
                c.asserts.append(AssertEmpty(stream))
                continue
            raise Error("expected ==, !=, ~ or !~ after out/err")
        if rc[i : i + 1] != '"':
            raise Error(f'`{op}` must be followed by a "quoted string"')
        s, i = expr.read_string(rc, i)
        match op:
            case "==":
                c.asserts.append(AssertEq(stream, s))
            case "!=":
                c.asserts.append(AssertNe(stream, s))
            case "~":
                _check_regex(s)
                c.asserts.append(AssertRe(stream, s))
            case "!~":
                _check_regex(s)
                c.asserts.append(AssertNotRe(stream, s))
            case other:
                raise Error(f"unknown operator `{other}`")
    if not c.asserts:
        raise Error("a program case needs at least one check after `=>`")
    return c


def _check_regex(s: str) -> None:
    """`Regex::new(&s).map_err(|e| format!("bad regex: {e}"))?`."""
    try:
        regex(s)
    except re.error as e:
        raise Error(f"bad regex: {e}") from None


def _is_match(pattern: str, text: str) -> bool:
    """`Regex::new(re).is_ok_and(|x| x.is_match(text))`."""
    try:
        r = regex(pattern)
    except re.error:
        return False
    return r.search(text) is not None


def show(s: str) -> str:
    return ValueStr(s).short(100)


def judge(c: Case, r: RunResult) -> str | None:
    """`None` = pass, `Some(reason)` = fail."""
    if r.stopped:
        return r.status

    def text(s: Stream) -> str:
        return r.out if s == Stream.OUT else r.err

    def label(s: Stream) -> str:
        return "stdout" if s == Stream.OUT else "stderr"

    for a in c.asserts:
        fail: str | None = None
        match a:
            case AssertExit(code):
                if r.exit != code:
                    if r.exit is not None:
                        base = f"exit code: expected {code}, got {r.exit}"
                    else:
                        base = f"exit code: expected {code}, {r.status}"
                    # A wrapper (valgrind, sanitizers) explains itself on stderr.
                    last = next((t for t in map(trim, reversed(lines(r.err))) if t != ""), None)
                    if last is not None:
                        fail = f"{base}; stderr: {report.truncate(last, 120)}"
                    else:
                        fail = base
            case AssertEq(s, want):
                if text(s) != want:
                    fail = f"{label(s)}: expected {show(want)}, got {show(text(s))}"
            case AssertNe(s, bad):
                if text(s) == bad:
                    fail = f"{label(s)}: must not be {show(bad)}"
            case AssertRe(s, pattern):
                if not _is_match(pattern, text(s)):
                    fail = f"{label(s)}: nothing matches /{pattern}/ in {show(text(s))}"
            case AssertNotRe(s, pattern):
                if _is_match(pattern, text(s)):
                    fail = f"{label(s)}: /{pattern}/ must not match {show(text(s))}"
            case AssertEmpty(s):
                if text(s) != "":
                    fail = f"{label(s)}: expected nothing, got {show(text(s))}"
        if fail is not None:
            return fail
    return None
