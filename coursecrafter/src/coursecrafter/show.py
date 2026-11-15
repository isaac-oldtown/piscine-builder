"""`coursecrafter show`: the curriculum as a readable overview, without the
truncation of the approval table. Read-only: no LLM, no file written."""

from __future__ import annotations

import os
from pathlib import Path
from typing import TYPE_CHECKING

from proctor.compat import (
    float_display,
    io_error,
    parse_uint,
    read_text,
    split_whitespace,
    trim,
    trim_end,
    trim_start,
    trim_start_matches,
)
from proctor.errors import Error

from coursecrafter import curriculum

if TYPE_CHECKING:
    from proctor import report


def width() -> int:
    """Terminal width from `COLUMNS`, else 100."""
    c = os.environ.get("COLUMNS")
    w = parse_uint(c) if c is not None else None
    return min(max(w if w is not None else 100, 60), 160)


def wrap(text: str, first: str, rest: str, width: int) -> str:
    """Word-wraps `text` to `width`: the first line after `first`, the others after `rest`."""
    out = ""
    line = first
    empty = True
    for word in split_whitespace(text):
        if not empty and len(line) + 1 + len(word) > width:
            out += trim_end(line)
            out += "\n"
            line = rest
            empty = True
        if not empty:
            line += " "
        line += word
        empty = False
    out += trim_end(line)
    out += "\n"
    return out


def hours(h: float | None) -> str:
    return f"{float_display(h)} h" if h is not None else "? h"


def level(d: int | None) -> str:
    return str(d) if d is not None else "?"


def list_(v: list[str]) -> str:
    """Rust `list` (renamed: it would hide the built-in `list`)."""
    if not v:
        return "none"
    return ", ".join(v)


def field(s: str, st: report.Style, label: str, value: str, indent: str, w: int) -> str:
    """The field lines under a unit or project heading."""
    head = f"{indent}{st.dim(f'{label:<11}')} "
    # `head` holds colour codes: wrap on the visible width.
    visible = len(indent) + 12
    wrapped = wrap(value, " " * visible, " " * visible, w)
    s += head
    s += trim_start(wrapped)
    return s


def _sum(hs: list[float]) -> float:
    """Plain left-to-right addition (Python's `sum` compensates rounding
    errors), from 0.0 so that no hours print `0 h`, not `-0 h`."""
    total = 0.0
    for h in hs:
        total += h
    return total


def unit(
    s: str,
    st: report.Style,
    plan: curriculum.Plan,
    e: curriculum.Entry,
    full: bool,
    w: int,
) -> str:
    indent = "    " if e.kind == curriculum.Kind.STAGE else "  "
    code = e.id.split("-")[0]
    tags = ", ".join(
        [
            hours(e.f.hours),
            f"concept {level(e.f.concept_difficulty)}/5",
            f"impl {level(e.f.difficulty)}/5",
            e.f.target if e.f.target is not None else "auto",
        ]
    )
    stretch = st.yellow(" [stretch goal]") if e.f.stretch else ""
    s += f"\n{indent[: len(indent) - 2]}{st.bold(code)}  {st.bold(e.title)}{stretch}  {st.dim(f'({tags})')}\n"
    if e.f.goal is not None:
        s += wrap(e.f.goal, indent, indent, w)
    if e.f.done_when is not None:
        s = field(s, st, "done when", e.f.done_when, indent, w)
    s = field(s, st, "introduces", list_(e.f.introduces), indent, w)
    s = field(s, st, "assumes", list_(e.f.assumes), indent, w)
    notes = [f"{n.title} ({', '.join(n.f.teaches)})" for n in plan.notes_for(e.id)]
    if notes:
        s = field(s, st, "notes", "; ".join(notes), indent, w)
    if full:
        if e.f.allows:
            s = field(s, st, "allows", ", ".join(e.f.allows), indent, w)
        if e.f.scaffold is not None:
            s = field(s, st, "scaffold", e.f.scaffold, indent, w)
        if e.f.gradable is False:
            s = field(
                s,
                st,
                "gradable",
                "no: only files, structure and the build are checked",
                indent,
                w,
            )
        if trim(e.brief):
            s = field(s, st, "brief", trim(e.brief), indent, w)
    return s


