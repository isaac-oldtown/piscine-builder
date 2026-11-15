from __future__ import annotations

import sys
from pathlib import Path

import pytest
from proctor.errors import Error
from proctor.program import (
    AssertEmpty,
    AssertEq,
    AssertExit,
    AssertRe,
    Program,
    RunResult,
    Stream,
)
from proctor.value import NULL, ValueInt, ValueList, ValueMap, ValueStr

from proctor import expr, program


def test_case_grammar() -> None:
    c = program.parse_case(
        r'-l "two words" @in.txt="a\nb\n" <<< "x\n" => out == "2 in.txt\n" err ~ "warn" exit 0'
    )
    assert c.args == ["-l", "two words"]
    assert c.stdin == "x\n"
    assert c.files == [("in.txt", "a\nb\n")]
    assert len(c.asserts) == 3
    c = program.parse_case(r'nope.log => err~"(?i)no such" exit 2')
    assert isinstance(c.asserts[0], AssertRe) and c.asserts[0].stream == Stream.ERR
    assert c.asserts[1] == AssertExit(2)
    with pytest.raises(Error):
        program.parse_case("a b")
    with pytest.raises(Error):
        program.parse_case(r'=> out == "x" bogus')
    c = program.parse_case(r"=> out empty, exit 1")
    assert c.asserts[0] == AssertEmpty(Stream.OUT)


def test_case_errors() -> None:
    def err(line: str) -> str:
        with pytest.raises(Error) as e:
            program.parse_case(line)
        return e.value.msg

    assert err("=> exit") == "exit needs a number"
    assert err("=> exit 99999999999") == "exit needs a number"
    assert err("=> out ==x") == '`==` must be followed by a "quoted string"'
    assert err('=> out === "x"') == "unknown operator `===`"
    assert err("<<< abc => exit 0") == '`<<<` must be followed by a "quoted string"'
    assert err('x @f= "y" => exit 0') == '@f= must be followed by a "quoted string"'
    assert err("=>") == "a program case needs at least one check after `=>`"
    assert err('=> out ~ "("').startswith("bad regex: ")
    c = program.parse_case('"a=>b" @x="1" a@b => out == "c"')
    assert c.args == ["a=>b", "a@b"]
    assert c.files == [("x", "1")]
    assert c.asserts == [AssertEq(Stream.OUT, "c")]


def result(out: str = "", err: str = "", exit: int | None = 0, status: str = "exit 0") -> RunResult:
    return RunResult(out=out, err=err, exit=exit, status=status, stopped=False)


def test_judge() -> None:
    def judge(line: str, r: RunResult) -> str | None:
        return program.judge(program.parse_case(line), r)

    assert judge('=> out == "a\\n" exit 0', result(out="a\n")) is None
    assert (
        judge("=> exit 0", result(err="x\n  \n", exit=1, status="exit 1"))
        == "exit code: expected 0, got 1; stderr: x"
    )
    assert (
        judge("=> exit 0", result(exit=None, status="crashed: segmentation fault"))
        == "exit code: expected 0, crashed: segmentation fault"
    )
    stopped = RunResult(out="", err="", exit=None, status="timed out after 100 ms", stopped=True)
    assert judge("=> exit 0", stopped) == "timed out after 100 ms"
    assert judge('=> out == "a"', result(out="b")) == 'stdout: expected "a", got "b"'
    assert judge('=> out != "b"', result(out="b")) == 'stdout: must not be "b"'
    # `$` is the end of the text, as in Rust's regex crate.
    assert (
        judge('=> err ~ "^ok$"', result(err="ok\n")) == 'stderr: nothing matches /^ok$/ in "ok\\n"'
    )
    assert judge('=> err ~ "^ok$"', result(err="ok")) is None
    assert judge('=> err !~ "ok"', result(err="ok")) == 'stderr: /ok/ must not match "ok"'
    assert judge("=> err empty", result(err="x")) == 'stderr: expected nothing, got "x"'


def test_run_result_value() -> None:
    r = RunResult(out="o", err="e", exit=None, status="terminated", stopped=False)
    assert r.to_value() == ValueMap(
        [
            (ValueStr("out"), ValueStr("o")),
            (ValueStr("err"), ValueStr("e")),
            (ValueStr("exit"), NULL),
            (ValueStr("status"), ValueStr("terminated")),
        ]
    )


ECHO = """\
import os, sys
data = sys.stdin.read()
print(repr(sys.argv[1:]))
print(repr(data))
if os.path.exists("sub/in.txt"):
    print(open("sub/in.txt").read())
sys.stderr.write("done\\n")
sys.exit(int(os.environ.get("CODE", "0")))
"""


def echo_program(tmp_path: Path, timeout: float = 10.0) -> Program:
    script = tmp_path / "echo.py"
    script.write_text(ECHO)
    return Program(
        cmd=[sys.executable, str(script), "base"],
        cwd=tmp_path,
        env={"CODE": "4"},
        timeout=timeout,
    )


def test_program_runs_with_args_stdin_and_files(tmp_path: Path) -> None:
    p = echo_program(tmp_path)
    r = p.run(["a b"], "in\n", [("sub/in.txt", "file body")])
    assert r.out == "['base', 'a b']\n'in\\n'\nfile body\n"
    assert r.err == "done\n"
    assert r.exit == 4
    assert r.status == "exit 4"
    assert not r.stopped
    assert not (tmp_path / "sub" / "in.txt").exists()
    with pytest.raises(Error) as e:
        p.run([], None, [("echo.py", "x")])
    assert e.value.msg == "input file `echo.py` would overwrite a file of the workspace"


def test_run_from_the_expression_language(tmp_path: Path) -> None:
    p = echo_program(tmp_path)
    v = p.call([(None, ValueList([ValueStr("x"), ValueInt(3)])), (None, ValueStr("s"))])
    assert isinstance(v, ValueMap)
    assert v.pairs[0] == (ValueStr("out"), ValueStr("['base', 'x', '3']\n's'\n"))
    assert v.pairs[2] == (ValueStr("exit"), ValueInt(4))
    v = p.call([("stdin", ValueStr("t")), ("args", ValueStr("'p q' r"))])
    assert isinstance(v, ValueMap)
    assert v.pairs[0] == (ValueStr("out"), ValueStr("['base', 'p q', 'r']\n't'\n"))
    with pytest.raises(expr.EvalErrorSpec) as e:
        p.call([("bogus", ValueInt(1))])
    assert e.value.msg == "run: unknown argument `bogus`"
    with pytest.raises(expr.EvalErrorSpec) as e:
        p.call([(None, ValueInt(1))])
    assert e.value.msg == "run: args must be a list, got int"
    with pytest.raises(expr.EvalErrorSpec) as e:
        p.call([(None, ValueList([])), (None, ValueStr("")), (None, ValueStr(""))])
    assert e.value.msg == "run(args, stdin) takes at most 2 arguments"
    missing = Program(cmd=["proctor-no-such-program"], cwd=tmp_path, env={}, timeout=5.0)
    with pytest.raises(expr.EvalErrorSpec) as e:
        missing.call([])
    assert e.value.msg.startswith("cannot run `proctor-no-such-program`: ")


def test_a_stopped_program_is_the_student_s_fault(tmp_path: Path) -> None:
    script = tmp_path / "spin.py"
    script.write_text("while True:\n    pass\n")
    p = Program(cmd=[sys.executable, str(script)], cwd=tmp_path, env={}, timeout=0.3)
    with pytest.raises(expr.EvalErrorStudent) as e:
        p.call([])
    assert e.value.msg == "program timed out after 300 ms"
