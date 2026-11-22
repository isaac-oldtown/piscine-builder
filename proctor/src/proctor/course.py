"""A course on disk: `<root>/.proctor/` holds everything hidden; the
student sees `subjects/`, `notes/`, `PROGRESS.md` and their workspaces."""

from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, TypeVar

from proctor import defaults, gens, journal, schema
from proctor.compat import io_error, read_text, trim
from proctor.errors import Error
from proctor.unit import Course, LangConfig, Project, StaticCheck, Unit

T = TypeVar("T")

HIDDEN = ".proctor"


@dataclass(kw_only=True)
class CourseDir:
    root: Path
    course: Course
    units: list[Unit]
    lib: gens.Lib

    @staticmethod
    def discover(start: Path) -> CourseDir:
        """Nearest ancestor of `start` (inclusive) holding `.proctor/course.yaml`."""
        try:
            start = _canonicalize(Path(start))
        except OSError as e:
            raise Error(f"{start}: {io_error(e)}") from None
        root = next(
            (d for d in (start, *start.parents) if (d / HIDDEN / "course.yaml").is_file()),
            None,
        )
        if root is None:
            raise Error(
                "not inside a PiscineBuilder course (no .proctor/course.yaml above this directory)"
            )
        return CourseDir.load(root)

    @staticmethod
    def load(root: Path) -> CourseDir:
        hidden = root / HIDDEN
        course = read_yaml(Course, hidden / "course.yaml")
        units: list[Unit] = []
        seen: set[str] = set()
        for id in course.units:
            if id in seen:
                raise Error(f"course.yaml lists `{id}` twice")
            seen.add(id)
            path = hidden / "units" / f"{id}.yaml"
            unit = read_yaml(Unit, path)
            if unit.id != id:
                raise Error(f"{path}: id is `{unit.id}`, expected `{id}`")
            p = unit.project
            if p is not None and p not in course.projects:
                raise Error(f"unit `{id}` belongs to unknown project `{p}`")
            units.append(unit)
        lib_path = hidden / "lib.yaml"
        lib: dict[str, Any] = read_yaml(dict[str, Any], lib_path) if lib_path.is_file() else {}
        return CourseDir(root=strip_verbatim(root), course=course, units=units, lib=lib)

    def hidden(self) -> Path:
        return self.root / HIDDEN

    def unit(self, id: str) -> Unit | None:
        return next((u for u in self.units if u.id == id), None)

    def index(self, id: str) -> int | None:
        return next((i for i, u in enumerate(self.units) if u.id == id), None)

    def project(self, u: Unit) -> tuple[str, Project] | None:
        p = u.project
        if p is None or p not in self.course.projects:
            return None
        return (p, self.course.projects[p])

    def language(self, u: Unit) -> str:
        lang = u.language
        if lang is None:
            pr = self.project(u)
            lang = pr[1].language if pr is not None else None
        if lang is None:
            lang = self.course.language
        return (lang if lang is not None else "generic").lower()

    def lang_config(self, u: Unit) -> LangConfig:
        """Built-in preset for the unit's language, overridden by the course's
        `languages:` entry."""
        lang = self.language(u)
        base = defaults.preset(lang)
        over = self.course.languages.get(lang)
        if over is None:
            over = self.course.languages.get(defaults.canonical(lang))
        return base.with_(over) if over is not None else base

    def run_cmd(self, u: Unit) -> str | None:
        """Program command: the latest stage that sets `run:`, else the language's."""
        run = next((x.run for x in reversed(self.chain(u)) if x.run is not None), None)
        return run if run is not None else self.lang_config(u).run

    def build_cmd(self, u: Unit) -> str | None:
        build = next((x.build for x in reversed(self.chain(u)) if x.build is not None), None)
        return build if build is not None else self.lang_config(u).build

    def env(self, u: Unit) -> dict[str, str]:
        """Language env first, then each stage's own variables."""
        env = dict(self.lang_config(u).env or {})
        for x in self.chain(u):
            env.update(x.env)
        return dict(sorted(env.items()))

    def static_checks(self, u: Unit) -> list[StaticCheck]:
        """Language static checks, then the stages' (same name = replaced)."""
        out = list(self.lang_config(u).static_checks or [])
        for x in self.chain(u):
            for s in x.static_checks:
                out = [o for o in out if o.name != s.name]
                out.append(s)
        return out

    def import_patterns(self, u: Unit) -> list[str]:
        out = list(self.lang_config(u).imports or [])
        for x in self.chain(u):
            out.extend(x.imports)
        return out

    def timeout_ms(self, u: Unit) -> int | None:
        return u.timeout_ms if u.timeout_ms is not None else self.lang_config(u).timeout_ms

    def wrap(self, u: Unit) -> str | None:
        """Run prefix: the latest stage's `wrap`, else the language's."""
        w = next((x.wrap for x in reversed(self.chain(u)) if x.wrap is not None), None)
        if w is None:
            w = self.lang_config(u).wrap
        return w if w is not None and trim(w) != "" else None

    def support(self, u: Unit) -> list[Path]:
        """Support files (absolute), language ones then each stage's."""
        rel = list(self.lang_config(u).support or [])
        for x in self.chain(u):
            rel.extend(x.support)
        out: list[Path] = []
        for r in rel:
            p = self.hidden() / r
            if p not in out:
                out.append(p)
        return out

    def workspace_name(self, u: Unit) -> str:
        return u.project if u.project is not None else u.id

    def workspace(self, u: Unit) -> Path:
        return self.root / self.workspace_name(u)

    def chain(self, u: Unit) -> list[Unit]:
        """For a stage: every stage of its project up to and including it.
        For a drill: just the drill."""
        p = u.project
        if p is None:
            return [u]
        out: list[Unit] = []
        for x in self.units:
            if x.project == p:
                out.append(x)
            if x.id == u.id:
                break
        return out

    def allowed(self, u: Unit) -> list[str] | None:
        """What `u`'s code may use: the `allowed` entries of `u` and of the
        earlier stages of its project, in order; `None` when none of them
        restricts anything. A stage lists only what it adds."""
        chain = self.chain(u)
        if all(x.allowed is None for x in chain):
            return None
        out: list[str] = []
        for x in chain:
            for a in x.allowed or []:
                if a not in out:
                    out.append(a)
        return out

    def stages_of(self, project: str) -> list[Unit]:
        return [u for u in self.units if u.project == project]

    def subject_rel(self, u: Unit) -> Path:
        if u.project is None:
            return Path("subjects") / f"{u.id}.md"
        return Path("subjects") / u.project / f"{u.id}.md"

    def overview_rel(self, project: str) -> Path:
        return Path("subjects") / project / "00-overview.md"

    def state(self) -> State:
        return State.from_events(journal.read(self.root), self)


