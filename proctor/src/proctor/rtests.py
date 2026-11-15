"""Tests written in Rust (`rust_tests:` in a unit spec): top-level `#[test]`
functions that proctor compiles against the student's library crate, which
they reach as `student`. They grade what cases cannot call: types, methods,
traits. Each test runs in its own process with the unit's time limit, so a
crash or a hang fails that test only."""

from __future__ import annotations

import json
import re
import shutil
from dataclasses import dataclass
from pathlib import Path

from proctor import lang, run, scan, sig
from proctor.command import Command
from proctor.compat import io_error, lines, read_text, trim, write_text
from proctor.errors import Error

# Put before every suite, so that the tests name the student's items directly.
PRELUDE = "#[allow(unused_imports)]\nuse student::*;\n\n"

_TEST_ATTR = re.compile(r"#\s*\[\s*test\s*\]")
_ATTR = re.compile(r"#\s*\[")
_FN = re.compile(r"fn\s+([A-Za-z_][A-Za-z0-9_]*)")
_PANIC = re.compile(r"^thread '.*?'(?: \(\d+\))? panicked at (.+:\d+:\d+):$")
_HIDDEN_AT = re.compile(r"\s+at\s+tests[\\/]t_\S+$")


@dataclass(kw_only=True)
class RustTest:
    name: str
    # The function as written, from its first attribute to its closing brace.
    text: str
    # Its body, comments and literals blanked.
    code: str
    # Preceded by a `// example` line: shown in the instructions.
    example: bool


def parse(source: str) -> list[RustTest]:
    """The top-level `#[test]` functions of `source`, in order."""
    code = scan.strip("rust", source)
    out: list[RustTest] = []
    depth = 0
    # Where the current top-level item starts (its attributes and comments).
    item = 0
    i = 0
    while i < len(code):
        c = code[i]
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                item = i + 1
        elif c == ";" and depth == 0:
            item = i + 1
        elif depth == 0 and c == "f" and (i == 0 or not scan.is_ident_byte(code[i - 1])):
            m = _FN.match(code, i)
            if m is not None and _TEST_ATTR.search(code, item, i) is not None:
                open_ = scan.body_open(code, m.end())
                close = scan.matching(code, open_) if open_ is not None else None
                if open_ is not None and close is not None:
                    a = _ATTR.search(code, item, i)
                    start = a.start() if a is not None else i
                    out.append(
                        RustTest(
                            name=m.group(1),
                            text=source[start : close + 1],
                            code=code[open_ : close + 1],
                            example=_marked(source[item:start]),
                        )
                    )
                    i = close + 1
                    item = i
                    continue
        i += 1
    return out


def _marked(before: str) -> bool:
    for ln in lines(before):
        t = trim(ln)
        if t.startswith("//") and trim(t.lstrip("/")).lower() == "example":
            return True
    return False


def problem(source: str, tests: list[RustTest]) -> str | None:
    """What makes `source` unusable as a suite, if anything."""
    if not tests:
        return "no top-level #[test] function"
    if len(_TEST_ATTR.findall(scan.strip("rust", source))) > len(tests):
        return "every #[test] function must be at the top level, not inside a `mod`"
    seen: set[str] = set()
    for t in tests:
        if t.name in seen:
            return f"two tests are named `{t.name}`"
        seen.add(t.name)
    return None


def calls(code: str, names: list[str]) -> list[str]:
    """Which of the expected functions `names` a test body calls."""
    args = r"(?:\s*::\s*<[^>]*>)?\s*\("
    out: list[str] = []
    for n in names:
        owner = sig.owner_of(n)
        f = re.escape(n.rsplit("::", 1)[-1])
        if owner is not None:
            pat = rf"\b{re.escape(owner)}\s*::\s*{f}\b|\.\s*{f}{args}"
        else:
            pat = rf"(?<![\w.]){f}{args}"
        if re.search(pat, code):
            out.append(n)
    return out


def target_name(unit: str) -> str:
    return "t_" + re.sub(r"[^A-Za-z0-9_]", "_", unit)


@dataclass(kw_only=True)
class Suites:
    """The compiled tests: one test program per unit."""

    programs: dict[str, list[str]]
    cwd: Path
    env: dict[str, str]
    timeout: float

    def run(self, unit: str, name: str) -> str | None:
        """None when test `name` of `unit` passes, else why it failed. Raises
        `Error` when the course is at fault."""
        cmd = self.programs.get(unit)
        if cmd is None:
            raise Error(f"no test program for {unit}")
        c = Command(cmd[0])
        c.args([*cmd[1:], name, "--exact", "--test-threads=1"]).current_dir(self.cwd).envs(self.env)
        out = run.run(c, None, self.timeout)
        if out.stopped:
            return out.status_text
        if out.success:
            if "running 0 tests" in out.stdout:
                raise Error(f"no test named `{name}`")
            if " ... ignored" in out.stdout:
                raise Error(f"`{name}` is marked #[ignore]")
            return None
        why = failure(out.stdout, name, self.cwd)
        if why is not None:
            return why
        if "has overflowed its stack" in out.stderr:
            return "crashed: stack overflow"
        return out.status_text


def failure(stdout: str, name: str, ws: Path) -> str | None:
    """libtest's reason for a failed test, on one line, without locations in
    the hidden tests."""
    ls = lines(stdout)
    head = f"---- {name} stdout ----"
    if head not in ls:
        return None
    said: list[str] = []
    at = ""
    for ln in ls[ls.index(head) + 1 :]:
        t = trim(ln)
        if t == "failures:" or t.startswith("---- "):
            break
        if not t or t.startswith("note: run with `RUST_BACKTRACE"):
            continue
        m = _PANIC.match(t)
        if m is not None:
            loc = _relative(m.group(1), ws)
            if not loc.startswith("tests/t_"):
                at = f" (at {loc})"
            continue
        said.append(_HIDDEN_AT.sub("", t.removeprefix("note: ")))
    return "; ".join(said) + at if said else f"failed{at}"


