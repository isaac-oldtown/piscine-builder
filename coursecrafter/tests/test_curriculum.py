from __future__ import annotations

import pytest
from coursecrafter.curriculum import ParseError, autofix, parse, problems_of, report, round5

from coursecrafter import prompts

GOOD = "---\ntitle: T\nlanguage: c\n---\n\n## d01-hello: Hello\ngoal: Print.\nconcept_difficulty: 2\ndifficulty: 1\nhours: 2\nintroduces: [compiling]\n\nBrief text.\n\n## note-make: Makefiles\nteaches: [make]\nwith: p01.1-build\n\n## p01-tool: Tool\ngoal: A tool.\nminimum: it builds and runs.\nassumes: [compiling]\n\n### p01.1-build: Build\ngoal: Build it.\nconcept_difficulty: 2\ndifficulty: 2\nhours: 3\nassumes: [make]\nintroduces: [linking]\n\n### p01.2-finale: Finale\ngoal: Finish.\nconcept_difficulty: 2\ndifficulty: 3\nhours: 4\nassumes: [linking]\n"


def parses(text: str) -> bool:
    try:
        parse(text)
    except ParseError:
        return False
    return True


def test_parses_and_validates_a_good_plan() -> None:
    try:
        plan = parse(GOOD)
    except ParseError as e:
        pytest.fail(report(e.problems))
    assert len(plan.entries) == 5
    assert plan.entries[0].brief == "Brief text."
    assert plan.entries[3].parent == "p01-tool"
    problems = plan.validate()
    assert all(p.warning for p in problems), report(problems)
    assert plan.prereqs(plan.entries[2]) == ["d01-hello"]


def test_autofix_renumbers_and_fills_hours() -> None:
    # Stages numbered 2 and 3, a note pointing at the old id, no hours on d01.
    messy = (
        GOOD.replace("p01.1-build", "p01.2-build")
        .replace("p01.2-finale", "p01.3-finale")
        .replace("difficulty: 1\nhours: 2\n", "difficulty: 1\n")
    )
    assert "must be numbered" in report(problems_of(messy))
    fixed, notes = autofix(messy)
    assert "### p01.1-build: Build" in fixed, fixed
    assert "### p01.2-finale: Finale" in fixed, fixed
    assert "with: p01.1-build" in fixed, "the note follows the rename"
    assert "introduces: [compiling]\nhours: 1.5\n\nBrief text." in fixed, fixed
    assert len(notes) == 3, notes
    assert all(p.warning for p in problems_of(fixed)), report(problems_of(fixed))
    assert len(autofix(fixed)[1]) == 0, "a fixed file needs no more fixes"


def test_drills_renumber_without_touching_longer_ids() -> None:
    text = "---\ntitle: T\nlanguage: c\n---\n\n## d03-a: A\ngoal: g.\ndifficulty: 1\nhours: 1\nintroduces: [x]\n\n## d02-ab: B\ngoal: g.\ndifficulty: 1\nhours: 1\nassumes: [x]\n\nSee d03-a first.\n"
    fixed, _ = autofix(text)
    assert "## d01-a: A" in fixed, fixed
    assert "## d02-ab: B" in fixed, fixed
    assert "See d01-a first." in fixed, fixed


def test_first_steps_are_guided_and_may_be_d00() -> None:
    r = report(problems_of(GOOD))
    assert "the first c unit is not `guided: true`" in r, r
    t = GOOD.replace("## d01-hello: Hello\n", "## d00-hello: Hello\nguided: true\n")
    r = report(problems_of(t))
    assert "guided" not in r and "must grow" not in r, r
    t = GOOD.replace("assumes: [make]\n", "assumes: [make]\nguided: true\n")
    assert "`guided` is for drills" in report(problems_of(t))


def test_platform_and_stage_rules() -> None:
    def errors(t: str) -> str:
        return report(problems_of(t))

    assert "platform" not in errors(GOOD)
    # A Linux stage followed by a portable one.
    t = GOOD.replace("assumes: [make]\n", "assumes: [make]\nplatform: linux\n")
    assert "needs `platform: linux` too" in errors(t), errors(t)
    # On the project, it covers every stage.
    t = GOOD.replace("minimum: it builds", "platform: linux\nminimum: it builds")
    assert "platform" not in errors(t), errors(t)
    t = GOOD.replace("hours: 2\n", "hours: 2\nplatform: windows\n")
    assert "unknown platform `windows`" in errors(t)
    # An earlier stage defining what a later feature does.
    t = GOOD.replace(
        "introduces: [linking]\n",
        "introduces: [linking]\n\nOther commands print `not supported yet`.\n",
    )
    assert "its tests run again at every later stage" in errors(t)