def read_yaml(tp: type[T], path: Path) -> T:
    try:
        text = read_text(path)
    except OSError as e:
        raise Error(f"{path}: {io_error(e)}") from None
    try:
        return schema.load(tp, text)
    except Error as e:
        raise Error(f"{path}: {e.msg}") from None


def _canonicalize(p: Path) -> Path:
    """Rust `fs::canonicalize`: on Windows, a verbatim path (`\\\\?\\C:\\x`,
    `\\\\?\\UNC\\server\\share`), as Rust returns it and `Path.resolve` does not.
    Paths derived from it keep the prefix in messages, as in Rust."""
    r = p.resolve(strict=True)
    if sys.platform != "win32":
        return r
    s = str(r)
    if s.startswith("\\\\?\\"):
        return r
    if s.startswith("\\\\"):
        return Path("\\\\?\\UNC\\" + s[2:])
    return Path("\\\\?\\" + s)


def strip_verbatim(p: Path) -> Path:
    """`\\\\?\\C:\\x` -> `C:\\x`, so paths print and pass to tools cleanly."""
    s = str(p)
    if s.startswith("\\\\?\\"):
        rest = s[4:]
        if not rest.startswith("UNC"):
            return Path(rest)
    return Path(p)


@dataclass(kw_only=True)
class State:
    unlocked: set[str]
    passed: set[str]
    # Stretch units the student chose to skip.
    skipped: set[str]
    # First unlocked unit neither passed nor skipped, else the last unlocked.
    current: str | None

    @staticmethod
    def from_events(events: list[journal.Event], c: CourseDir) -> State:
        unlocked: set[str] = set()
        passed: set[str] = set()
        skipped: set[str] = set()
        for e in events:
            match e.ev:
                case "unlock":
                    unlocked.add(e.unit)
                case "pass":
                    passed.add(e.unit)
                case "skip":
                    skipped.add(e.unit)
                case _:
                    pass
        cur = next(
            (u for u in c.units if u.id in unlocked and u.id not in passed and u.id not in skipped),
            None,
        )
        if cur is None:
            cur = next((u for u in reversed(c.units) if u.id in unlocked), None)
        return State(
            unlocked=unlocked,
            passed=passed,
            skipped=skipped,
            current=cur.id if cur is not None else None,
        )

    def done(self, id: str) -> bool:
        """Passed, or skipped as a stretch goal."""
        return id in self.passed or id in self.skipped

    def next_locked(self, c: CourseDir) -> Unit | None:
        """The unit `next` would unlock, if the current one is done."""
        return next((u for u in c.units if u.id not in self.unlocked), None)
