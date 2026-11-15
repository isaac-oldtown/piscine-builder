"""Subprocess helper: captured output, optional stdin, hard timeout, output
limit, process-tree kill."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import IO

from proctor.command import Command, ExitStatus, Stdio
from proctor.compat import from_utf8_lossy, io_error, is_whitespace
from proctor.errors import Err, Error, Ok, Result

# Most bytes kept of one output stream; a process that writes more is
# stopped, so a print loop cannot fill the memory before its time limit.
MAX_OUTPUT = 16 << 20


@dataclass(kw_only=True)
class Out:
    success: bool
    code: int | None
    # Killed for its time limit or for writing more than `MAX_OUTPUT`;
    # `status_text` says which.
    stopped: bool
    stdout: str
    stderr: str
    status_text: str


class JoinHandle:
    """A reader thread and the bytes it collected (`thread::JoinHandle<Vec<u8>>`)."""

    def __init__(self, target: threading.Thread, result: list[bytes]) -> None:
        self.thread = target
        self.result = result

    def join(self) -> bytes:
        self.thread.join()
        return self.result[0] if self.result else b""


def _as_millis(d: float) -> int:
    """`Duration::as_millis` of `d` seconds (`1.001` is 1001 ms, not 1000)."""
    return round(d * 1_000_000_000) // 1_000_000


def drain(r: IO[bytes], over: threading.Event) -> JoinHandle:
    """Reads `r` to the end, keeping its first `MAX_OUTPUT` bytes; `over` is set
    when there were more."""
    result: list[bytes] = []

    def go() -> None:
        limit = MAX_OUTPUT
        buf = bytearray()
        try:
            while len(buf) <= limit:
                chunk = r.read(min(65536, limit + 1 - len(buf)))
                if not chunk:
                    break
                buf += chunk
        except (OSError, ValueError):
            pass
        if len(buf) > limit:
            del buf[limit:]
            over.set()
            # Keep the pipe moving until the process is killed.
            try:
                while r.read(65536):
                    pass
            except (OSError, ValueError):
                pass
        result.append(bytes(buf))
        try:
            r.close()
        except (OSError, ValueError):
            pass

    t = threading.Thread(target=go, daemon=True)
    t.start()
    return JoinHandle(t, result)


def output_limit() -> str:
    return f"stopped: more than {MAX_OUTPUT >> 20} MB of output"


def kill_tree(child: subprocess.Popen[bytes]) -> None:
    pid = str(child.pid)
    if sys.platform == "win32":
        try:
            Command("taskkill").args(["/PID", pid, "/T", "/F"]).stdout(Stdio.null()).stderr(
                Stdio.null()
            ).status()
        except OSError:
            pass
    if os.name == "posix":
        try:
            Command("kill").args(["-KILL", f"-{pid}"]).stderr(Stdio.null()).status()
        except OSError:
            pass
    try:
        child.kill()
    except OSError:
        pass


def describe_status(status: ExitStatus) -> str:
    """Human description of how a process ended ("exit 1", "segmentation fault")."""
    if os.name == "posix":
        sig = status.signal()
        if sig is not None:
            match sig:
                case 11:
                    return "crashed: segmentation fault"
                case 6:
                    return "crashed: abort"
                case 8:
                    return "crashed: floating point exception"
                case 7:
                    return "crashed: bus error"
                case n:
                    return f"killed by signal {n}"
    c = status.code()
    if c is None:
        return "terminated"
    match c & 0xFFFFFFFF:
        case 0xC0000005:
            return "crashed: segmentation fault (access violation)"
        case 0xC00000FD:
            return "crashed: stack overflow"
        case 0xC0000094:
            return "crashed: integer division by zero"
        case 0xC0000409:
            return "crashed: abort (stack buffer overrun or fail-fast)"
        case 0xC000001D:
            return "crashed: illegal instruction"
        case _:
            return f"exit {c}"


def configure(cmd: Command) -> None:
    if os.name == "posix":
        cmd.process_group(0)
    cmd.env("CARGO_TERM_COLOR", "never")


def run(cmd: Command, stdin: bytes | None, timeout: float) -> Out:
    cmd.stdin(Stdio.piped() if stdin is not None else Stdio.null()).stdout(Stdio.piped()).stderr(
        Stdio.piped()
    )
    configure(cmd)
    program = cmd.get_program()
    try:
        child = cmd.spawn()
    except OSError as e:
        raise Error(f"cannot run `{program}`: {io_error(e)}") from None
    sin = child.stdin
    if stdin is not None and sin is not None:
        data = bytes(stdin)

        def feed() -> None:
            try:
                sin.write(data)
            except (OSError, ValueError):
                pass
            try:
                sin.close()
            except (OSError, ValueError):
                pass

        threading.Thread(target=feed, daemon=True).start()
    over = threading.Event()
    out = drain(child.stdout, over) if child.stdout is not None else None
    err = drain(child.stderr, over) if child.stderr is not None else None
    deadline = time.monotonic() + timeout
    # The exit status, or why the process was stopped.
    ended: Result[ExitStatus]
    while True:
        try:
            rc = child.poll()
        except OSError as e:
            raise Error(f"waiting for `{program}`: {io_error(e)}") from None
        if rc is not None:
            ended = Ok(ExitStatus(rc))
            break
        stop: str | None
        if over.is_set():
            stop = output_limit()
        elif time.monotonic() >= deadline:
            stop = f"timed out after {_as_millis(timeout)} ms"
        else:
            stop = None
        if stop is not None:
            kill_tree(child)
            try:
                child.wait()
            except OSError:
                pass
            ended = Err(stop)
            break
        time.sleep(0.005)

    def join(h: JoinHandle | None) -> str:
        data = h.join() if h is not None else b""
        return from_utf8_lossy(data)

    stdout, stderr = join(out), join(err)
    # Output cut at the limit is not the program's output, even if it exited by itself.
    if isinstance(ended, Ok) and over.is_set():
        ended = Err(output_limit())
    match ended:
        case Ok(s):
            return Out(
                success=s.success(),
                code=s.code(),
                stopped=False,
                stdout=stdout,
                stderr=stderr,
                status_text=describe_status(s),
            )
        case Err(why):
            return Out(
                success=False,
                code=None,
                stopped=True,
                stdout=stdout,
                stderr=stderr,
                status_text=why,
            )
    raise AssertionError(ended)


def exists(program: str) -> bool:
    """True if `program --version` (or similar) can be spawned at all."""
    cmd = Command(program)
    cmd.arg("--version").stdin(Stdio.null()).stdout(Stdio.null()).stderr(Stdio.null())
    try:
        cmd.status()
    except OSError:
        return False
    return True


def split_words(s: str) -> list[str]:
    """Splits a command line into words: whitespace-separated, with "double" and
    'single' quotes grouping words. No shell is involved."""
    words: list[str] = []
    cur: list[str] = []
    has = False
    quote: str | None = None
    chars = iter(s)
    for c in chars:
        if quote is not None and c == quote:
            quote = None
        elif quote == '"' and c == "\\":
            n = next(chars, None)
            if n is not None:
                cur.append(n)
        elif quote is not None:
            cur.append(c)
        elif c in ('"', "'"):
            quote = c
            has = True
        elif is_whitespace(c):
            if has:
                words.append("".join(cur))
                cur = []
                has = False
        else:
            cur.append(c)
            has = True
    if has:
        words.append("".join(cur))
    return words


def _copy_file(src: Path, dst: Path) -> None:
    """`fs::copy`: contents and permissions; on Windows `CopyFileExW` also
    keeps the modification time."""
    if sys.platform == "win32":
        shutil.copy2(src, dst)
    else:
        shutil.copy(src, dst)


def copy_tree(from_: Path, to: Path) -> None:
    """Copies `from_` into `to`, skipping version-control and build output dirs."""
    try:
        to.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        raise Error(f"{to}: {io_error(e)}") from None
    try:
        rd = os.scandir(from_)
    except OSError as e:
        raise Error(f"{from_}: {io_error(e)}") from None
    with rd:
        entries = list(rd)
    for entry in entries:
        name = entry.name
        if name in (".git", "target", "node_modules", "__pycache__", ".proctor"):
            continue
        src = Path(entry.path)
        dst = to / name
        if os.path.isdir(src):
            copy_tree(src, dst)
        else:
            try:
                _copy_file(src, dst)
            except OSError as e:
                raise Error(f"{src}: {io_error(e)}") from None
