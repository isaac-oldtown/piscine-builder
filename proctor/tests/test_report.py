"""report.rs has no test module; these pin the terminal texts."""

from __future__ import annotations

import pytest
from proctor.unit import Unit

from proctor import engine, report, schema


def test_truncate_counts_characters_and_flattens_lines() -> None:
    assert report.truncate("a\nb\rc", 10) == "a b c"
    assert report.truncate("abcdef", 6) == "abcdef"
    assert report.truncate("abcdef", 4) == "abcd..."
    assert report.truncate("ééééé", 3) == "ééé..."


def test_style_paints_only_with_color() -> None:
    plain = report.Style(color=False, ascii=False)
    color = report.Style(color=True, ascii=True)
    assert plain.red("x") == "x"
    assert color.red("x") == "\x1b[31mx\x1b[0m"
    assert color.green("x") == "\x1b[32mx\x1b[0m"
    assert color.yellow("x") == "\x1b[33mx\x1b[0m"
    assert color.dim("x") == "\x1b[2mx\x1b[0m"
    assert color.bold("x") == "\x1b[1mx\x1b[0m"
    assert (plain.ok_mark(), plain.ko_mark(), plain.skip_mark(), plain.run_mark()) == (
        "✔",
        "✘",
        "·",
        "▸",
    )
    assert color.ok_mark() == "\x1b[32m[ok]\x1b[0m"
    assert color.run_mark() == ">"


def test_style_detect_honours_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PROCTOR_ASCII", "")
    monkeypatch.setenv("NO_COLOR", "")
    s = report.Style.detect()
    assert s.ascii and not s.color
    monkeypatch.delenv("PROCTOR_ASCII")
    assert not report.Style.detect().ascii


def item(ok: bool, name: str, detail: str = "", **kw: bool) -> engine.Item:
    return engine.Item(
        ok=ok,
        name=name,
        detail=detail,
        random=kw.get("random", False),
        course_error=kw.get("course_error", False),
    )


def verdict(first_fail: tuple[str, engine.Item] | None, ok: bool = False) -> engine.Verdict:
    return engine.Verdict(ok=ok, passed=3, total=4, first_fail=first_fail, warnings=0, phases=[])


def test_verdict_lines() -> None:
    s = report.Style(color=False, ascii=False)
    assert report.verdict_line(verdict(None, ok=True), 1, s) == "OK 3/4"
    assert report.verdict_line(verdict(None), 1, s) == "KO"
    t = item(False, "case f(1) == 2", "expected 2, got 3")
    assert (
        report.verdict_line(verdict(("tests", t)), 1, s) == "KO case f(1) == 2: expected 2, got 3"
    )
    r = item(False, "law f", "f(0): expected 1, got 2 (round 3)", random=True)
    assert (
        report.verdict_line(verdict(("regression", r)), 0xAB, s)
        == "KO law f: f(0): expected 1, got 2 (round 3)   (replay: proctor check --seed 00000000000000ab)"
    )
    c = item(False, "compile", "src/a.c:1: error: x")
    assert report.verdict_line(verdict(("build", c)), 1, s) == "KO build: src/a.c:1: error: x"
    f = item(False, "fmt", "not formatted: src/lib.rs:1")
    assert report.verdict_line(verdict(("build", f)), 1, s) == "KO fmt: not formatted: src/lib.rs:1"
    st = item(False, "notes.txt", "is not part of the expected layout")
    assert (
        report.verdict_line(verdict(("structure", st)), 1, s)
        == "KO structure: notes.txt is not part of the expected layout"
    )
    assert report.verdict_line(verdict(None), 1, report.Style(color=True, ascii=False)) == (
        "\x1b[31mKO\x1b[0m"
    )


def term(quiet: bool = False, verbose: bool = False) -> report.Term:
    return report.Term(style=report.Style(color=False, ascii=False), verbose=verbose, quiet=quiet)


def test_term_lines(capsys: pytest.CaptureFixture[str]) -> None:
    t = term()
    u = schema.load(Unit, "id: d01-x\ntitle: X\ngoal: g\n")
    t.start(u, 0xFF, "your work")
    t.phase_begin("build", False)
    t.phase_begin("tests", True)
    t.item(item(True, "case a", ""))
    t.item(item(True, "law f", "200 rounds"))
    t.item(item(False, "case b", "line1\nline2"))
    t.item(item(False, "law g", "course error: no", course_error=True))
    t.item(item(True, "n" * 80))
    t.phase_end(
        engine.Phase(
            name="tests", status=engine.Status.FAIL, summary="3/5", items=[], streamed=True
        )
    )
    t.phase_end(
        engine.Phase(
            name="build",
            status=engine.Status.SKIP,
            summary="skipped (the build failed)",
            items=[],
            streamed=False,
        )
    )
    assert capsys.readouterr().out.split("\n") == [
        "proctor d01-x  seed 00000000000000ff  against your work",
        " ▸ tests",
        "   ✔ case a",
        "   ✔ law f  200 rounds",
        "   ✘ case b  line1 line2",
        "   ✘ law g  course error: no",
        "   ✔ " + "n" * 72 + "...",
        " ✘ tests       3/5",
        " · build       skipped (the build failed)",
        "",
    ]


def test_term_lists_up_to_eight_failures(capsys: pytest.CaptureFixture[str]) -> None:
    items = [item(False, f"use{i}", f"at a.c:{i} is not in Allowed") for i in range(10)]
    p = engine.phase("allowed", items, False, lambda _o, _t: "")
    term().phase_end(p)
    out = capsys.readouterr().out.split("\n")
    assert out[0] == " ✘ allowed     10 problems"
    assert out[1] == "   ✘ use0  at a.c:0 is not in Allowed"
    assert out[8] == "   ✘ use7  at a.c:7 is not in Allowed"
    assert out[9:] == ["   ... and 2 more", ""]


def test_term_log_and_quiet(capsys: pytest.CaptureFixture[str]) -> None:
    term().log("hidden\n")
    term(verbose=True).log("  \n")
    term(verbose=True).log("warning: x\n\n")
    term(quiet=True, verbose=True).log("never")
    term(quiet=True).item(item(True, "x"))
    assert capsys.readouterr().out == "warning: x\n"
