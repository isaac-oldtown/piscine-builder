"""The environment the course will be followed in: detected on this machine
(coursecrafter assumes the student works where the course is built) and,
optionally, declared in the curriculum front matter when it differs.
Planning and generation only rely on tools listed here."""

from __future__ import annotations

import copy
import functools
import os
import platform
import shutil
import sys
import tempfile
import threading
from dataclasses import dataclass
from pathlib import Path

from proctor.command import Command
from proctor.compat import lines, regex, trim, trim_start_matches, write_text
from proctor.errors import Error

# (display name, program, version arguments)
TOOLS: list[tuple[str, str, list[str]]] = [
    ("gcc", "gcc", ["--version"]),
    ("clang", "clang", ["--version"]),
    ("make", "make", ["--version"]),
    ("cmake", "cmake", ["--version"]),
    ("gdb", "gdb", ["--version"]),
    ("lldb", "lldb", ["--version"]),
    ("valgrind", "valgrind", ["--version"]),
    ("cargo", "cargo", ["--version"]),
    ("rustfmt", "rustfmt", ["--version"]),
    ("clippy", "cargo", ["clippy", "--version"]),
    ("bash", "bash", ["--version"]),
    ("perl", "perl", ["-e", "print $^V"]),
    ("node", "node", ["--version"]),
    ("npm", "npm", ["--version"]),
    ("deno", "deno", ["--version"]),
    ("go", "go", ["version"]),
    ("java", "java", ["-version"]),
    ("javac", "javac", ["-version"]),
    ("ruby", "ruby", ["--version"]),
    ("php", "php", ["--version"]),
    ("lua", "lua", ["-v"]),
    ("git", "git", ["--version"]),
    ("docker", "docker", ["--version"]),
    ("sqlite3", "sqlite3", ["--version"]),
    ("shellcheck", "shellcheck", ["--version"]),
    ("ruff", "ruff", ["--version"]),
]


@dataclass(kw_only=True)
class Env:
    os: str
    arch: str
    found: list[tuple[str, str]]
    missing: list[str]
    facts: list[str]
    # Declared in the curriculum (`environment:`), if any.
    declared: str | None

    def has(self, tool: str) -> bool:
        return any(n == tool or n.startswith(f"{tool} ") for n, _ in self.found)

    def summary(self) -> str:
        """Compact text for prompts and for `coursecrafter env`."""
        s = ""
        if self.declared is not None:
            s += f"Declared by the course author (authoritative for what the student has):\n{trim(self.declared)}\n\nMachine where the course is being built and verified (detected):\n"
        s += f"OS: {self.os} ({self.arch})\n"
        found = [n if not v else f"{n} {v}" for n, v in self.found]
        s += f"Available: {', '.join(found)}\n"
        s += f"Not installed: {', '.join(self.missing)}\n"
        for f in self.facts:
            s += f"{f}\n"
        return s

    def prompt_block(self) -> str:
        """Text added to system prompts: the environment plus how to use it."""
        return (
            f"# Student environment\n{self.summary()}\nRely only on tools listed as available. Every test runs on the build machine above "
            "before it is kept, so a tool missing there makes the unit fail its quality gate.\n"
        )


def version_of(program: str, args: list[str]) -> str | None:
    from proctor import lang, run

    c = Command(lang.resolve(program))
    c.args(args)
    try:
        out = run.run(c, None, 8.0)
    except Error:
        # Windows ships some tools as .cmd shims (npm, yarn...).
        if sys.platform != "win32":
            return None
        c = Command(f"{program}.cmd")
        c.args(args)
        try:
            out = run.run(c, None, 8.0)
        except Error:
            return None
    if not out.success:
        return None
    text = out.stderr if not trim(out.stdout) else out.stdout
    line = next((trim(x) for x in lines(text) if trim(x)), None)
    if line is None:
        return None
    # Keep the version-looking part.
    dotted = regex(r"v?\d+(\.\d+)+")
    after_version = regex(r"(?i)version\D*(\d+)")
    m = dotted.search(line)
    if m is not None:
        return trim_start_matches(m[0], "v")
    c2 = after_version.search(line)
    if c2 is not None:
        return c2[1]
    c3 = regex(r"(\d+)\s*$").search(line)
    return c3[1] if c3 is not None else line


