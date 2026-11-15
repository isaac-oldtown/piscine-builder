"""A long-lived harness process that loads the student's functions and
answers `call` requests over stdin/stdout (see `value.py` for the format).
A crash or a hang only costs the current call: the process is restarted
on the next one."""

from __future__ import annotations

import enum
import io
import queue
import subprocess
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import IO

from proctor import report, run, value
from proctor.command import Command, ExitStatus, Stdio
from proctor.compat import byte_len, from_utf8_lossy, io_error, lines, trim, trim_end
from proctor.errors import Error
from proctor.value import NULL, Value


class CallError(Error):
    """Base of `CallErrorRaised`, `CallErrorCrashed`, `CallErrorTimeout` and
    `CallErrorProtocol`."""

    def message(self) -> str:
        match self:
            case CallErrorRaised(msg=m):
                if m.startswith("panic: "):
                    return f"panicked: {m[len('panic: ') :]}"
                return f"raised {m}"
            case CallErrorCrashed(msg=m):
                return m
            case CallErrorTimeout(ms=ms):
                return f"timed out after {ms} ms (infinite loop?)"
            case CallErrorProtocol(msg=m):
                return f"harness error: {m}"
        raise AssertionError(type(self))

    def __str__(self) -> str:
        return self.message()


class CallErrorRaised(CallError):
    __match_args__ = ("msg",)


class CallErrorCrashed(CallError):
    __match_args__ = ("msg",)


class CallErrorTimeout(CallError):
    __match_args__ = ("ms",)

    def __init__(self, ms: int) -> None:
        self.ms = ms
        super().__init__(f"timed out after {ms} ms (infinite loop?)")


class CallErrorProtocol(CallError):
    __match_args__ = ("msg",)


def _as_millis(d: float) -> int:
    """`Duration::as_millis` of `d` seconds (`1.001` is 1001 ms, not 1000)."""
    return round(d * 1_000_000_000) // 1_000_000


class RecvTimeoutError(enum.Enum):
    TIMEOUT = "timeout"
    DISCONNECTED = "disconnected"


class SyncChannel:
    """`mpsc::sync_channel` of lines: bounded, the receiver sees the sender
    hang up, and the sender sees the receiver go away."""

    def __init__(self, bound: int) -> None:
        self.q: queue.Queue[str | None] = queue.Queue(maxsize=bound)
        self.receiver_gone = threading.Event()
        self.disconnected = False

    def send(self, line: str | None) -> bool:
        """Waits for room; False when the receiver is gone."""
        while not self.receiver_gone.is_set():
            try:
                self.q.put(line, timeout=0.05)
            except queue.Full:
                continue
            return True
        return False

    def hang_up(self) -> None:
        """The sender is dropped."""
        self.send(None)

    def recv_timeout(self, timeout: float) -> str | RecvTimeoutError:
        if self.disconnected:
            return RecvTimeoutError.DISCONNECTED
        try:
            line = self.q.get(timeout=max(0.0, timeout))
        except queue.Empty:
            return RecvTimeoutError.TIMEOUT
        if line is None:
            self.disconnected = True
            return RecvTimeoutError.DISCONNECTED
        return line

    def drop(self) -> None:
        """The receiver is dropped."""
        self.receiver_gone.set()


@dataclass(eq=False)
class SharedText:
    """`Arc<Mutex<String>>`."""

    text: str = ""
    lock: threading.Lock = field(default_factory=threading.Lock)


@dataclass(kw_only=True, eq=False)
class Proc:
    child: subprocess.Popen[bytes]
    stdin: IO[bytes]
    rx: SyncChannel
    stderr: SharedText

    def drop(self) -> None:
        """What dropping the Rust value does: the pipe to the process is
        closed and the stdout reader stops at its next line."""
        try:
            self.stdin.close()
        except (OSError, ValueError):
            pass
        self.rx.drop()


