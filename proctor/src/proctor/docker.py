"""Units marked `platform: linux` need Linux APIs (fork, signals, /proc).
Elsewhere, proctor reruns the same command in an Ubuntu container: it
builds the image itself, from its own package, the first time it is needed
and again whenever proctor changes. The course folder is mounted, so the
student keeps working in their own editor and terminal."""

from __future__ import annotations

import enum
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path
from typing import Any

import proctor
import proctor.selftest
from proctor import compat, course, report
from proctor.command import Command, Stdio
from proctor.compat import U64_MASK, from_utf8_lossy, io_error, lines, trim
from proctor.errors import Error
from proctor.unit import Unit

# Set inside the container, so a command there never delegates again.
INSIDE = "PROCTOR_IN_CONTAINER"

# proctor's own package, which the image is built from: the folder it runs
# from, whether a checkout or an installed copy.
SOURCE = Path(proctor.__file__).parent

# The image still has Rust, gcc and python3 for the students' units; proctor
# itself is this Python package, run by a `proctor` launcher.
DOCKERFILE = r"""FROM ubuntu:24.04
RUN apt-get update \
 && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
    build-essential ca-certificates curl procps python3 python3-yaml \
 && rm -rf /var/lib/apt/lists/*
ENV RUSTUP_HOME=/usr/local/rustup CARGO_HOME=/usr/local/cargo PATH=/usr/local/cargo/bin:$PATH
RUN curl -sSf https://sh.rustup.rs | sh -s -- -y --profile minimal \
    --default-toolchain stable --component clippy,rustfmt
COPY proctor /opt/proctor/proctor
ENV PYTHONPATH=/opt/proctor
RUN printf '#!/bin/sh\nexec python3 -m proctor "$@"\n' > /usr/local/bin/proctor \
 && chmod +x /usr/local/bin/proctor
"""


def needed(u: Unit) -> bool:
    """The container for this unit: on another OS than Linux, unless already inside."""
    return (
        u.platform == "linux"
        and not sys.platform.startswith("linux")
        and os.environ.get(INSIDE) is None
    )


def fnv(h: int, data: bytes) -> int:
    for b in data:
        h ^= b
        h = (h * 0x100000001B3) & U64_MASK
    return h


def context() -> list[tuple[str, bytes]]:
    """Files of the image's build context (relative path, content)."""
    package = SOURCE
    out: list[tuple[str, bytes]] = [("Dockerfile", DOCKERFILE.encode())]
    stack = [package]
    sources: list[tuple[str, bytes]] = []
    while stack:
        d = stack.pop()
        try:
            entries = list(d.iterdir())
        except OSError as e:
            raise Error(f"{d}: {io_error(e)}") from None
        for p in entries:
            if p.is_dir():
                if p.name != "__pycache__":
                    stack.append(p)
            elif p.suffix != ".pyc":
                rel = p.relative_to(package).as_posix()
                try:
                    body = p.read_bytes()
                except OSError as e:
                    raise Error(io_error(e)) from None
                sources.append((f"proctor/{rel}", body))
    sources.sort()
    out.extend(sources)
    return out


def quiet(cmd: Command) -> bool:
    try:
        return cmd.stdout(Stdio.null()).stderr(Stdio.null()).status().success()
    except OSError:
        return False


class Status(enum.Enum):
    # No `docker` command.
    MISSING = "missing"
    # Installed, but the engine does not answer.
    STOPPED = "stopped"
    # Docker Desktop is set to Windows containers.
    WINDOWS_CONTAINERS = "windows_containers"
    READY = "ready"


def status() -> Status:
    try:
        o = Command("docker").args(["info", "--format", "{{.OSType}}"]).stdin(Stdio.null()).output()
    except OSError:
        return Status.MISSING
    if not o.status.success():
        return Status.STOPPED
    if trim(from_utf8_lossy(o.stdout)) != "linux":
        return Status.WINDOWS_CONTAINERS
    return Status.READY


def check_docker() -> None:
    match status():
        case Status.READY:
            return
        case Status.MISSING:
            raise Error(
                "this unit runs on Linux, in a container, and `docker` was not found: install Docker Desktop (`coursecrafter setup` can do it), start it, then run the command again"
            )
        case Status.STOPPED:
            raise Error(
                "this unit runs on Linux, in a container, and Docker is not running: start Docker Desktop, wait until it is ready, then run the command again"
            )
        case Status.WINDOWS_CONTAINERS:
            raise Error("Docker runs Windows containers: switch Docker Desktop to Linux containers")


