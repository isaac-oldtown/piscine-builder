from __future__ import annotations

import io
import sys
from pathlib import Path

import pytest
from proctor.compat import write_text
from proctor.harness import (
    CallErrorCrashed,
    CallErrorProtocol,
    CallErrorRaised,
    CallErrorTimeout,
    Harness,
)
from proctor.value import NULL, ValueInt, ValueList, ValueStr

from proctor import harness, lang, run


def test_long_lines_are_cut_and_the_next_answer_still_arrives() -> None:
    r = io.BufferedReader(io.BytesIO(b"ok 1\r\nxxxxxxxxxxxx\nok 2\npartial"), buffer_size=4)
    lines = []
    while (line := harness.next_line(r, 6)) is not None:
        lines.append(line)
    assert lines == ["ok 1", "xxxxxx", "ok 2", "partia"]


def test_call_error_messages() -> None:
    assert CallErrorRaised("panic: boom").message() == "panicked: boom"
    assert CallErrorRaised("ValueError: x").message() == "raised ValueError: x"
    assert CallErrorCrashed("crashed: stack overflow").message() == "crashed: stack overflow"
    assert CallErrorTimeout(500).message() == "timed out after 500 ms (infinite loop?)"
    assert CallErrorProtocol("bad").message() == "harness error: bad"
    match CallErrorTimeout(7):
        case CallErrorTimeout(ms):
            assert ms == 7


def test_last_meaningful_line() -> None:
    assert harness.last_meaningful_line("a\n  b  \n \n\n") == "b"
    assert harness.last_meaningful_line("  \n") == ""
    assert harness.last_meaningful_line("x" * 200) == "x" * 160 + "..."


STUDENT = """\
import atexit, os, sys

def add(a, b):
    return a + b

def boom():
    raise ValueError("bad")

def spin():
    while True:
        pass

def die():
    sys.stderr.write("dying now\\n")
    sys.stderr.flush()
    os._exit(3)

def chatty(x):
    print("noise")
    return [x, "é"]

def arm():
    def end():
        sys.stderr.write("==1== definitely lost: 10 bytes\\n")
        sys.stderr.flush()
        os._exit(5)
    atexit.register(end)
"""

FUNCS = {"m.add", "add", "boom", "spin", "die", "chatty", "arm"}


def py_harness(tmp_path: Path, timeout: float) -> Harness:
    ws = tmp_path / "ws"
    ws.mkdir()
    write_text(ws / "m.py", STUDENT)
    script = tmp_path / "proctor_harness.py"
    write_text(script, lang.PY_HARNESS)
    env = {"PYTHONIOENCODING": "utf-8", "PYTHONDONTWRITEBYTECODE": "1"}
    cmd = [sys.executable, "-u", str(script), str(ws), "m"]
    return Harness.new(cmd, ws, env, timeout, set(FUNCS))


def test_calls_through_the_python_harness(tmp_path: Path) -> None:
    h = py_harness(tmp_path, 5.0)
    try:
        assert h.call("add", [ValueInt(2), ValueInt(3)]) == ValueInt(5)
        assert h.call("m.add", [ValueStr("a"), ValueStr("b")]) == ValueStr("ab")
        assert h.call("chatty", [ValueInt(1)]) == ValueList([ValueInt(1), ValueStr("é")])
        with pytest.raises(CallErrorProtocol) as p:
            h.call("nope", [])
        assert p.value.message() == "harness error: `nope` is not callable through the harness"
        with pytest.raises(CallErrorRaised) as r:
            h.call("boom", [])
        assert r.value.msg == "ValueError: bad"
        with pytest.raises(CallErrorCrashed) as c:
            h.call("die", [])
        assert c.value.msg == "exited the process (exit 3): dying now"
        assert h.proc is None
        # Restarted on the next call.
        assert h.call("add", [ValueInt(1), ValueInt(1)]) == ValueInt(2)
        assert h.finish() is None
        assert h.proc is None
    finally:
        h.close()


def test_a_hang_costs_only_the_current_call(tmp_path: Path) -> None:
    h = py_harness(tmp_path, 0.5)
    try:
        with pytest.raises(CallErrorTimeout) as t:
            h.call("spin", [])
        assert t.value.ms == 500
        assert t.value.message() == "timed out after 500 ms (infinite loop?)"
        assert h.proc is None
        assert h.call("add", [ValueInt(20), ValueInt(22)]) == ValueInt(42)
    finally:
        h.close()


def test_finish_reports_a_failing_exit(tmp_path: Path) -> None:
    h = py_harness(tmp_path, 5.0)
    try:
        assert h.call("arm", []) == NULL
        assert h.finish() == "the process ended with exit 5: ==1== definitely lost: 10 bytes"
        assert h.finish() is None
    finally:
        h.close()


FAKE = """\
import sys
out = sys.stdout
for line in sys.stdin:
    parts = line.split()
    name = parts[1]
    if name == "marked":
        out.write("stray text\\n" + "x" * 100 + "\\n\\x1eSC ok i7  \\r\\n")
    elif name == "err":
        out.write("err " + "broken".encode().hex() + "\\n")
    elif name == "exc":
        out.write("exc " + "Err".encode().hex() + "\\n")
    elif name == "garbled":
        out.write("ok szz\\n")
    elif name == "quiet":
        out.write("ok\\n")
    elif name == "hang":
        out.write("partial answer without newline")
    out.flush()
"""


def fake_harness(tmp_path: Path, timeout: float) -> Harness:
    script = tmp_path / "fake.py"
    write_text(script, FAKE)
    funcs = {"marked", "err", "exc", "garbled", "quiet", "hang"}
    return Harness.new([sys.executable, str(script)], tmp_path, {}, timeout, funcs)


def test_marked_answers_stray_output_and_long_lines(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(run, "MAX_OUTPUT", 64)
    h = fake_harness(tmp_path, 1.0)
    try:
        assert h.call("marked", []) == ValueInt(7)
        with pytest.raises(CallErrorProtocol) as e:
            h.call("err", [])
        assert e.value.msg == "broken"
        with pytest.raises(CallErrorRaised) as r:
            h.call("exc", [])
        assert r.value.msg == "Err"
        with pytest.raises(CallErrorProtocol) as g:
            h.call("garbled", [])
        assert g.value.msg == "bad hex: invalid digit found in string"
        assert h.call("quiet", []) == NULL
        with pytest.raises(CallErrorTimeout):
            h.call("hang", [])
    finally:
        h.close()
    assert h.proc is None


def test_a_missing_harness_program(tmp_path: Path) -> None:
    h = Harness.new(["proctor-no-such-harness"], tmp_path, {}, 1.0, {"f"})
    with pytest.raises(CallErrorProtocol) as e:
        h.call("f", [])
    assert e.value.msg.startswith("cannot start harness `proctor-no-such-harness`: ")
    h2 = Harness.new([], tmp_path, {}, 1.0, {"f"})
    with pytest.raises(CallErrorProtocol) as e2:
        h2.call("f", [])
    assert e2.value.msg == "empty command"
