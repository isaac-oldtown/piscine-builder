"""The curriculum: one markdown file, written by a browser chat, by
`coursecrafter new`, or by hand. It is the contract between planning and
generation, so it is parsed strictly and every problem is reported in a
form that can be pasted back to whoever wrote it.

```text
---
title: C from scratch
language: c
---
## d01-hello: Hello
goal: Compile and run a first program.
difficulty: 1
introduces: [compiling, printf]

Free text: a brief for the generator.
```"""

from __future__ import annotations

import enum
import json
import math
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from proctor.compat import WHITESPACE, float_display, lines, parse_uint, regex, trim, trim_end
from proctor.errors import Error
from proctor.schema import U8

from proctor import schema

if TYPE_CHECKING:
    from coursecrafter import env


@dataclass(kw_only=True)
class Meta:
    title: str
    language: str | None = None
    level: str | None = None
    hours_per_week: float | None = None
    # Course length; with `hours_per_week` it sets the hour budget.
    weeks: float | None = None
    description: str | None = None
    # Where students will follow the course, when it differs from the
    # machine building it (OS, compilers, tools installed).
    environment: str | None = None
    # How thoroughly the tests probe: relaxed, standard or strict (default).
    rigor: str | None = None
    # Course-wide habits the units rely on (quoted in RULES.md).
    conventions: str | None = None


@dataclass(kw_only=True)
class Fields:
    kind: str | None = None
    language: str | None = None
    difficulty: U8 | None = None
    goal: str | None = None
    introduces: list[str] = field(default_factory=list)
    assumes: list[str] = field(default_factory=list)
    target: str | None = None
    scaffold: str | None = None
    allows: list[str] = field(default_factory=list)
    gradable: bool | None = None
    teaches: list[str] = field(default_factory=list)
    with_: str | None = field(default=None, metadata={"yaml": "with"})
    # Planning estimate of implementation and debugging time. Never shown
    # in the instructions.
    hours: float | None = None
    # Optional unit, skippable by the student.
    stretch: bool = False
    # The instructions walk the student through it step by step (a first
    # contact with a language or its toolchain).
    guided: bool = False
    # `linux`: needs Linux APIs; built and tested in a Linux container on
    # other systems. On a project, applies to every stage.
    platform: str | None = None
    # One line: the observable behaviour that means the unit is done.
    done_when: str | None = None
    # How hard the ideas are to understand (`difficulty` is how hard they
    # are to implement and debug).
    concept_difficulty: U8 | None = None
    # Projects: what the project must do to count as a success.
    minimum: str | None = None


class Kind(enum.Enum):
    DRILL = "drill"
    PROJECT = "project"
    STAGE = "stage"
    NOTE = "note"

    def name(self) -> str:  # type: ignore[override]
        """Rust `Kind::name` (also the member's value)."""
        return str(self.value)


@dataclass(kw_only=True)
class Entry:
    id: str
    title: str
    kind: Kind
    f: Fields
    brief: str
    parent: str | None
    line: int


@dataclass(kw_only=True)
class Problem:
    line: int
    id: str | None
    msg: str
    warning: bool

    @staticmethod
    def err(line: int, id: str | None, msg: str) -> Problem:
        return Problem(line=line, id=id, msg=msg, warning=False)

    @staticmethod
    def warn(line: int, id: str | None, msg: str) -> Problem:
        return Problem(line=line, id=id, msg=msg, warning=True)


class ParseError(Error):
    """Rust's `Err(Vec<Problem>)` from `parse`: the problems, with their
    `report` as the message."""

    def __init__(self, problems: list[Problem]) -> None:
        super().__init__(report(problems))
        self.problems = problems


_STAGE = regex(r"^p\d{2,}\.\d+-[a-z0-9]+(-[a-z0-9]+)*$")
_PROJECT = regex(r"^p\d{2,}-[a-z0-9]+(-[a-z0-9]+)*$")
_DRILL = regex(r"^d\d{2,}-[a-z0-9]+(-[a-z0-9]+)*$")
_NOTE = regex(r"^note-[a-z0-9]+(-[a-z0-9]+)*$")
# `^(#{2,3})\s+(\S+?)\s*:\s*(.+?)\s*$` with Rust's `\s` (Python's also
# matches U+001C..U+001F).
_S = f"[{WHITESPACE}]"
_HEADING = regex(rf"^(#{{2,3}}){_S}+([^{WHITESPACE}]+?){_S}*:{_S}*(.+?){_S}*$")


def kind_of(id: str) -> Kind | None:
    if _STAGE.search(id):
        return Kind.STAGE
    elif _PROJECT.search(id):
        return Kind.PROJECT
    elif _DRILL.search(id):
        return Kind.DRILL
    elif _NOTE.search(id):
        return Kind.NOTE
    else:
        return None