def image() -> str:
    """The image tag, building the image first if this proctor has none yet."""
    check_docker()
    files = context()
    h = 0xCBF29CE484222325
    for p, body in files:
        h = fnv(h, p.encode())
        h = fnv(h, body)
    tag = f"piscinebuilder-proctor:{h:016x}"
    if quiet(Command("docker").args(["image", "inspect", tag])):
        return tag
    print(
        "Building the Linux image for Linux units (first time, or after a proctor update; a few minutes)...",
        file=sys.stderr,
    )
    dir = Path(tempfile.gettempdir()) / f"piscinebuilder-image-{h:016x}"
    shutil.rmtree(dir, ignore_errors=True)
    for p, body in files:
        path = dir / p
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            raise Error(io_error(e)) from None
        try:
            path.write_bytes(body)
        except OSError as e:
            raise Error(f"{path}: {io_error(e)}") from None
    try:
        ok = (
            Command("docker")
            .args(["build", "-t", tag])
            .arg(dir)
            .stdout(Stdio.STDERR)
            .status()
            .success()
        )
    except OSError:
        ok = False
    shutil.rmtree(dir, ignore_errors=True)
    if not ok:
        raise Error(
            "building the Linux image failed (see the output above); check your internet connection and run the command again"
        )
    return tag


def run_command(c: course.CourseDir, args: list[str], tty: bool, port: int | None) -> Command:
    """`docker run` in the course folder, at the same place as the current directory."""
    tag = image()
    try:
        root = course._canonicalize(c.root)
    except OSError:
        root = c.root
    root = course.strip_verbatim(root)
    try:
        cwd = Path.cwd()
    except OSError as e:
        raise Error(io_error(e)) from None
    try:
        cwd = course._canonicalize(cwd)
    except OSError:
        pass
    cwd = course.strip_verbatim(cwd)
    try:
        # Joined by hand: an empty relative path prints as "" in Rust, "." here.
        rel = "/".join(cwd.relative_to(root).parts)
    except ValueError:
        rel = ""
    h = fnv(0xCBF29CE484222325, str(root).encode())
    cmd = Command("docker")
    cmd.args(["run", "--rm", "--init", "-i"])
    if tty:
        cmd.arg("-t")
    if port is not None:
        cmd.arg("-p").arg(f"127.0.0.1:{port}:{port}")
    cmd.arg("-v").arg(f"{root}:/course")
    # Builds stay in fast Linux storage, apart from the Windows ones.
    cmd.args(["-v", "piscinebuilder-cargo-registry:/usr/local/cargo/registry"])
    cmd.arg("-v").arg(f"piscinebuilder-cache-{h:016x}:/course/.proctor/cache")
    rel = rel.replace("\\", "/")
    cmd.arg("-w").arg(f"/course/{rel}".rstrip("/"))
    cmd.args(["-e", f"{INSIDE}=1"])
    for k in ["NO_COLOR", "PROCTOR_ASCII"]:
        if os.environ.get(k) is not None:
            cmd.args(["-e", k])
    cmd.arg(tag).args(args)
    return cmd


def interactive() -> bool:
    return compat.is_terminal(sys.stdin) and compat.is_terminal(sys.stdout)


def delegate(c: course.CourseDir, args: list[str]) -> int:
    """Runs `args` in the container with the terminal attached; returns the exit code."""
    full = ["proctor", *args]
    cmd = run_command(c, full, interactive(), None)
    try:
        status = cmd.status()
    except OSError as e:
        raise Error(f"docker: {io_error(e)}") from None
    code = status.code()
    return 2 if code is None else max(0, min(code, 255))


def shell(c: course.CourseDir, dir: Path, port: int | None) -> int:
    """An interactive shell in the container, in `dir` (relative to the course)."""
    try:
        rel = "/".join(dir.relative_to(c.root).parts)
    except ValueError:
        rel = str(dir)
    rel = rel.replace("\\", "/")
    script = f"cd \"/course/{rel}\" 2>/dev/null; echo 'Linux container: your course folder is /course (exit to leave)'; exec bash"
    args = ["bash", "-c", script]
    cmd = run_command(c, args, interactive(), port)
    try:
        status = cmd.status()
    except OSError as e:
        raise Error(f"docker: {io_error(e)}") from None
    code = status.code()
    return 2 if code is None else max(0, min(code, 255))


def _reject_constant(name: str) -> Any:
    # serde_json does not read `NaN` or `Infinity`.
    raise ValueError(name)


def selftest(
    c: course.CourseDir, units: list[Unit], seed: int
) -> list[proctor.selftest.UnitReport]:
    """Self-tests `units` in the container with `seed`."""
    args = ["proctor", "selftest", "--json", "--no-validate", "--seed"]
    args.append(f"{seed:016x}")
    args.extend(u.id for u in units)
    cmd = run_command(c, args, False, None).stdin(Stdio.null()).stderr(Stdio.piped())
    try:
        o = cmd.output()
    except OSError as e:
        raise Error(f"docker: {io_error(e)}") from None
    stdout = from_utf8_lossy(o.stdout)
    reports: list[proctor.selftest.UnitReport] = []
    for line in lines(stdout):
        try:
            v = json.loads(line, parse_constant=_reject_constant)
        except (ValueError, RecursionError):
            continue
        r = proctor.selftest.UnitReport.from_json(v)
        if r is not None:
            reports.append(r)
    if len(reports) != len(units):
        err = from_utf8_lossy(o.stderr)
        raise Error(
            f"the self-test in the Linux container did not report: {report.truncate(trim(err), 300)}"
        )
    return reports