def asan_works(cc: str) -> bool:
    """AddressSanitizer is often installed with the compiler but unusable
    (MinGW, some macOS setups): try it for real."""
    from proctor import lang

    dir = Path(tempfile.gettempdir()) / f"piscinebuilder-asan-{os.getpid()}"
    try:
        dir.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass
    src = dir / "t.c"
    bin = dir / lang.exe("t")

    def succeeds(c: Command) -> bool:
        try:
            return c.output().status.success()
        except OSError:
            return False

    try:
        write_text(src, "int main(void) { return 0; }\n")
        written = True
    except OSError:
        written = False
    ok = (
        written
        and succeeds(Command(cc).args(["-fsanitize=address", str(src), "-o", str(bin)]))
        and succeeds(Command(bin))
    )
    shutil.rmtree(dir, ignore_errors=True)
    return ok


def detect(declared: str | None) -> Env:
    """Probes once per process (a few seconds), then reuses the result."""
    e = copy.deepcopy(_machine())
    e.declared = declared if declared is not None and trim(declared) else None
    return e


@functools.cache
def _machine() -> Env:
    return probe()


def probe() -> Env:
    from proctor import lang

    results: list[list[tuple[str, str | None]]] = []
    handles: list[threading.Thread] = []
    for name, prog, args in TOOLS:
        slot: list[tuple[str, str | None]] = []

        def go(
            name: str = name,
            prog: str = prog,
            args: list[str] = args,
            slot: list[tuple[str, str | None]] = slot,
        ) -> None:
            slot.append((name, version_of(prog, args)))

        t = threading.Thread(target=go, daemon=True)
        t.start()
        handles.append(t)
        results.append(slot)
    found: list[tuple[str, str]] = []
    missing: list[str] = []
    for h, slot in zip(handles, results, strict=True):
        h.join()
        # An empty slot is a thread that failed (Rust: `join()` is `Err`).
        for name, v in slot:
            if v is not None:
                found.append((name, v))
            else:
                missing.append(name)
    py = lang.python()
    if py is not None:
        v = version_of(py, ["--version"])
        found.insert(0, (f"python ({py})", v if v is not None else ""))
    else:
        missing.append("python 3")
    facts: list[str] = []
    cc = lang.cc()
    if cc is not None:
        facts.append(f"C compiler: `{cc}`.")
        facts.append(
            "AddressSanitizer works (-fsanitize=address): usable for leak and memory-error checks."
            if asan_works(cc)
            else "AddressSanitizer does NOT work with this compiler: do not use -fsanitize."
        )
    else:
        facts.append("No C compiler.")
    if not any(n == "valgrind" for n, _ in found):
        facts.append("valgrind is not installed: do not wrap runs with it.")
    if sys.platform == "win32":
        bash = lang.resolve("bash")
        facts.append(
            "Windows: commands run without a shell; bash comes from msys2 or Git Bash (POSIX tools like grep, sed, tr live there)."
            if bash.endswith("bash.exe") and "system32" not in bash.lower()
            else "Windows: commands run without a shell, and no POSIX bash was found."
        )
    found.sort()
    missing.sort()
    return Env(
        os=_os_name(),
        arch=_arch_name(),
        found=found,
        missing=missing,
        facts=facts,
        declared=None,
    )


def _os_name() -> str:
    """Rust's `std::env::consts::OS`."""
    if sys.platform == "win32":
        return "windows"
    if sys.platform == "darwin":
        return "macos"
    if sys.platform.startswith("linux"):
        return "linux"
    if sys.platform.startswith("freebsd"):
        return "freebsd"
    return sys.platform


def _arch_name() -> str:
    """Rust's `std::env::consts::ARCH`."""
    m = platform.machine().lower()
    return {
        "amd64": "x86_64",
        "x86_64": "x86_64",
        "x64": "x86_64",
        "arm64": "aarch64",
        "aarch64": "aarch64",
        "i386": "x86",
        "i686": "x86",
        "x86": "x86",
    }.get(m, m)


# Tools a unit's text mentions, to warn when they are missing.
MENTIONABLE: list[str] = [
    "make",
    "cmake",
    "gdb",
    "lldb",
    "valgrind",
    "node",
    "npm",
    "deno",
    "go",
    "java",
    "ruby",
    "php",
    "lua",
    "git",
    "docker",
    "sqlite3",
    "shellcheck",
    "perl",
    "clang",
]
