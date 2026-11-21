from __future__ import annotations

import shutil
from pathlib import Path

import pytest
from proctor.compat import read_text
from proctor.program import parse_case

from coursecrafter import build, config, crosscheck, curriculum, llm, prompts
from proctor import yamlio

PLAN = "---\ntitle: T\nlanguage: python\n---\n\n## d01-twice: Twice\ngoal: Double a number.\n"
CONTRACT = """target: function
goal: Double a number.
module: m
stubs: m.py
functions:
  - { name: double, sig: "def double(n: int) -> int", returns: "twice n" }
"""
TESTS = """<tests>
cases: |
  # example
  double(2) == 4
  double(3) == 7
random:
  laws:
    - { name: twice, gen: { n: int 0..10 }, check: ["double(n) == n + n"] }
think_about:
  - negative numbers
  - zero
</tests>"""


def builder(tmp_path: Path, retries: int) -> build.Builder:
    dead = llm.Llm.resolve(
        llm.Choice(provider="custom", base_url="http://127.0.0.1:9/v1", model="m"),
        config.Config(),
    )
    opts = build.Options(
        retries=retries, tests=True, only=[], yes=True, text_only=False, debug=False
    )
    return build.Builder.new(tmp_path, curriculum.parse(PLAN), dead, opts, "ENV")


def test_a_disputed_case_goes_back_once_then_is_dropped(tmp_path: Path) -> None:
    if shutil.which("python") is None:
        pytest.skip("no python")
    b = builder(tmp_path, retries=2)
    u = b.plan.units()[0]
    m = b.merge_contract(u, yamlio.load(CONTRACT))
    assert b.check_contract(u, m, {}) == []
    build.write(b.unit_path(u.id), yamlio.dump(m))
    asked: list[tuple[str, str]] = []
    # The writer keeps its wrong case; the reading finds 6, and the law agrees.
    replies = {"tests": [TESTS, TESTS], "verify": ["<answers>\n1: 4\n2: 6\n3: true\n</answers>"]}

    def chat(label: str, messages: list[llm.Msg]) -> str:
        kind = label.split("-")[0]
        asked.append((kind, messages[-1].content))
        return replies[kind].pop(0)

    b.chat = chat  # type: ignore[method-assign]
    b.tests(u, b.plan.units(), {})
    assert [k for k, _ in asked] == ["tests", "verify", "tests"]
    # The independent reading never sees the tests or their results.
    verify = asked[1][1]
    assert "double(3) == 7" not in verify and "== 4" not in verify
    assert "the value of `double(3)`" in verify
    # The writer hears both objections: the reading's and the law's.
    retry = asked[2][1]
    assert "finds `6` where the case expects `7`" in retry
    assert "`law twice` fails its check" in retry
    spec = yamlio.load(read_text(b.unit_path(u.id)))
    assert spec["cases"] == "# example\ndouble(2) == 4\n"
    assert len(spec["random"]["laws"]) == 1
    assert b.verified(u.id)
    # A suite changed by hand is no longer the one that was checked.
    spec["cases"] += "double(5) == 10\n"
    build.write(b.unit_path(u.id), yamlio.dump(spec))
    assert not b.verified(u.id)


def test_answers_are_read_line_by_line() -> None:
    got = crosscheck.answers("notes\n<answers>\n1: 4\n2) false, n = 3\n3. ?\n</answers>")
    assert got == {1: "4", 2: "false, n = 3", 3: "?"}
    check = crosscheck._run_check(parse_case('<<< "b a\\n" => out == "1 a\\n1 b\\n" exit 0'))
    assert check('out "1 a\\n1 b\\n" err "" exit 0') is None
    assert check('out "1 b\\n1 a\\n" err "" exit 0') is not None
    assert "leave its exit code open" in (check('out "1 a\\n1 b\\n" err "" exit ?') or "")
    assert crosscheck._truth("? depends on the order") is not None
    assert crosscheck._value_check("[1, 2]")("[1,2]") is None


def test_run_command_is_fixed_by_the_first_stage(tmp_path: Path) -> None:
    # C and Rust run what they build: the run command matters elsewhere.
    plan = curriculum.parse(prompts.PLAN_EXAMPLE.replace("language: c\n", "language: python\n", 1))
    b = build.Builder.new(
        tmp_path,
        plan,
        llm.Llm.resolve(
            llm.Choice(provider="custom", base_url="http://127.0.0.1:9/v1", model="m"),
            config.Config(),
        ),
        build.Options(retries=0, tests=False, only=[], yes=True, text_only=False, debug=False),
        "ENV",
    )
    first, later = plan.entry("p01.2-build"), plan.entry("p01.3-finale")
    assert first is not None and later is not None
    build.write(b.unit_path(first.id), "id: p01.2-build\ntarget: program\nrun: python wc.py\n")
    assert b.stage_run(later) == ("p01.2-build", "python wc.py")
    assert "run: python wc.py (fixed by p01.2-build" in b.entry_text(later)
    build.write(b.unit_path(later.id), "id: p01.3-finale\ntarget: program\nrun: python -m wc\n")
    assert b.run_changed(later) == "p01.2-build"
    m = b.merge_contract(later, {"target": "program", "run": "python -m wc"})
    assert m["run"] == "python wc.py"