def parse(text: str) -> Plan:
    """Raises `ParseError` (Rust's `Err(Vec<Problem>)`)."""
    text = text.replace("\r\n", "\n")
    ls = lines(text)
    problems: list[Problem] = []
    i = 0
    while i < len(ls) and not trim(ls[i]):
        i += 1
    if i >= len(ls) or trim(ls[i]) != "---":
        raise ParseError(
            [
                Problem.err(
                    i + 1,
                    None,
                    "the file must start with a `---` front matter block (title, language)",
                )
            ]
        )
    fm_start = i + 1
    fm_end = next((j for j in range(fm_start, len(ls)) if trim(ls[j]) == "---"), None)
    if fm_end is None:
        raise ParseError([Problem.err(fm_start, None, "the front matter is not closed by `---`")])
    try:
        meta = schema.load(Meta, "\n".join(ls[fm_start:fm_end]))
    except Error as e:
        raise ParseError([Problem.err(fm_start, None, f"front matter: {e.msg}")]) from None
    entries: list[Entry] = []
    last_project: str | None = None
    j = fm_end + 1
    while j < len(ls):
        line = ls[j]
        level = len(line) - len(line.lstrip("#"))
        if not (level == 2 or level == 3) or not line[level:].startswith(" "):
            j += 1
            continue
        c = _HEADING.search(line)
        if c is None:
            problems.append(
                Problem.err(
                    j + 1,
                    None,
                    f"headings must read `## id: Title`, got `{trim(line)}`",
                )
            )
            j += 1
            continue
        hashes, id, title = len(c[1]), c[2], c[3]
        start = j + 1
        k = start
        while k < len(ls) and trim(ls[k]) and not ls[k].startswith("#"):
            k += 1
        yaml = "\n".join(ls[start:k])
        b = k
        while b < len(ls) and not (ls[b].startswith("## ") or ls[b].startswith("### ")):
            b += 1
        brief = trim("\n".join(ls[k:b]))
        if not trim(yaml):
            f = Fields()
        else:
            try:
                f = schema.load(Fields, yaml)
            except Error as e:
                problems.append(Problem.err(start, id, e.msg))
                j = b
                continue
        detected = kind_of(id)
        if f.kind is not None:
            match f.kind:
                case "drill":
                    kind = Kind.DRILL
                case "project":
                    kind = Kind.PROJECT
                case "stage":
                    kind = Kind.STAGE
                case "note":
                    kind = Kind.NOTE
                case other:
                    problems.append(
                        Problem.err(
                            start,
                            id,
                            f"unknown kind `{other}` (drill, project, stage, note)",
                        )
                    )
                    j = b
                    continue
            if detected is not None and detected != kind:
                problems.append(
                    Problem.err(
                        j + 1,
                        id,
                        f"id `{id}` does not look like a {kind.name()}",
                    )
                )
        elif detected is not None:
            kind = detected
        else:
            problems.append(
                Problem.err(
                    j + 1,
                    id,
                    f"id `{id}` must be dNN-slug, pNN-slug, pNN.k-slug or note-slug (lowercase, hyphens)",
                )
            )
            j = b
            continue
        parent: str | None
        if kind == Kind.STAGE:
            if hashes != 3:
                problems.append(
                    Problem.err(j + 1, id, "a stage heading uses `###`, under its project")
                )
            if last_project is None:
                problems.append(
                    Problem.err(j + 1, id, "a stage must follow its project's `##` heading")
                )
            parent = last_project
        elif kind == Kind.PROJECT:
            last_project = id
            parent = None
        else:
            if hashes == 3:
                problems.append(Problem.err(j + 1, id, "only project stages use `###` headings"))
            last_project = None
            parent = None
        entries.append(
            Entry(
                id=id,
                title=title,
                kind=kind,
                f=f,
                brief=brief,
                parent=parent,
                line=j + 1,
            )
        )
        j = b
    if problems:
        raise ParseError(problems)
    return Plan(meta=meta, entries=entries)


def norm(concept: str) -> str:
    return trim(concept).lower()


def _sum(hs: list[float]) -> float:
    """Rust's `Iterator::sum` of f64s: plain left-to-right addition from
    -0.0 (Python's `sum` compensates rounding errors and starts at 0)."""
    total = -0.0
    for h in hs:
        total += h
    return total


def _num(id: str) -> int:
    """The number after the first letter of an id (0 when there is none, or
    when it does not fit a u32)."""
    digits = ""
    for c in id[1:]:
        if not "0" <= c <= "9":
            break
        digits += c
    n = parse_uint(digits, (1 << 32) - 1)
    return n if n is not None else 0


