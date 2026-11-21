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


def builder(tmp_path: Path, retries: int, cross_check: bool = True) -> build.Builder:
    dead = llm.Llm.resolve(
        llm.Choice(provider="custom", base_url="http://127.0.0.1:9/v1", model="m"),
        config.Config(),
    )
    opts = build.Options(
        retries=retries,
        tests=True,
        only=[],
        yes=True,
        text_only=False,
        debug=False,
        cross_check=cross_check,
    )
    return build.Builder.new(tmp_path, curriculum.parse(PLAN), dead, opts, "ENV")


def scripted(b: build.Builder, replies: dict[str, list[str]]) -> list[tuple[str, str]]:
    asked: list[tuple[str, str]] = []

    def chat(label: str, messages: list[llm.Msg]) -> str:
        kind = label.split("-")[0]
        asked.append((kind, messages[-1].content))
        return replies[kind].pop(0)

    b.chat = chat  # type: ignore[method-assign]
    return asked


def contract_written(b: build.Builder) -> curriculum.Entry:
    u = b.plan.units()[0]
    m = b.merge_contract(u, yamlio.load(CONTRACT))
    assert b.check_contract(u, m, {}) == []
    build.write(b.unit_path(u.id), yamlio.dump(m))
    return u


def test_a_case_its_law_contradicts_is_dropped_without_asking_again(tmp_path: Path) -> None:
    if shutil.which("python") is None:
        pytest.skip("no python")
    b = builder(tmp_path, retries=2, cross_check=False)
    u = contract_written(b)
    # Two laws contradict the wrong case: it goes, they stay.
    tests = TESTS.replace(
        '"double(n) == n + n"] }',
        '"double(n) == n + n"] }\n    - { name: even, gen: { n: int 0..10 }, check: ["double(n) % 2 == 0"] }',
    )
    asked = scripted(b, {"tests": [tests]})
    b.tests(u, b.plan.units(), {})
    assert [k for k, _ in asked] == ["tests"]
    spec = yamlio.load(read_text(b.unit_path(u.id)))
    assert spec["cases"] == "# example\ndouble(2) == 4\n"
    assert len(spec["random"]["laws"]) == 2
    assert "contradicted by 2 laws" in read_text(b.log.path)


def test_a_law_and_a_case_that_only_disagree_with_each_other_both_go(tmp_path: Path) -> None:
    if shutil.which("python") is None:
        pytest.skip("no python")
    b = builder(tmp_path, retries=2, cross_check=False)
    u = contract_written(b)
    tests = TESTS.replace(
        '"double(n) == n + n"] }',
        '"double(n) == n + n"] }\n    - { name: positive, gen: { n: int 1..10 }, check: ["double(n) > n"] }',
    )
    scripted(b, {"tests": [tests]})
    b.tests(u, b.plan.units(), {})
    spec = yamlio.load(read_text(b.unit_path(u.id)))
    assert spec["cases"] == "# example\ndouble(2) == 4\n"
    assert [x["name"] for x in spec["random"]["laws"]] == ["positive"]


def test_a_disputed_case_goes_back_once_then_is_dropped(tmp_path: Path) -> None:
    if shutil.which("python") is None:
        pytest.skip("no python")
    b = builder(tmp_path, retries=2)
    u = contract_written(b)
    # The writer keeps its wrong case, which no law contradicts; the reading finds 6.
    tests = TESTS.replace('"double(n) == n + n"', '"double(n) >= n"')
    asked = scripted(
        b, {"tests": [tests, tests], "verify": ["<answers>\n1: 4\n2: 6\n3: true\n</answers>"]}
    )
    b.tests(u, b.plan.units(), {})
    assert [k for k, _ in asked] == ["tests", "verify", "tests"]
    # The independent reading never sees the tests or their results.
    verify = asked[1][1]
    assert "double(3) == 7" not in verify and "== 4" not in verify
    assert "the value of `double(3)`" in verify
    retry = asked[2][1]
    assert "finds `6` where the case expects `7`" in retry
    spec = yamlio.load(read_text(b.unit_path(u.id)))
    assert spec["cases"] == "# example\ndouble(2) == 4\n"
    assert len(spec["random"]["laws"]) == 1
    assert b.verified(u.id)
    # A suite changed by hand is no longer the one that was checked.
    spec["cases"] += "double(5) == 10\n"
    build.write(b.unit_path(u.id), yamlio.dump(spec))
    assert not b.verified(u.id)


def test_tests_proctor_cannot_use_are_dropped_without_asking_again(tmp_path: Path) -> None:
    if shutil.which("python") is None:
        pytest.skip("no python")
    b = builder(tmp_path, retries=2, cross_check=False)
    u = contract_written(b)
    tests = """<tests>
cases: |
  double(2) == 4
  double(2) ==
  triple(2) == 6
  double(1) == 2
  double(3) == 6
random:
  laws:
    - { name: off by one, gen: { n: int 0..10 }, check: ["double(n) == n + 1"] }
    - { name: even, gen: { n: int 0..10 }, check: ["double(n) % 2 == 0"] }
    - { name: unknown gen, gen: { n: nonsense 1..2 }, check: ["double(n) > 0"] }
think_about: [zero]
notes: not a key of <tests>
</tests>"""
    asked = scripted(b, {"tests": [tests]})
    b.tests(u, b.plan.units(), {})
    assert [k for k, _ in asked] == ["tests"]
    spec = yamlio.load(read_text(b.unit_path(u.id)))
    # The law that contradicts two cases goes; the cases stay.
    assert spec["cases"] == "double(2) == 4\ndouble(1) == 2\ndouble(3) == 6\n"
    assert [x["name"] for x in spec["random"]["laws"]] == ["even"]
    assert "think_about" not in spec
    log = read_text(b.log.path)
    assert "dropped 4 test(s)" in log
    assert "unknown key `notes`" in log


def test_contracts_are_repaired_in_form() -> None:
    m = {
        "goal": "Count \u2014 then stop.",
        "stubs": ["m.py"],
        "functions": [{"name": "m.f", "sig": "def f() -> int", "returns": "1 \u2013 2"}],
        "requirements_note": "x",
    }
    fixes = build.repair_contract(m)
    assert m["goal"] == "Count - then stop."
    assert m["functions"][0]["returns"] == "1 - 2"
    assert m["stubs"] == "m.py"
    assert "requirements_note" not in m
    assert len(fixes) == 3


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