def _relative(path: str, ws: Path) -> str:
    b = str(ws)
    out = path.replace(f"{b}\\", "").replace(f"{b}/", "").replace("\\\\?\\", "")
    return out.replace("\\", "/")


def _write_if_changed(path: Path, body: str) -> None:
    """Unchanged files keep their time, so cargo does not rebuild them."""
    try:
        if read_text(path) == body:
            return
    except OSError:
        pass
    path.parent.mkdir(parents=True, exist_ok=True)
    write_text(path, body)


def build(ctx: lang.Ctx, suites: list[tuple[str, str]], timeout: float) -> Suites:
    """Compiles each unit's tests against the crate in `ctx.ws`. Raises
    `lang.BuildError`."""
    pkg = lang.cargo_package(ctx.ws)
    d = ctx.cache / "rust-tests"
    ws = str(ctx.ws).replace("\\", "/")
    toml = f'[package]\nname = "proctor_tests"\nversion = "0.0.0"\nedition = "2024"\npublish = false\nautotests = false\n\n[dependencies]\nstudent = {{ package = "{pkg}", path = "{ws}" }}\n'
    files = {"src/lib.rs": ""}
    targets: dict[str, str] = {}
    for unit, source in suites:
        t = target_name(unit)
        targets[t] = unit
        toml += f'\n[[test]]\nname = "{t}"\npath = "tests/{t}.rs"\n'
        files[f"tests/{t}.rs"] = PRELUDE + source
    files["Cargo.toml"] = toml + "\n[workspace]\n"
    try:
        for rel, body in files.items():
            _write_if_changed(d / rel, body)
        for old in (d / "tests").iterdir():
            if f"tests/{old.name}" not in files:
                old.unlink()
    except OSError as e:
        raise lang.BuildError.new(io_error(e)) from None
    c = Command("cargo")
    c.args(["test", "--no-run", "--quiet", "--message-format=json-render-diagnostics"])
    c.current_dir(d).envs(ctx.env).env("CARGO_TARGET_DIR", ctx.cache / "target")
    try:
        out = run.run(c, None, lang.BUILD_TIMEOUT)
    except Error as e:
        raise lang.BuildError.new(e.msg) from None
    if not out.success:
        rel = ctx.rel(out.stderr)
        if "no library targets found" in rel or "lib target" in rel:
            msg = "the crate has no library target (src/lib.rs)"
        else:
            first = lang.first_error(rel)
            hidden = re.search(r" \(tests/t_[^)]*\)$", first) if first is not None else None
            if first is None:
                msg = "proctor's tests do not build"
            elif hidden is not None:
                msg = (
                    f"proctor's tests do not compile against your crate: {first[: hidden.start()]}"
                )
            else:
                msg = first
        raise lang.BuildError(msg=msg, log=out.stderr)
    programs: dict[str, list[str]] = {}
    for ln in lines(out.stdout):
        try:
            v = json.loads(ln)
        except ValueError:
            continue
        if not isinstance(v, dict) or v.get("reason") != "compiler-artifact":
            continue
        target, exe = v.get("target"), v.get("executable")
        name = target.get("name") if isinstance(target, dict) else None
        if isinstance(name, str) and name in targets and isinstance(exe, str):
            programs[targets[name]] = ctx.wrapped([exe])
    for unit, _ in suites:
        if unit not in programs:
            raise lang.BuildError.new(f"cargo built no test program for {unit}")
    return Suites(
        programs=programs, cwd=ctx.ws, env={"RUST_BACKTRACE": "0", **ctx.env}, timeout=timeout
    )


def stub_reference(ref: Path, dst: Path, protos: list[sig.Proto]) -> list[str]:
    """Copies the reference crate to `dst` with the bodies of `protos` replaced
    by `todo!()`, so that its types stay for the tests to compile against.
    Returns the functions it did not find."""
    shutil.rmtree(dst, ignore_errors=True)
    run.copy_tree(ref, dst)
    found: set[str] = set()
    for rel in scan.walk(dst):
        if not rel.endswith(".rs"):
            continue
        path = dst / rel
        try:
            raw = read_text(path)
        except OSError as e:
            raise Error(f"{path}: {io_error(e)}") from None
        code = scan.strip("rust", raw)
        every_impl = scan.impl_blocks(code, None)
        spans: list[tuple[int, int]] = []
        for p in protos:
            owner = p.owner()
            blocks = every_impl if owner is None else scan.impl_blocks(code, owner)
            for m in re.finditer(rf"\bfn\s+{re.escape(p.short_name())}\b", code):
                in_block = any(a < m.start() < b for a, b in blocks)
                # A free function outside every impl, a method inside its type's.
                if in_block != (owner is not None):
                    continue
                open_ = scan.body_open(code, m.end())
                close = scan.matching(code, open_) if open_ is not None else None
                if open_ is not None and close is not None:
                    spans.append((open_, close))
                    found.add(p.name)
        kept: list[tuple[int, int]] = []
        for a, b in sorted(set(spans)):
            if not kept or a > kept[-1][1]:
                kept.append((a, b))
        for a, b in reversed(kept):
            raw = raw[:a] + "{\n    todo!()\n}" + raw[b + 1 :]
        if kept:
            try:
                write_text(path, raw)
            except OSError as e:
                raise Error(f"{path}: {io_error(e)}") from None
    return [p.name for p in protos if p.name not in found]