@dataclass(kw_only=True)
class Plan:
    meta: Meta
    entries: list[Entry]

    def units(self) -> list[Entry]:
        """Drills and stages, in teaching order: the units proctor will grade."""
        return [e for e in self.entries if e.kind in (Kind.DRILL, Kind.STAGE)]

    def entry(self, id: str) -> Entry | None:
        return next((e for e in self.entries if e.id == id), None)

    def language(self, e: Entry) -> str:
        lang = e.f.language
        if lang is None and e.parent is not None:
            p = self.entry(e.parent)
            if p is not None:
                lang = p.f.language
        if lang is None:
            lang = self.meta.language
        return (lang if lang is not None else "").lower()

    def platform(self, e: Entry) -> str | None:
        """`linux` when the unit (or its project) needs Linux APIs."""
        if e.f.platform is not None:
            return e.f.platform
        if e.parent is not None:
            p = self.entry(e.parent)
            if p is not None:
                return p.f.platform
        return None

    def notes_for(self, unit: str) -> list[Entry]:
        return [e for e in self.entries if e.kind == Kind.NOTE and e.f.with_ == unit]

    def stages(self, project: str) -> list[Entry]:
        return [e for e in self.entries if e.parent == project]

    def prereqs(self, e: Entry) -> list[str]:
        """Earlier units whose `introduces` cover what `e` assumes."""
        wanted = {norm(a) for a in e.f.assumes}
        out: list[str] = []
        for u in self.units():
            if u.id == e.id:
                break
            if u.parent is not None and u.parent == e.parent:
                continue
            if any(norm(c) in wanted for c in u.f.introduces):
                id = u.parent if u.parent is not None else u.id
                if id not in out:
                    out.append(id)
        return out

    def validate(self) -> list[Problem]:
        from proctor import lang as proctor_lang

        p: list[Problem] = []
        if not trim(self.meta.title):
            p.append(Problem.err(2, None, "front matter needs a `title`"))
        r = self.meta.rigor
        if r is not None and trim(r) not in ("relaxed", "standard", "strict"):
            p.append(Problem.err(2, None, f"`rigor: {r}` must be relaxed, standard or strict"))
        seen: set[str] = set()
        for e in self.entries:
            if e.id in seen:
                p.append(Problem.err(e.line, e.id, "duplicate id"))
            seen.add(e.id)
        # Numbering grows in teaching order.
        # Drills may start at d00 (a first-steps unit before d01).
        last_d: int | None = None
        last_p = 0
        for e in self.entries:
            match e.kind:
                case Kind.DRILL:
                    n = _num(e.id)
                    if last_d is not None and n <= last_d:
                        p.append(
                            Problem.err(
                                e.line,
                                e.id,
                                f"drill numbers must grow in order (after d{last_d:02})",
                            )
                        )
                    last_d = n
                case Kind.PROJECT:
                    n = _num(e.id)
                    if n <= last_p:
                        p.append(
                            Problem.err(
                                e.line,
                                e.id,
                                f"project numbers must grow in order (after p{last_p:02})",
                            )
                        )
                    last_p = n
                case _:
                    pass
        for proj in [e for e in self.entries if e.kind == Kind.PROJECT]:
            stages = self.stages(proj.id)
            if len(stages) < 2:
                p.append(
                    Problem.err(
                        proj.line,
                        proj.id,
                        "a project needs at least two stages, the last one being its finale",
                    )
                )
            code = proj.id.split("-")[0]
            for k, s in enumerate(stages):
                want = f"{code}.{k + 1}-"
                if not s.id.startswith(want):
                    p.append(
                        Problem.err(
                            s.line,
                            s.id,
                            f"stage {k + 1} of {proj.id} must be numbered `{want}...`",
                        )
                    )
            if proj.f.goal is None or not trim(proj.f.goal):
                p.append(Problem.err(proj.line, proj.id, "a project needs a one-sentence `goal`"))
        unit_ids = {u.id for u in self.units()}
        for e in self.entries:
            id = e.id
            match e.kind:
                case Kind.DRILL | Kind.STAGE:
                    if e.f.goal is None or not trim(e.f.goal):
                        p.append(Problem.err(e.line, id, "needs a one-sentence `goal`"))
                    d = e.f.difficulty
                    if d is None:
                        p.append(Problem.err(e.line, id, "needs `difficulty` (1 to 5)"))
                    elif not 1 <= d <= 5:
                        p.append(Problem.err(e.line, id, "`difficulty` must be 1 to 5"))
                    t = e.f.target
                    if t is not None and t != "function" and t != "program":
                        p.append(Problem.err(e.line, id, "`target` must be function or program"))
                    sc = e.f.scaffold
                    if sc is not None and sc not in ("none", "minimal", "full"):
                        p.append(
                            Problem.err(e.line, id, "`scaffold` must be none, minimal or full")
                        )
                    lang = self.language(e)
                    if not lang:
                        p.append(
                            Problem.err(
                                e.line,
                                id,
                                "no language: set `language` here or in the front matter",
                            )
                        )
                    if e.f.target == "function" and lang and not proctor_lang.has_harness(lang):
                        p.append(
                            Problem.err(
                                e.line,
                                id,
                                f"`target: function` is available for c, python and rust; use `target: program` for {lang}",
                            )
                        )
                    if e.f.gradable is False:
                        p.append(
                            Problem.warn(
                                e.line,
                                id,
                                "gradable: false, so proctor will only check files, structure and the build",
                            )
                        )
                    if e.f.teaches or e.f.with_ is not None:
                        p.append(Problem.err(e.line, id, "`teaches` and `with` are for notes"))
                case Kind.NOTE:
                    if not e.f.teaches:
                        p.append(Problem.err(e.line, id, "a note needs `teaches: [...]`"))
                    w = e.f.with_
                    if w is None:
                        p.append(
                            Problem.err(
                                e.line,
                                id,
                                "a note needs `with: <unit id>` (revealed together with that unit)",
                            )
                        )
                    elif w not in unit_ids:
                        p.append(
                            Problem.err(
                                e.line,
                                id,
                                f"`with: {w}` is not a drill or stage of this curriculum",
                            )
                        )
                case Kind.PROJECT:
                    pass
        # Teach before use.
        known: set[str] = set()
        project_checked: set[str] = set()
        for u in self.units():
            for note in self.notes_for(u.id):
                known.update(norm(c) for c in note.f.teaches)
            needs: list[tuple[str, Entry]] = [(a, u) for a in u.f.assumes]
            pid = u.parent
            if pid is not None and pid not in project_checked:
                project_checked.add(pid)
                parent = self.entry(pid)
                if parent is not None:
                    needs.extend((a, parent) for a in parent.f.assumes)
            for a, owner in needs:
                if norm(a) not in known:
                    p.append(
                        Problem.err(
                            owner.line,
                            owner.id,
                            f"assumes `{a}`, which no earlier unit introduces and no note teaches; add a drill, a stage or a note for it, or move this unit later",
                        )
                    )
            known.update(norm(c) for c in u.f.introduces)
        if not self.units():
            p.append(Problem.err(1, None, "the curriculum has no drill or stage"))
        p.extend(self.quality())
        return p

    def required_hours(self) -> float:
        """Hours of the required units (stretch goals excluded)."""
        return _sum([u.f.hours for u in self.units() if not u.f.stretch and u.f.hours is not None])

    def stretch_hours(self) -> float:
        return _sum([u.f.hours for u in self.units() if u.f.stretch and u.f.hours is not None])

    def budget(self) -> float | None:
        if self.meta.hours_per_week is None or self.meta.weeks is None:
            return None
        return self.meta.hours_per_week * self.meta.weeks

    def quality(self) -> list[Problem]:
        """Planning checks beyond the format: workload, stretch goals, units that
        nothing uses, difficulty spread, wording."""
        p: list[Problem] = []
        units = self.units()
        for u in units:
            id = u.id
            h = u.f.hours
            if h is None:
                p.append(
                    Problem.err(
                        u.line,
                        id,
                        "needs `hours`: an honest estimate of implementation and debugging time",
                    )
                )
            elif h <= 0.0:
                p.append(Problem.err(u.line, id, "`hours` must be positive"))
            elif h > 16.0:
                p.append(
                    Problem.warn(
                        u.line,
                        id,
                        f"{float_display(h)} hours is more than two days of work: split it into smaller units",
                    )
                )
            d = u.f.concept_difficulty
            if d is None:
                p.append(
                    Problem.err(
                        u.line,
                        id,
                        "needs `concept_difficulty` (1 to 5): how hard the ideas are to understand, next to `difficulty` for implementation",
                    )
                )
            elif not 1 <= d <= 5:
                p.append(Problem.err(u.line, id, "`concept_difficulty` must be 1 to 5"))
            if u.f.done_when is None or not trim(u.f.done_when):
                p.append(
                    Problem.warn(
                        u.line,
                        id,
                        "no `done_when`: say in one line what behaviour means the unit is done",
                    )
                )
        # Budget.
        budget = self.budget()
        if budget is not None:
            req = self.required_hours()
            # At least 12% of the budget stays free for debugging and revisiting concepts.
            if req > budget:
                p.append(
                    Problem.err(
                        1,
                        None,
                        f"required units total {_fixed0(req)} h for a budget of {_fixed0(budget)} h (hours_per_week x weeks): cut scope, mark units `stretch: true`, or extend the weeks",
                    )
                )
            elif req > budget * 0.88:
                p.append(
                    Problem.warn(
                        1,
                        None,
                        f"required units total {_fixed0(req)} h of a {_fixed0(budget)} h budget: keep 10 to 15% free for debugging and revisiting concepts",
                    )
                )
        # First steps: the first unit of the course's language takes the student
        # by the hand (toolchain, compiling and running, basic syntax).
        seen_langs: set[str] = set()
        for u in units:
            lang = self.language(u)
            main = self.meta.language.lower() if self.meta.language is not None else None
            new_lang = lang not in seen_langs
            seen_langs.add(lang)
            if new_lang and not u.f.guided and main == lang:
                p.append(
                    Problem.warn(
                        u.line,
                        u.id,
                        f"the first {lang} unit is not `guided: true`: start the language with a short guided first-steps drill (install check, compiling and running, basic syntax, a first program built step by step), unless the student already writes {lang}",
                    )
                )
            if u.f.guided and u.kind != Kind.DRILL:
                p.append(
                    Problem.err(
                        u.line,
                        u.id,
                        "`guided` is for drills: a project stage is not walked through",
                    )
                )
        # Stretch goals: optional means nothing required may depend on them.
        stretch_only: dict[str, str] = {}
        required_known: set[str] = set()
        for u in units:
            for note in self.notes_for(u.id):
                required_known.update(norm(c) for c in note.f.teaches)
            if not u.f.stretch:
                for a in u.f.assumes:
                    a = norm(a)
                    by = stretch_only.get(a)
                    if a not in required_known and by is not None:
                        p.append(
                            Problem.err(
                                u.line,
                                u.id,
                                f"assumes `{a}`, which only the stretch goal {by} introduces; a skipped stretch goal would leave a gap",
                            )
                        )
            for c in u.f.introduces:
                if u.f.stretch:
                    stretch_only.setdefault(norm(c), u.id)
                else:
                    required_known.add(norm(c))
        for proj in [e for e in self.entries if e.kind == Kind.PROJECT]:
            stages = self.stages(proj.id)
            if proj.f.minimum is None or not trim(proj.f.minimum):
                p.append(
                    Problem.err(
                        proj.line,
                        proj.id,
                        "a project needs `minimum`: one line saying what it must do to count as a success",
                    )
                )
            first_stretch = next((i for i, s in enumerate(stages) if s.f.stretch), None)
            if first_stretch is not None:
                late = next((s for s in stages[first_stretch:] if not s.f.stretch), None)
                if late is not None:
                    p.append(
                        Problem.err(
                            late.line,
                            late.id,
                            "a required stage cannot follow a stretch stage: stretch stages come after the required finale",
                        )
                    )
            if stages and stages[0].f.stretch:
                p.append(
                    Problem.err(
                        stages[0].line,
                        stages[0].id,
                        "the first stage of a project cannot be a stretch goal",
                    )
                )
            # Regression reruns earlier stages with the current one, on its system.
            first = next(
                (i for i, s in enumerate(stages) if self.platform(s) is not None),
                None,
            )
            if first is not None:
                late = next((s for s in stages[first:] if self.platform(s) is None), None)
                if late is not None:
                    p.append(
                        Problem.err(
                            late.line,
                            late.id,
                            f"{stages[first].id} before it needs Linux, so this stage needs `platform: linux` too (or put it on the project)",
                        )
                    )
            # Earlier stages are tested again at every later stage.
            for st in stages[: max(len(stages) - 1, 0)]:
                if "not supported yet" in st.brief.lower():
                    p.append(
                        Problem.err(
                            st.line,
                            st.id,
                            "do not specify what this stage does with a feature a later stage adds (such as a `not supported yet` message): its tests run again at every later stage and would fail there",
                        )
                    )
        for e in [e for e in self.entries if e.kind != Kind.NOTE]:
            pl = e.f.platform
            if pl is not None and pl != "linux":
                p.append(
                    Problem.err(
                        e.line,
                        e.id,
                        f"unknown platform `{pl}`: the only value is `linux`, for units that need Linux APIs",
                    )
                )
        # Progression: hard stages do not stack new concepts, and rely on
        # concepts practised in a unit, not only read about in a note.
        practised = {norm(c) for u in units for c in u.f.introduces}
        for u in units:
            if not (u.kind == Kind.STAGE and u.f.difficulty is not None and u.f.difficulty >= 4):
                continue
            if len(u.f.introduces) >= 3:
                p.append(
                    Problem.warn(
                        u.line,
                        u.id,
                        f"a hard stage introducing {len(u.f.introduces)} new concepts: practise some of them in a small drill first",
                    )
                )
            for a in u.f.assumes:
                if norm(a) not in practised:
                    p.append(
                        Problem.warn(
                            u.line,
                            u.id,
                            f"relies on `{a}`, which only a note covers: practise it in a drill before this hard stage",
                        )
                    )
        # Units nothing uses: a drill must serve something later.
        for i, u in enumerate(units):
            if u.kind != Kind.DRILL or u.f.stretch or not u.f.introduces:
                continue
            later = units[i + 1 :]
            if not later:
                continue
            used = any(
                any(any(norm(a) == norm(c) for a in lt.f.assumes) for lt in later)
                for c in u.f.introduces
            ) or any(
                e.kind == Kind.PROJECT
                and e.line > u.line
                and any(any(norm(c) == norm(a) for c in u.f.introduces) for a in e.f.assumes)
                for e in self.entries
            )
            if not used:
                p.append(
                    Problem.warn(
                        u.line,
                        u.id,
                        "nothing later assumes what this drill introduces: tie it to a later unit, fold it into another one, or mark it stretch",
                    )
                )
        # Difficulty spread: 5 stays rare, and one level must not dominate.
        diffs = [u.f.difficulty for u in units if u.f.difficulty is not None]
        if diffs:
            fives = sum(1 for d in diffs if d == 5)
            if fives > max(len(diffs) // 10, 1):
                p.append(
                    Problem.warn(
                        1,
                        None,
                        f"{fives} units at difficulty 5: keep 5 for the one or two hardest units of the course",
                    )
                )
            if len(diffs) >= 8:
                for level in range(1, 6):
                    n = sum(1 for d in diffs if d == level)
                    if n * 100 > len(diffs) * 55:
                        p.append(
                            Problem.warn(
                                1,
                                None,
                                f"{n} of {len(diffs)} units are at difficulty {level}: spread the scale so it carries information",
                            )
                        )
        return p

    def preflight(self, env: env.Env) -> list[Problem]:
        """Languages used, with a warning when the toolchain is missing, and
        tools the units rely on that this machine does not have."""
        from coursecrafter import env as cc_env
        from proctor import lang as proctor_lang
        from proctor import run

        out: list[Problem] = []
        for u in self.units():
            text = " | ".join(s.lower() for s in [*u.f.introduces, *u.f.assumes, *u.f.allows])
            for tool in cc_env.MENTIONABLE:
                r = re.compile(rf"\b{re.escape(tool)}\b")
                if r.search(text) and not env.has(tool):
                    out.append(
                        Problem.warn(
                            u.line,
                            u.id,
                            f"relies on `{tool}`, which is not installed on this machine; its tests cannot be verified here",
                        )
                    )
        langs: dict[str, int] = {}
        for u in self.units():
            langs.setdefault(self.language(u), u.line)
        for lang, line in sorted(langs.items()):
            match lang:
                case "c":
                    tool, ok = "a C compiler (cc, gcc or clang)", proctor_lang.cc() is not None
                case "python":
                    tool, ok = "Python 3", proctor_lang.python() is not None
                case "rust":
                    tool, ok = "cargo", run.exists("cargo")
                case "bash" | "sh" | "shell":
                    tool, ok = "bash", run.exists(proctor_lang.resolve("bash"))
                case _:
                    continue
            if not ok:
                out.append(
                    Problem.warn(
                        line,
                        None,
                        f"{lang}: {tool} is not installed on this machine; install it before building the tests",
                    )
                )
        return out


def _fixed0(x: float) -> str:
    """Rust `format!("{x:.0}")`: rounded half to even, `-0` kept."""
    return f"{x:.0f}"


def _round(x: float) -> float:
    """Rust `f64::round`: half away from zero (Python's `round` is half to even)."""
    if not math.isfinite(x):
        return x
    t = float(math.trunc(x))
    if abs(x - t) >= 0.5:
        t += math.copysign(1.0, x)
    return math.copysign(t, x) if t == 0 else t


def default_hours(difficulty: int) -> float:
    """Rough planning hours for a difficulty, used when `hours` is missing."""
    match difficulty:
        case 0 | 1:
            return 1.5
        case 2:
            return 3.0
        case 3:
            return 5.0
        case 4:
            return 8.0
        case _:
            return 12.0


def _id_char(c: str) -> bool:
    return (c.isascii() and c.isalnum()) or c == "-" or c == "_"


def replace_id(text: str, old: str, new: str) -> str:
    """Replaces whole ids only: `d02-a` must not match inside `d02-ab`."""
    out = ""
    rest = text
    while (i := rest.find(old)) != -1:
        before = rest[i - 1] if i > 0 else None
        tail = rest[i + len(old) :]
        after = tail[0] if tail else None
        out += rest[:i]
        if (before is not None and (_id_char(before) or before == ".")) or (
            after is not None and _id_char(after)
        ):
            out += old
        else:
            out += new
        rest = tail
    out += rest
    return out


def quoted(v: str) -> str:
    return '"' + v.replace("\\", "\\\\").replace('"', '\\"') + '"'


def is_quoted(v: str) -> bool:
    return len(v) >= 2 and (
        (v.startswith('"') and v.endswith('"')) or (v.startswith("'") and v.endswith("'"))
    )


def needs_quotes(v: str, in_list: bool) -> bool:
    """A plain scalar YAML would misread: `a: b` becomes a map, a leading
    backtick or bracket is a syntax error, ` #` starts a comment."""
    if not v or is_quoted(v):
        return False
    starts_badly = v[0] in "`[{&*!|>%@'\""
    list_breakers = in_list and any(c in v for c in ",[]{}")
    return starts_badly or ": " in v or v.endswith(":") or " #" in v or list_breakers


def split_items(inner: str) -> list[str]:
    """Splits the inside of a flow list at top-level commas; commas inside
    `<...>`, `(...)`, `[...]` or backticks stay in their item (`HashMap<K, V>`)."""
    items: list[str] = []
    cur = ""
    depth, tick, quote = 0, False, None
    for c in inner:
        if c in "\"'" and not tick and (quote is None or quote == c):
            quote = None if quote is not None else c
        elif c == "`" and quote is None:
            tick = not tick
        elif c in "<([{" and quote is None and not tick:
            depth += 1
        elif c in ">)]}" and quote is None and not tick:
            depth -= 1
        elif c == "," and quote is None and not tick and depth <= 0:
            items.append(trim(cur))
            cur = ""
            continue
        cur += c
    if trim(cur):
        items.append(trim(cur))
    return items


_SCALAR = regex(r"^(title|description|environment|goal|done_when|minimum):[ \t]+(.+?)[ \t]*$")
_LIST = regex(r"^(introduces|assumes|allows|teaches):[ \t]+\[(.*)\][ \t]*$")


def quote_yaml_values(text: str) -> tuple[str, list[str]]:
    """Quotes values YAML would reject or misread, on the known keys: scalars
    (`title: Rust: the basics`) and items of lists
    (`introduces: [error policy: report and continue, HashMap<K, V>]`)."""
    fixes: list[str] = []

    def fix(i: int, line: str) -> str:
        c = _SCALAR.search(line)
        if c is not None and needs_quotes(c[2], False):
            fixes.append(f"line {i + 1}: quoted the `{c[1]}` value")
            return f"{c[1]}: {quoted(c[2])}"
        c = _LIST.search(line)
        if c is not None:
            items = split_items(c[2])
            if any(needs_quotes(it, True) for it in items):
                fixed = [quoted(it) if needs_quotes(it, True) else it for it in items]
                fixes.append(f"line {i + 1}: quoted items of the `{c[1]}` list")
                return f"{c[1]}: [{', '.join(fixed)}]"
        return line

    out = [fix(i, line) for i, line in enumerate(text.split("\n"))]
    return "\n".join(out), fixes


def autofix(text: str) -> tuple[str, list[str]]:
    """Mechanical corrections that are not worth an LLM call: drill and project
    numbers that do not grow, stages numbered out of sequence (references in
    `with:` and briefs follow the new ids), and missing `hours` (filled from
    the difficulty). Returns the fixed text and one line per correction."""
    text, fixes = quote_yaml_values(text.replace("\r\n", "\n"))
    try:
        plan = parse(text)
    except ParseError:
        return text, fixes

    def slug(id: str) -> str:
        parts = id.split("-", 1)
        return parts[1] if len(parts) == 2 else ""

    def grows(ids: list[Entry]) -> bool:
        return all(_num(ids[k + 1].id) > _num(ids[k].id) for k in range(len(ids) - 1))

    renames: list[tuple[str, str]] = []
    drills = [e for e in plan.entries if e.kind == Kind.DRILL]
    if not grows(drills):
        for i, d in enumerate(drills):
            renames.append((d.id, f"d{i + 1:02}-{slug(d.id)}"))
    projects = [e for e in plan.entries if e.kind == Kind.PROJECT]
    renumber_projects = not grows(projects)
    for i, p in enumerate(projects):
        code = f"p{i + 1:02}" if renumber_projects else p.id.split("-")[0]
        renames.append((p.id, f"{code}-{slug(p.id)}"))
        for k, s in enumerate(plan.stages(p.id)):
            renames.append((s.id, f"{code}.{k + 1}-{slug(s.id)}"))
    renames = [(old, new) for old, new in renames if old != new]
    # Two phases, so a chain like d03 -> d02 and d02 -> d01 cannot collide.
    for i, (old, _) in enumerate(renames):
        text = replace_id(text, old, f"\x01{i}\x01")
    for i, (old, new) in enumerate(renames):
        text = text.replace(f"\x01{i}\x01", new)
        fixes.append(f"renumbered {old} to {new}")
    try:
        plan = parse(text)
    except ParseError:
        return text, fixes
    trailing = text.endswith("\n")
    ls = lines(text)
    missing = [
        (u.line, default_hours(u.f.difficulty), u.id)
        for u in plan.units()
        if u.f.hours is None and u.f.difficulty is not None
    ]
    # Bottom to top, so earlier line numbers stay valid.
    for line, hours, id in reversed(missing):
        k = line
        while k < len(ls) and trim(ls[k]) and not ls[k].startswith("#"):
            k += 1
        ls.insert(k, f"hours: {float_display(hours)}")
        fixes.append(f"{id}: no `hours`, set {float_display(hours)} from its difficulty")
    out = "\n".join(ls)
    if trailing:
        out += "\n"
    return out, fixes


def problems_of(text: str) -> list[Problem]:
    """Parse problems or validation problems of a curriculum text."""
    try:
        plan = parse(text)
    except ParseError as e:
        return e.problems
    return plan.validate()


def _json(v: dict[str, object]) -> str:
    # serde_json's `json!` maps are BTreeMaps: keys in sorted order.
    return json.dumps(v, separators=(",", ":"), ensure_ascii=False, sort_keys=True)


def log_validation(course: Path, source: str, fixes: list[str], problems: list[Problem]) -> None:
    """Appends fixes and problems to `.proctor/coursecrafter/validator.log`
    (JSON lines), to see which rules planners trip on most."""
    from proctor import journal

    if not fixes and not problems:
        return
    path = course / ".proctor" / "coursecrafter" / "validator.log"
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass
    t = journal.now()
    out = ""
    for f in fixes:
        out += _json({"t": t, "source": source, "level": "fixed", "msg": f})
        out += "\n"
    for p in problems:
        level = "warning" if p.warning else "error"
        out += _json({"t": t, "source": source, "level": level, "id": p.id, "msg": p.msg})
        out += "\n"
    try:
        with open(path, "ab") as fh:
            fh.write(out.encode("utf-8"))
    except OSError:
        pass


def report(problems: list[Problem]) -> str:
    s = ""
    for p in problems:
        tag = "warning" if p.warning else "error"
        at = f"line {p.line} ({p.id})" if p.id is not None else f"line {p.line}"
        s += f"- {tag}, {at}: {p.msg}\n"
    return s


def paste_block(problems: list[Problem]) -> str:
    """A block the user can paste back into the chat that wrote the curriculum."""
    errors = [p for p in problems if not p.warning]
    return f"The PiscineBuilder importer rejected the curriculum. Fix these problems and send the complete file again, in the same format:\n\n{report(errors)}"


def difficulties(e: Entry) -> str:
    """`concept/implementation` difficulty, e.g. `2/4`."""

    def show(d: int | None) -> str:
        return str(d) if d is not None else "?"

    return f"{show(e.f.concept_difficulty)}/{show(e.f.difficulty)}"


def table(plan: Plan) -> str:
    """Compact plan table for approval."""
    from coursecrafter import llm

    rows: list[list[str]] = [["unit", "lang", "c/i", "hours", "target", "introduces", "assumes"]]
    for e in plan.entries:
        stretch = " (stretch)" if e.f.stretch else ""
        target = e.f.target if e.f.target is not None else "auto"
        match e.kind:
            case Kind.PROJECT:
                id, lang, diff, tgt = f"{e.id} (project)", plan.language(e), "", ""
            case Kind.NOTE:
                with_ = e.f.with_ if e.f.with_ is not None else ""
                id, lang, diff, tgt = f"  {e.id} (note, with {with_})", "", "", ""
            case Kind.STAGE:
                id, lang, diff, tgt = (
                    f"  {e.id}{stretch}",
                    plan.language(e),
                    difficulties(e),
                    target,
                )
            case Kind.DRILL:
                id, lang, diff, tgt = f"{e.id}{stretch}", plan.language(e), difficulties(e), target
        intro = ", ".join(e.f.teaches) if e.kind == Kind.NOTE else ", ".join(e.f.introduces)
        if e.kind == Kind.PROJECT:
            h = _sum([s.f.hours for s in plan.stages(e.id) if s.f.hours is not None])
            hours = round5(h) if h > 0.0 else ""
        else:
            hours = float_display(e.f.hours) if e.f.hours is not None else ""
        rows.append([id, lang, diff, hours, tgt, intro, ", ".join(e.f.assumes)])
    widths = [min(max(len(r[c]) for r in rows), 40 if c >= 5 else 50) for c in range(7)]
    s = ""
    for n, r in enumerate(rows):
        cells = []
        for c, v in enumerate(r):
            v = llm.cut(v, widths[c])
            cells.append(f"{v:<{widths[c]}}")
        s += trim_end("  ".join(cells))
        s += "\n"
        if n == 0:
            s += "  ".join("-" * w for w in widths)
            s += "\n"
    s += summary(plan)
    return s


def round5(h: float) -> str:
    """Hours for display, to the nearest 5 (59 -> 60, 33 -> 35, 6 -> 5):
    estimates are not precise enough for more."""
    r = _round(h / 5.0) * 5.0
    if h > 0.0 and r == 0.0:
        return "under 5"
    return _fixed0(r)


def summary(plan: Plan) -> str:
    """Workload and difficulty in two lines, under the table."""
    req = plan.required_hours()
    stretch = plan.stretch_hours()
    s = f"\nWorkload: about {round5(req)} h required"
    if stretch > 0.0:
        s += f" + {round5(stretch)} h of stretch goals"
    b = plan.budget()
    if b is not None:
        # f64::max: 1.0 when `b` is NaN.
        s += f" for a budget of {round5(b)} h ({_fixed0(req * 100.0 / (b if b >= 1.0 else 1.0))}%)"
    counts = [0] * 6
    for u in plan.units():
        if u.f.difficulty is not None:
            counts[min(u.f.difficulty, 5)] += 1
    spread = [f"{d}: {counts[d]}" for d in range(1, 6) if counts[d] > 0]
    s += (
        f"\nImplementation difficulty: {', '.join(spread)} (c/i column: concept / implementation)\n"
    )
    return s
