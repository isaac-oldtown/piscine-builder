"""`std::process::Command` with the same builder API, on top of `subprocess`,
so code that builds a command in one module and runs it in another keeps
its shape: `Command("cargo").args(["build"]).current_dir(ws).env("K", "v")`.

Spawning a program that does not exist raises `ProgramNotFound` (an
`OSError`), displayed by `compat.io_error` as Rust displays it."""

from __future__ import annotations

import enum
import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import IO, Any


class Stdio(enum.Enum):
    INHERIT = "inherit"
    NULL = "null"
    PIPED = "piped"
    # `Stdio::from(std::io::stderr())`: the child's stream goes to our stderr.
    STDERR = "stderr"

    @staticmethod
    def null() -> Stdio:
        return Stdio.NULL

    @staticmethod
    def piped() -> Stdio:
        return Stdio.PIPED


class ProgramNotFound(FileNotFoundError):
    """The program to run does not exist (Rust: `program not found` on Windows)."""


def _popen_stdio(s: Stdio) -> int | IO[Any] | None:
    match s:
        case Stdio.INHERIT:
            return None
        case Stdio.NULL:
            return subprocess.DEVNULL
        case Stdio.PIPED:
            return subprocess.PIPE
        case Stdio.STDERR:
            return sys.stderr
    raise AssertionError(s)


@dataclass(frozen=True)
class ExitStatus:
    """How a process ended. `returncode` is Popen's (negative: killed by
    that signal on Unix)."""

    returncode: int

    def success(self) -> bool:
        return self.returncode == 0

    def code(self) -> int | None:
        """The exit code, as an i32 (Windows codes such as 0xC0000005 are
        negative, as Rust reports them); `None` when killed by a signal."""
        rc = self.returncode
        if os.name == "posix":
            return None if rc < 0 else rc
        rc &= 0xFFFFFFFF
        return rc - (1 << 32) if rc >= 1 << 31 else rc

    def signal(self) -> int | None:
        """The signal that killed the process (Unix only)."""
        if os.name == "posix" and self.returncode < 0:
            return -self.returncode
        return None


@dataclass(frozen=True)
class Output:
    status: ExitStatus
    stdout: bytes
    stderr: bytes


class Command:
    def __init__(self, program: str | os.PathLike[str]) -> None:
        self.program = os.fspath(program)
        self.argv: list[str] = []
        self.cwd: Path | None = None
        self.env_add: dict[str, str] = {}
        self.stdin_io: Stdio | None = None
        self.stdout_io: Stdio | None = None
        self.stderr_io: Stdio | None = None
        self.pgid: int | None = None

    @staticmethod
    def new(program: str | os.PathLike[str]) -> Command:
        return Command(program)

    def arg(self, a: str | os.PathLike[str]) -> Command:
        self.argv.append(os.fspath(a))
        return self

    def args(self, a: list[str] | tuple[str, ...] | list[Any]) -> Command:
        self.argv.extend(os.fspath(x) for x in a)
        return self

    def current_dir(self, d: str | os.PathLike[str]) -> Command:
        self.cwd = Path(d)
        return self

    def env(self, k: str, v: str | os.PathLike[str]) -> Command:
        self.env_add[k] = os.fspath(v)
        return self

    def envs(self, m: dict[str, str]) -> Command:
        for k, v in m.items():
            self.env(k, v)
        return self

    def stdin(self, s: Stdio) -> Command:
        self.stdin_io = s
        return self

    def stdout(self, s: Stdio) -> Command:
        self.stdout_io = s
        return self

    def stderr(self, s: Stdio) -> Command:
        self.stderr_io = s
        return self

    def process_group(self, pgid: int) -> Command:
        """Unix: run the child in process group `pgid` (0: a new group)."""
        self.pgid = pgid
        return self

    def get_program(self) -> str:
        return self.program

    def get_args(self) -> list[str]:
        return list(self.argv)

    def environment(self) -> dict[str, str] | None:
        """The child's environment, or None to inherit ours unchanged."""
        if not self.env_add:
            return None
        env = dict(os.environ)
        if sys.platform == "win32":
            # Windows variable names are case-insensitive: replace, not duplicate.
            upper = {k.upper(): k for k in env}
            for k, v in self.env_add.items():
                env.pop(upper.get(k.upper(), k), None)
                env[k] = v
        else:
            env.update(self.env_add)
        return env

    def popen(self, stdin: Stdio, stdout: Stdio, stderr: Stdio) -> subprocess.Popen[bytes]:
        """Starts the process; `stdin`/`stdout`/`stderr` apply where the
        command did not set its own."""
        kwargs: dict[str, Any] = {}
        if self.pgid is not None and os.name == "posix":
            kwargs["process_group"] = self.pgid
        try:
            return subprocess.Popen(
                [self.program, *self.argv],
                cwd=self.cwd,
                env=self.environment(),
                stdin=_popen_stdio(self.stdin_io or stdin),
                stdout=_popen_stdio(self.stdout_io or stdout),
                stderr=_popen_stdio(self.stderr_io or stderr),
                **kwargs,
            )
        except FileNotFoundError as e:
            if self.cwd is not None and not self.cwd.is_dir():
                raise
            raise ProgramNotFound(e.errno, e.strerror, e.filename) from None

    def spawn(self) -> subprocess.Popen[bytes]:
        return self.popen(Stdio.INHERIT, Stdio.INHERIT, Stdio.INHERIT)

    def status(self) -> ExitStatus:
        p = self.popen(Stdio.INHERIT, Stdio.INHERIT, Stdio.INHERIT)
        return ExitStatus(p.wait())

    def output(self) -> Output:
        p = self.popen(Stdio.NULL, Stdio.PIPED, Stdio.PIPED)
        out, err = p.communicate()
        return Output(ExitStatus(p.returncode), out or b"", err or b"")