@dataclass(kw_only=True, eq=False)
class Harness:
    cmd: list[str]
    cwd: Path
    env: dict[str, str]
    timeout: float
    funcs: set[str]
    proc: Proc | None = None

    @staticmethod
    def new(
        cmd: list[str], cwd: Path, env: dict[str, str], timeout: float, funcs: set[str]
    ) -> Harness:
        return Harness(cmd=cmd, cwd=cwd, env=env, timeout=timeout, funcs=funcs, proc=None)

    def start(self) -> None:
        if not self.cmd:
            raise CallErrorProtocol("empty command")
        prog, args = self.cmd[0], self.cmd[1:]
        cmd = Command(prog)
        cmd.args(args).current_dir(self.cwd).envs(self.env).stdin(Stdio.piped()).stdout(
            Stdio.piped()
        ).stderr(Stdio.piped())
        run.configure(cmd)
        try:
            child = cmd.spawn()
        except OSError as e:
            raise CallErrorProtocol(f"cannot start harness `{prog}`: {io_error(e)}") from None
        stdin = child.stdin
        if stdin is None:
            raise CallErrorProtocol("no stdin")
        stdout = child.stdout
        if stdout is None:
            raise CallErrorProtocol("no stdout")
        stderr_pipe = child.stderr
        if stderr_pipe is None:
            raise CallErrorProtocol("no stderr")
        # Bounded: a student's print loop waits for the reader instead of
        # piling lines up in memory.
        tx = SyncChannel(16)

        def read_out() -> None:
            out = stdout
            try:
                while True:
                    try:
                        line = next_line(out, run.MAX_OUTPUT)
                    except (OSError, ValueError):
                        break
                    if line is None:
                        break
                    if not tx.send(line):
                        break
            finally:
                try:
                    out.close()
                except (OSError, ValueError):
                    pass
                tx.hang_up()

        threading.Thread(target=read_out, daemon=True).start()
        stderr = SharedText()
        sink = stderr

        def read_err() -> None:
            try:
                while True:
                    try:
                        buf = _read_some(stderr_pipe, 4096)
                    except (OSError, ValueError):
                        break
                    if not buf:
                        break
                    with sink.lock:
                        s = sink.text + from_utf8_lossy(buf)
                        length = byte_len(s)
                        if length > 8192:
                            b = s.encode("utf-8")
                            cut = length - 4096
                            while cut < len(b) and b[cut] & 0xC0 == 0x80:
                                cut += 1
                            s = b[cut:].decode("utf-8")
                        sink.text = s
            finally:
                try:
                    stderr_pipe.close()
                except (OSError, ValueError):
                    pass

        threading.Thread(target=read_err, daemon=True).start()
        self.proc = Proc(child=child, stdin=stdin, rx=tx, stderr=stderr)

    def stop(self) -> None:
        p, self.proc = self.proc, None
        if p is not None:
            run.kill_tree(p.child)
            try:
                p.child.wait()
            except OSError:
                pass
            p.drop()

    def died(self) -> CallError:
        p, self.proc = self.proc, None
        if p is None:
            return CallErrorCrashed("harness exited")
        try:
            status: ExitStatus | None = ExitStatus(p.child.wait())
        except OSError:
            status = None
        time.sleep(0.02)
        with p.stderr.lock:
            tail = last_meaningful_line(p.stderr.text)
        what = run.describe_status(status) if status is not None else "harness died"
        if what.startswith("exit "):
            what = f"exited the process ({what})"
        p.drop()
        return CallErrorCrashed(what if tail == "" else f"{what}: {tail}")

    def call(self, name: str, args: list[Value]) -> Value:
        if name not in self.funcs:
            raise CallErrorProtocol(f"`{name}` is not callable through the harness")
        if self.proc is None:
            self.start()
        line = f"call {name} {len(args)}"
        for a in args:
            line += " " + value.encode(a)
        line += "\n"
        p = self.proc
        wrote = False
        if p is not None:
            try:
                p.stdin.write(line.encode("utf-8"))
                p.stdin.flush()
            except (OSError, ValueError):
                wrote = False
            else:
                wrote = True
        if not wrote:
            raise self.died()
        # The time limit covers the whole call, stray output included.
        deadline = time.monotonic() + self.timeout
        while True:
            p = self.proc
            if p is None:
                raise CallErrorCrashed("harness exited")
            res = p.rx.recv_timeout(deadline - time.monotonic())
            match res:
                case str(resp):
                    # Harnesses that cannot silence student stdout mark their answers.
                    i = resp.find("\x1eSC ")
                    if i >= 0:
                        resp = resp[i + 4 :]
                    resp = trim_end(resp)
                    if resp.startswith("ok "):
                        try:
                            return value.decode_str(resp[3:])
                        except Error as e:
                            raise CallErrorProtocol(e.msg) from None
                    if resp == "ok":
                        return NULL
                    if resp.startswith("exc "):
                        it = resp[4:].split(" ", 1)
                        ty = value.unhex_text(it[0] if it else "")
                        msg = value.unhex_text(it[1] if len(it) > 1 else "")
                        raise CallErrorRaised(ty if msg == "" else f"{ty}: {msg}")
                    if resp.startswith("err "):
                        raise CallErrorProtocol(value.unhex_text(resp[4:]))
                    # Anything else is stray output; keep waiting for the answer.
                case RecvTimeoutError.TIMEOUT:
                    self.stop()
                    raise CallErrorTimeout(_as_millis(self.timeout))
                case RecvTimeoutError.DISCONNECTED:
                    raise self.died()

    def finish(self) -> str | None:
        """Ends the process politely (stdin closed) and reports a failing exit:
        leak checkers and sanitizers only speak when the program ends."""
        p, self.proc = self.proc, None
        if p is None:
            return None
        child, stderr = p.child, p.stderr
        p.drop()
        deadline = time.monotonic() + self.timeout
        while True:
            try:
                rc = child.poll()
            except OSError:
                rc = None
            else:
                if rc is not None:
                    status = ExitStatus(rc)
                    break
                if time.monotonic() < deadline:
                    time.sleep(0.01)
                    continue
            run.kill_tree(child)
            try:
                child.wait()
            except OSError:
                pass
            return None
        if status.success():
            return None
        time.sleep(0.05)
        with stderr.lock:
            text = stderr.text
        key = next(
            (
                report.truncate(t, 200)
                for t in map(trim, lines(text))
                if "ERROR" in t or "definitely lost" in t or "leak" in t
            ),
            None,
        )
        if key is None:
            key = last_meaningful_line(text)
        return f"the process ended with {run.describe_status(status)}: {key}"

    def close(self) -> None:
        """Rust's `Drop`: stops the process."""
        self.stop()

    def __del__(self) -> None:
        try:
            self.stop()
        except Exception:
            pass


def _read_some(r: IO[bytes], n: int) -> bytes:
    """`Read::read`: what is available, at most `n` bytes, waiting only for
    the first."""
    if isinstance(r, io.BufferedReader):
        return r.read1(n)
    return r.read(n)


def next_line(r: IO[bytes], max: int) -> str | None:
    """The next line of `r` without its line ending, cut to `max` bytes (the
    rest of a longer line is skipped: it is stray output, never an answer).
    `None` at the end of the stream."""
    line = r.readline(max)
    if not line:
        return None
    if line.endswith(b"\n"):
        line = line[:-1]
        if line.endswith(b"\r"):
            line = line[:-1]
    elif len(line) == max:
        skip_line(r)
    return from_utf8_lossy(line)


def skip_line(r: IO[bytes]) -> None:
    """Discards input up to and including the next newline."""
    while True:
        buf = r.readline(8192)
        if not buf or buf.endswith(b"\n"):
            return


def last_meaningful_line(s: str) -> str:
    for t in map(trim, reversed(lines(s))):
        if t:
            return report.truncate(t, 160)
    return ""
