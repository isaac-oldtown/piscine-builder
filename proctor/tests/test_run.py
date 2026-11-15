from __future__ import annotations

import io
import os
import sys
import threading
import time
from pathlib import Path

import pytest
from proctor.command import Command, ExitStatus
from proctor.errors import Error

from proctor import run


def test_words_split_with_quotes() -> None:
    assert run.split_words(r"""python -m "my mod" 'a b' x\y""") == [
        "python",
        "-m",
        "my mod",
        "a b",
        "x\\y",
    ]
    assert run.split_words('echo ""') == ["echo", ""]


def test_output_is_kept_up_to_the_limit() -> None:
    over = threading.Event()
    small = run.drain(io.BytesIO(b"hello"), over).join()
    assert small == b"hello"
    assert not over.is_set()
    flood = io.BytesIO(b"x" * (run.MAX_OUTPUT + 4096))
    kept = run.drain(flood, over).join()
    assert len(kept) == run.MAX_OUTPUT
    assert over.is_set()


# Real processes on this machine.


def python(code: str) -> Command:
    return Command(sys.executable).args(["-c", code])


def test_a_quick_command_is_captured() -> None:
    if sys.platform == "win32":
        cmd = Command("cmd").args(["/c", "echo hello"])
    else:
        cmd = Command("sh").args(["-c", "echo hello"])
    out = run.run(cmd, None, 10.0)
    assert out.success
    assert out.code == 0
    assert not out.stopped
    assert out.stdout.replace("\r\n", "\n") == "hello\n"
    assert out.stderr == ""
    assert out.status_text == "exit 0"
    assert cmd.env_add.get("CARGO_TERM_COLOR") == "never"


def test_exit_code_stdin_and_stderr() -> None:
    code = "import sys; d = sys.stdin.read(); sys.stdout.write(d.upper()); sys.stderr.write('e'); sys.exit(3)"
    cmd = python(code).env("PYTHONIOENCODING", "utf-8")
    out = run.run(cmd, "abc é\n".encode(), 10.0)
    assert not out.success
    assert out.code == 3
    assert not out.stopped
    assert out.stdout.replace("\r\n", "\n") == "ABC É\n"
    assert out.stderr == "e"
    assert out.status_text == "exit 3"


def test_no_stdin_reads_nothing() -> None:
    out = run.run(python("import sys; print(len(sys.stdin.read()))"), None, 10.0)
    assert out.stdout.strip() == "0"


def test_a_hanging_process_tree_is_killed_at_the_time_limit() -> None:
    if sys.platform == "win32":
        # ping inherits the pipes: the drain threads only end if the whole tree dies.
        cmd = Command("cmd").args(["/c", "ping -n 30 127.0.0.1"])
    else:
        cmd = Command("sh").args(["-c", "sleep 30; echo late"])
    start = time.monotonic()
    out = run.run(cmd, None, 0.3)
    assert time.monotonic() - start < 10
    assert out.stopped
    assert not out.success
    assert out.code is None
    assert out.status_text == "timed out after 300 ms"


def test_a_flood_of_output_is_stopped(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(run, "MAX_OUTPUT", 1 << 20)
    start = time.monotonic()
    out = run.run(python("while True: print('x' * 1000)"), None, 30.0)
    assert time.monotonic() - start < 20
    assert out.stopped
    assert out.code is None
    assert out.status_text == "stopped: more than 1 MB of output"
    assert len(out.stdout) == 1 << 20


def test_output_over_the_limit_fails_even_after_a_normal_exit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(run, "MAX_OUTPUT", 1 << 20)
    out = run.run(python("import sys; sys.stderr.write('y' * (3 << 20))"), None, 30.0)
    assert out.stopped
    assert not out.success
    assert out.status_text == "stopped: more than 1 MB of output"
    assert len(out.stderr) == 1 << 20


def test_a_missing_program_is_an_error() -> None:
    with pytest.raises(Error) as e:
        run.run(Command("proctor-no-such-program"), None, 5.0)
    assert e.value.msg.startswith("cannot run `proctor-no-such-program`: ")
    if sys.platform == "win32":
        assert e.value.msg == "cannot run `proctor-no-such-program`: program not found"


def test_exists() -> None:
    assert run.exists(sys.executable)
    assert not run.exists("proctor-no-such-program")


def test_describe_status() -> None:
    assert run.describe_status(ExitStatus(0)) == "exit 0"
    assert run.describe_status(ExitStatus(2)) == "exit 2"
    if sys.platform == "win32":
        assert (
            run.describe_status(ExitStatus(0xC0000005))
            == "crashed: segmentation fault (access violation)"
        )
        assert run.describe_status(ExitStatus(0xC00000FD)) == "crashed: stack overflow"
        assert run.describe_status(ExitStatus(0xFFFFFFFF)) == "exit -1"
    else:
        assert run.describe_status(ExitStatus(-11)) == "crashed: segmentation fault"
        assert run.describe_status(ExitStatus(-9)) == "killed by signal 9"


def test_split_words_edge_cases() -> None:
    assert run.split_words("  ") == []
    assert run.split_words('a "b\\"c" \'d\\e\'') == ["a", 'b"c', "d\\e"]
    assert run.split_words('a"b c"d') == ["ab cd"]
    assert run.split_words("x\u3000y") == ["x", "y"]
    assert run.split_words('"unterminated') == ["unterminated"]


def test_copy_tree_skips_build_output(tmp_path: Path) -> None:
    src = tmp_path / "src"
    for rel in ["a.c", "sub/b.h", ".git/HEAD", "target/x", "sub/__pycache__/m.pyc", ".proctor/k"]:
        p = src / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(rel.encode())
    dst = tmp_path / "dst"
    run.copy_tree(src, dst)
    got = sorted(
        os.path.relpath(os.path.join(d, f), dst).replace("\\", "/")
        for d, _, fs in os.walk(dst)
        for f in fs
    )
    assert got == ["a.c", "sub/b.h"]
    assert (dst / "sub" / "b.h").read_bytes() == b"sub/b.h"
    with pytest.raises(Error) as e:
        run.copy_tree(tmp_path / "missing", tmp_path / "out")
    assert e.value.msg.startswith(f"{tmp_path / 'missing'}: ")