def project(
    s: str,
    st: report.Style,
    plan: curriculum.Plan,
    p: curriculum.Entry,
    full: bool,
    w: int,
) -> str:
    stages = plan.stages(p.id)
    req = _sum([x.f.hours for x in stages if not x.f.stretch and x.f.hours is not None])
    extra = _sum([x.f.hours for x in stages if x.f.stretch and x.f.hours is not None])
    size = f"project, {len(stages)} stages, about {curriculum.round5(req)} h"
    if extra > 0.0:
        size += f" + {curriculum.round5(extra)} h stretch"
    code = p.id.split("-")[0]
    s += f"\n{st.bold(code)}  {st.bold(p.title)}  {st.dim(f'({size})')}\n"
    if p.f.goal is not None:
        s += wrap(p.f.goal, "  ", "  ", w)
    if p.f.minimum is not None:
        s = field(s, st, "minimum", p.f.minimum, "  ", w)
    if p.f.assumes:
        s = field(s, st, "assumes", list_(p.f.assumes), "  ", w)
    if full and trim(p.brief):
        s = field(s, st, "scope", trim(p.brief), "  ", w)
    for stage in stages:
        s = unit(s, st, plan, stage, full, w)
    return s


def render(plan: curriculum.Plan, approval: str, full: bool, st: report.Style, w: int) -> str:
    m = plan.meta
    s = f"{st.bold(m.title)}\n"
    facts: list[str] = []
    if m.language is not None:
        facts.append(m.language)
    if m.level is not None:
        facts.append(m.level)
    if m.hours_per_week is not None and m.weeks is not None:
        h, wk = m.hours_per_week, m.weeks
        plural = "" if wk == 1.0 else "s"
        facts.append(f"{float_display(h)} h/week for {float_display(wk)} week{plural}")
    facts.append(approval)
    s += st.dim(" · ".join(facts))
    s += "\n"
    if m.environment is not None:
        s += wrap(f"environment: {m.environment}", "", "  ", w)
    s += trim_start_matches(curriculum.summary(plan), "\n").replace(
        " (c/i column: concept / implementation)", ""
    )
    units = plan.units()
    drills = sum(1 for u in units if u.kind == curriculum.Kind.DRILL)
    projects = sum(1 for e in plan.entries if e.kind == curriculum.Kind.PROJECT)
    stretch = sum(1 for u in units if u.f.stretch)
    s += st.dim(
        f"{drills} drill(s), {projects} project(s), {len(units)} unit(s) in total, {stretch} stretch goal(s)\n"
    )
    for e in plan.entries:
        match e.kind:
            case curriculum.Kind.DRILL:
                s = unit(s, st, plan, e, full, w)
            case curriculum.Kind.PROJECT:
                s = project(s, st, plan, e, full, w)
            case _:
                pass
    return s


def cmd_show(raw: list[str]) -> bool:
    from coursecrafter import cli
    from proctor import report

    a = cli.Args.parse(raw, ["course"], ["full"])
    course = a.course()
    match a.positional:
        case [f]:
            file = Path(f)
        case []:
            file = course / "curriculum.md"
        case _:
            raise Error("usage: coursecrafter show [curriculum.md] [--course DIR] [--full]")
    try:
        text = read_text(file)
    except OSError as e:
        raise Error(f"{file}: {io_error(e)}") from None
    try:
        plan = curriculum.parse(text)
    except Error as e:
        # Rust's `Err(Vec<Problem>)`: the exception carries the problems.
        problems = getattr(e, "problems", None)
        if problems is None:
            raise
        print(f"{file} does not parse:\n{curriculum.report(problems)}")
        return False
    st = report.Style.detect()
    try:
        approved = read_text(cli.approval_path(course))
    except OSError:
        approved = ""
    if not trim(approved):
        approval = st.yellow("not approved")
    elif trim(approved) == cli.text_hash(text):
        approval = st.green("approved")
    else:
        approval = st.yellow("changed since approval")
    print(render(plan, approval, a.has("full"), st, width()), end="")
    problems = plan.validate()
    errors = sum(1 for p in problems if not p.warning)
    print()
    if not problems:
        print(st.green("Validator: no problem."))
    else:
        line = f"Validator: {errors} error(s), {len(problems) - errors} warning(s)."
        print(st.red(line) if errors > 0 else st.yellow(line))
        print(curriculum.report(problems), end="")
    if not a.has("full"):
        print(st.dim("Briefs, Allowed lists and scaffolds: coursecrafter show --full"))
    return errors == 0
