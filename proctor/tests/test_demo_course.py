"""End to end on the demo course: its specs validate, every unit passes the
check coursecrafter's build gate runs (the tests run on empty stubs and the
function tests fail there), its tests and random laws accept the known-good
solutions in `demo_solutions/` under several seeds, and runaway student work
is stopped. Units whose toolchain is not installed are skipped."""

from __future__ import annotations

import shutil
import sys
from pathlib import Path

import pytest
from common import DEMO, demo_copy, missing_tool
from proctor.compat import read_text, write_text

from proctor import course, docker, engine, selftest

SOLUTIONS = Path(__file__).parent / "demo_solutions"


def fixed_cases(id: str, limit_ms: int, files: list[tuple[str, str]]) -> list[engine.Item] | None:
    """The tests phase of unit `id` checked against `files`, on its fixed cases
    only, with time limit `limit_ms`; `None` when its toolchain is missing."""
    with demo_copy(id) as s:
        root = s.path
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


def test_demo_course_passes_the_build_gate() -> None:
    with demo_copy("demo") as s:
        c = course.CourseDir.load(s.path)
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
            if not r.ok() or r.notes:
                failures.append(f"{u.id}: error {r.error}, weak {r.vacuous}, notes {r.notes}")
        assert not failures, "\n".join(failures)
        assert checked > 0, "no unit could be checked on this machine"


def test_correct_solutions_pass_whatever_the_seed() -> None:
    with demo_copy("demo") as s:
        c = course.CourseDir.load(s.path)
        failures = []
        checked = 0
        for u in c.units:
            why = "needs the Linux container" if docker.needed(u) else missing_tool(c, u)
            if why is not None or not (SOLUTIONS / u.id).is_dir():
                continue
            # Built in the scratch copy, never in the repo.
            solution = s.path / "solutions" / u.id
            shutil.copytree(SOLUTIONS / u.id, solution)
            checked += 1
            for seed in (1, 0xDEADBEEF, 0x0123456789ABCDEF):
                opts = engine.Options(
                    seed=seed, against=engine.AgainstDir(solution), tests_only=False
                )
                v = engine.Engine.new(c, u, opts).check(selftest.Silent(log=""))
                if not v.ok:
                    failures.append(f"{u.id} (seed {seed:016x}): {v.first_fail}")
                    break
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
    cargo = read_text(DEMO / ".proctor" / "scaffold" / "d03-longest-word" / "Cargo.toml")
    lib = 'pub fn longest_word(text: &str) -> Option<&str> {\n    loop {\n        println!("{text}");\n    }\n}\n\npub fn word_count(text: &str) -> usize {\n    loop {\n        println!("{text}");\n    }\n}\n'
    items = fixed_cases("d03-longest-word", 500, [("Cargo.toml", cargo), ("src/lib.rs", lib)])
    if items is None:
        pytest.skip("no cargo")
    for i in items:
        assert not i.ok and "timed out" in i.detail, i.detail