def test_planning_rules_minimum_buffer_and_stretch_order() -> None:
    def errors(t: str) -> str:
        return report(problems_of(t))

    # No minimum viable result, no concept difficulty.
    t = GOOD.replace("minimum: it builds and runs.\n", "").replace("concept_difficulty: 2\n", "", 1)
    r = errors(t)
    assert "needs `minimum`" in r, r
    assert "needs `concept_difficulty`" in r, r
    # 9 h required: over a 10 h budget's 88%, under the budget itself.
    t = GOOD.replace("language: c\n", "language: c\nhours_per_week: 5\nweeks: 2\n")
    r = errors(t)
    assert "keep 10 to 15% free" in r, r
    assert "error, line 1" not in r, r
    # A stretch stage followed by a required one.
    t = GOOD.replace("assumes: [make]\n", "assumes: [make]\nstretch: true\n")
    assert "cannot follow a stretch stage" in errors(t)


def test_autofix_quotes_values_starting_with_a_backtick() -> None:
    t = GOOD.replace("goal: Finish.", 'goal: `make` runs "all".')
    assert not parses(t)
    fixed, notes = autofix(t)
    assert r'goal: "`make` runs \"all\"."' in fixed, fixed
    assert len(notes) == 1, notes
    assert all(p.warning for p in problems_of(fixed))


def test_autofix_quotes_colons_and_list_items() -> None:
    t = (
        GOOD.replace("title: T\n", "title: Rust: the basics\n")
        .replace(
            "introduces: [linking]",
            "introduces: [linking, error policy: report and continue, HashMap<K, V>, `?` operator]",
        )
        .replace("assumes: [linking]", "assumes: [linking, error policy: report and continue]")
    )
    assert not parses(t)
    fixed, notes = autofix(t)
    assert 'title: "Rust: the basics"' in fixed, fixed
    assert (
        r'introduces: [linking, "error policy: report and continue", "HashMap<K, V>", "`?` operator"]'
        in fixed
    ), fixed
    assert len(notes) == 3, notes
    problems = problems_of(fixed)
    assert all(p.warning for p in problems), report(problems)
    assert len(autofix(fixed)[1]) == 0, "quoting is idempotent"


def test_the_prompt_example_is_a_valid_curriculum() -> None:
    problems = problems_of(prompts.PLAN_EXAMPLE)
    assert all(p.warning for p in problems), report(problems)
    # It also follows the rules the validator cannot check, since models
    # copy the example more than they read the rules.
    plan = parse(prompts.PLAN_EXAMPLE)
    for u in plan.units():
        for c in u.f.introduces:
            assert " and " not in c, f"{u.id}: `{c}` is two concepts"
        if not u.f.guided and u.parent is None:
            assert len(u.f.introduces) <= 3, f"{u.id}: more than three concepts"


def test_hours_round_to_the_nearest_five() -> None:
    assert round5(59.0) == "60"
    assert round5(33.0) == "35"
    assert round5(6.0) == "5"
    assert round5(2.0) == "under 5"


def test_catches_teach_before_use_and_shape_errors() -> None:
    bad = GOOD.replace("with: p01.1-build", "with: p01.2-finale").replace(
        "difficulty: 3", "difficulty: 9"
    )
    plan = parse(bad)
    text = report(plan.validate())
    assert "assumes `make`" in text, text
    assert "difficulty" in text, text
    assert not parses("no front matter")
    wrong_id = GOOD.replace("## d01-hello: Hello", "## hello: Hello")
    assert not parses(wrong_id)


def test_the_prompt_example_has_no_warning_either() -> None:
    assert problems_of(prompts.PLAN_EXAMPLE) == []


def test_drills_and_notes_may_sit_between_the_stages_of_a_project() -> None:
    plan = parse(prompts.PLAN_EXAMPLE)
    ids = [e.id for e in plan.entries]
    assert ids.index("d03-files") < ids.index("p01.3-finale")
    assert [s.id for s in plan.stages("p01-wc")] == [
        "p01.1-intro",
        "p01.2-build",
        "p01.3-finale",
        "p01.4-consolidate",
        "p01.5-unicode",
    ]
    assert plan.entry("d03-files") is not None and plan.entry("d03-files").parent is None  # type: ignore[union-attr]


def test_a_project_wants_an_introduction_and_a_consolidation_stage() -> None:
    text = report(problems_of(GOOD))
    assert "the first stage of a project is a reading-only introduction" in text, text
    assert "no consolidation stage" in text, text


def test_a_drill_only_a_stretch_unit_needs_is_stretch_too() -> None:
    drill = (
        "## d04-bits: Bits\ngoal: Use bit operations.\nconcept_difficulty: 2\ndifficulty: 2\nhours: 2\n"
        "introduces: [bit operations]\nassumes: [argv]\ntarget: program\n\n"
    )
    text = prompts.PLAN_EXAMPLE.replace("### p01.3-finale", drill + "### p01.3-finale").replace(
        "assumes: [file I/O]\ntarget: program\n\nOnly once",
        "assumes: [file I/O, bit operations]\ntarget: program\n\nOnly once",
    )
    out = report(problems_of(text))
    assert "d04-bits" in out and "only stretch units assume what this drill introduces" in out, out
