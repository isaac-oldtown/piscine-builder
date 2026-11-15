"""End to end on the demo course: its specs validate, every unit passes the
check coursecrafter's build gate runs (the reference passes its own tests,
and the function tests fail on empty stubs), and runaway student work is
stopped. Units whose toolchain is not installed are skipped."""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
from collections.abc import Iterator
from pathlib import Path

import pytest
from proctor.compat import read_text, write_text
from proctor.unit import Unit

from proctor import course, docker, engine, lang, run, selftest

DEMO = Path(__file__).resolve().parents[2] / "examples" / "demo-course"


def copy_course(src: Path, dst: Path) -> None:
    """Copies the course, without a cache left by running it in place."""
    dst.mkdir(parents=True, exist_ok=True)
    for e in src.iterdir():
        if e.is_dir():
            if e.name != "cache":
                copy_course(e, dst / e.name)
        else:
            shutil.copy(e, dst / e.name)


def scratch(name: str) -> Iterator[Path]:
    """A fresh copy of the demo course in the temp folder, removed afterwards."""
    d = Path(tempfile.gettempdir()) / f"proctor-{name}-{os.getpid()}"
    shutil.rmtree(d, ignore_errors=True)
    copy_course(DEMO, d)
    try:
        yield d
    finally:
        shutil.rmtree(d, ignore_errors=True)


def missing_tool(c: course.CourseDir, u: Unit) -> str | None:
    """Why `u` cannot be checked on this machine, if it cannot."""
    language = c.language(u)
    if language == "c":
        return "no C compiler" if lang.cc() is None else None
    if language == "python":
        return "no Python 3" if lang.python() is None else None
    if language == "rust":
        program = "cargo"
    elif language in ("bash", "sh", "shell"):
        program = "bash"
    else:
        cmd = c.lang_config(u).run
        words = run.split_words(cmd) if cmd is not None else []
        program = words[0] if words else language
    return None if run.exists(lang.resolve(program)) else f"no `{program}`"


def fixed_cases(id: str, limit_ms: int, files: list[tuple[str, str]]) -> list[engine.Item] | None:
    """The tests phase of unit `id` checked against `files`, on its fixed cases
    only, with time limit `limit_ms`; `None` when its toolchain is missing."""
    for root in scratch(id):
        spec = root / ".proctor" / "units" / f"{id}.yaml"
        fixed = read_text(spec).split("\nrandom:")[0]
        write_text(spec, f"{fixed}\ntimeout_ms: {limit_ms}\n")
        c = course.CourseDir.load(root)
        u = c.unit(id)
        assert u is not None
        why = missing_tool(c, u)
        if why is not None:
            print(f"skipped {id}: {why}", file=sys.stderr)
            return None
        work = root / "work"
        for path, body in files:
            p = work / path
            p.parent.mkdir(parents=True, exist_ok=True)
            write_text(p, body)
        opts = engine.Options(seed=1, against=engine.AgainstDir(work), tests_only=True)
        v = engine.Engine.new(c, u, opts).check(selftest.Silent(log=""))
        tests = next((p for p in v.phases if p.name == "tests"), None)
        items = tests.items if tests is not None else []
        assert items, f"{id}: no tests ran: {v.first_fail}"
        return items
    raise AssertionError("unreachable")


def test_demo_course_passes_the_build_gate() -> None:
    for root in scratch("demo"):
        c = course.CourseDir.load(root)
        assert engine.validate(c) == []
        failures = []
        checked = 0
        for u in c.units:
            why = "needs the Linux container" if docker.needed(u) else missing_tool(c, u)
            if why is not None:
                print(f"skipped {u.id}: {why}", file=sys.stderr)
                continue
            checked += 1
            r = selftest.unit(c, u, 1)
            if not r.ok() or any(n.startswith("stubs") for n in r.notes):
                failures.append(
                    f"{u.id}: reference {r.reference}, weak {r.vacuous}, notes {r.notes}"
                )
        assert not failures, "\n".join(failures)
        assert checked > 0, "no unit could be checked on this machine"


def test_a_program_flooding_its_output_is_stopped() -> None:
    # Builtins only: a 640 KB line, printed forever.
    flood = "s=xxxxxxxxxx\nfor i in 1 2 3 4 5 6 7 8 9 10 11 12 13 14 15 16; do s=$s$s; done\nwhile :; do printf '%s\\n' \"$s\"; done\n"
    items = fixed_cases("d02-word-count", 30_000, [("wc.sh", flood)])
    if items is None:
        pytest.skip("no bash")
    for i in items:
        assert not i.ok and "MB of output" in i.detail, i.detail


def test_a_function_printing_forever_times_out() -> None:
    cargo = read_text(DEMO / ".proctor" / "refs" / "d03-longest-word" / "Cargo.toml")
    lib = 'pub fn longest_word(text: &str) -> Option<&str> {\n    loop {\n        println!("{text}");\n    }\n}\n\npub fn word_count(text: &str) -> usize {\n    loop {\n        println!("{text}");\n    }\n}\n'
    items = fixed_cases("d03-longest-word", 500, [("Cargo.toml", cargo), ("src/lib.rs", lib)])
    if items is None:
        pytest.skip("no cargo")
    for i in items:
        assert not i.ok and "timed out" in i.detail, i.detail
