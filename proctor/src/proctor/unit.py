"""On-disk course format. One YAML file per unit holds both the contract
(what the instructions say) and the tests (what proctor runs), so the
two can never drift apart.

Read with `schema.load`: unknown keys are errors, as `deny_unknown_fields`
made them in the Rust tools."""

from __future__ import annotations

import enum
from dataclasses import dataclass, field, replace
from typing import Annotated, Any

from proctor.compat import WHITESPACE as _WS
from proctor.compat import lines as _lines
from proctor.schema import U8, U32, U64, Untagged


@dataclass(kw_only=True)
class LangConfig:
    """How to build, run and inspect one language. proctor ships defaults for
    common languages (see `defaults.py`); a course's `languages:` entry
    overrides any field, and unit fields win over both. Every field is optional
    so "not set" (inherit) differs from "set to empty" (replace)."""

    # Display name in the instructions.
    name: str | None = None
    # Compiler for C-family builds (default: CC, then cc, gcc, clang).
    compiler: str | None = None
    # Compiler flags, used for the program build and the function harness.
    flags: list[str] | None = None
    build: str | None = None
    run: str | None = None
    static_checks: list[StaticCheck] | None = field(default=None, metadata={"yaml": "static"})
    # Import-detection regexes (group 1 = module) for the Allowed check.
    imports: list[str] | None = None
    # Source file extensions, without the dot.
    extensions: list[str] | None = None
    # Line-comment marker (`#`, `--`, `;`...), so commented-out code
    # is ignored by the Allowed and prototype checks.
    comment: str | None = None
    env: dict[str, str] | None = None
    timeout_ms: U64 | None = None
    # Command prefix for every run of student code, e.g.
    # `valgrind --leak-check=full --error-exitcode=99`.
    wrap: str | None = None
    # Course files (relative to `.proctor/`) built together with the
    # student's code, e.g. a malloc-failure shim.
    support: list[str] | None = None

    def with_(self, over: LangConfig) -> LangConfig:
        """`self` with every field that `over` sets replaced."""
        return LangConfig(
            name=over.name if over.name is not None else self.name,
            compiler=over.compiler if over.compiler is not None else self.compiler,
            flags=over.flags if over.flags is not None else self.flags,
            build=over.build if over.build is not None else self.build,
            run=over.run if over.run is not None else self.run,
            static_checks=(
                over.static_checks if over.static_checks is not None else self.static_checks
            ),
            imports=over.imports if over.imports is not None else self.imports,
            extensions=over.extensions if over.extensions is not None else self.extensions,
            comment=over.comment if over.comment is not None else self.comment,
            env=over.env if over.env is not None else self.env,
            timeout_ms=over.timeout_ms if over.timeout_ms is not None else self.timeout_ms,
            wrap=over.wrap if over.wrap is not None else self.wrap,
            support=over.support if over.support is not None else self.support,
        )

    def copy(self) -> LangConfig:
        return replace(self)


@dataclass(kw_only=True)
class Project:
    title: str
    goal: str
    # What the project must do to count as a success.
    minimum: str | None = None
    language: str | None = None
    difficulty: U8 | None = None
    prereqs: list[str] = field(default_factory=list)
    concepts: list[str] = field(default_factory=list)


@dataclass(kw_only=True)
class Course:
    title: str
    language: str | None = None
    hours_per_week: float | None = None
    weeks: float | None = None
    # How thoroughly the hidden tests probe: relaxed, standard or strict
    # (the default). Stated in RULES.md.
    rigor: str | None = None
    # Course-wide habits the units rely on, quoted in RULES.md.
    conventions: str | None = None
    # Unit ids in teaching order. Project stages appear in place.
    units: list[str]
    # Sorted by id, as a BTreeMap is.
    projects: dict[str, Project] = field(default_factory=dict)
    # Per-language defaults inherited by every unit of that language.
    languages: dict[str, LangConfig] = field(default_factory=dict)


class Target(enum.Enum):
    # proctor loads the functions and calls them from its own main.
    FUNCTION = "function"
    # proctor runs the student's program as a whole.
    PROGRAM = "program"


class Scaffold(enum.Enum):
    NONE = "none"
    MINIMAL = "minimal"
    FULL = "full"


@dataclass(kw_only=True)
class Func:
    name: str
    sig: str
    returns: str = ""


class Rule(enum.Enum):
    REQUIRED = "required"
    ONE_OR_MORE = "one_or_more"
    FREE = "free"
    FORBIDDEN = "forbidden"


@dataclass(kw_only=True)
class FileRule:
    path: str
    rule: Rule = Rule.REQUIRED
    note: str = ""


# Requirements: markdown text, or a list of bullets (untagged in YAML).
Requirements = Annotated[str | list[str], Untagged("Requirements")]


def requirements_is_empty(r: str | list[str]) -> bool:
    if isinstance(r, str):
        return r.strip(_WS) == ""
    return all(x.strip(_WS) == "" for x in r)


def requirements_markdown(r: str | list[str]) -> str:
    """The whole section as markdown."""
    if isinstance(r, str):
        return r.strip(_WS) + "\n"
    return "".join(f"- {x.strip(_WS)}\n" for x in r)


@dataclass(kw_only=True)
class StaticCheck:
    name: str
    cmd: str


@dataclass(kw_only=True)
class FuzzSpec:
    # Function target: the function to compare against the reference.
    func: str | None = field(default=None, metadata={"yaml": "fn"})
    # Program target: a name for the test.
    name: str | None = None
    # Function target: generator per parameter name (overrides defaults).
    args: dict[str, Any] = field(default_factory=dict)
    # Program target: generator for the argument list (list or string).
    argv: Any = None
    # Program target: generator for stdin.
    stdin: Any = None
    rounds: U32 | None = None


# A fuzz entry: a function name, or a full spec (untagged in YAML).
Fuzz = Annotated[str | FuzzSpec, Untagged("Fuzz")]


@dataclass(kw_only=True)
class Law:
    name: str | None = None
    vars: dict[str, Any] = field(default_factory=dict, metadata={"yaml": "gen"})
    check: list[str] = field(default_factory=list)
    # `[f, g]`: g(f(x, ...)) == x
    roundtrip: list[str] | None = None
    # `f`: f(f(x)) == f(x)
    idempotent: str | None = None
    # `[f, g]`: f(args) == g(args)
    agree: list[str] | None = None
    rounds: U32 | None = None
    # Per-round time limit, for performance laws.
    time_ms: U64 | None = None


@dataclass(kw_only=True)
class Random:
    rounds: U32 | None = None
    fuzz: list[Fuzz] = field(default_factory=list)
    laws: list[Law] = field(default_factory=list)


@dataclass(kw_only=True)
class Unit:
    id: str
    title: str
    # Set for project stages: the project id (also the workspace name).
    project: str | None = None
    language: str | None = None
    difficulty: U8 | None = None
    prereqs: list[str] = field(default_factory=list)
    concepts: list[str] = field(default_factory=list)
    introduces: list[str] = field(default_factory=list)
    assumes: list[str] = field(default_factory=list)
    # Note files (in `.proctor/notes/`) revealed with this unit.
    notes: list[str] = field(default_factory=list)
    # How hard the ideas are to understand (`difficulty` is implementation).
    concept_difficulty: U8 | None = None
    # Planned hours, for the schedule in PROGRESS.md. Never shown in instructions.
    hours: float | None = None
    # Optional unit: the student may skip it with `proctor skip`.
    stretch: bool = False
    # `linux`: needs Linux APIs; on other systems proctor builds, runs
    # and tests it in a Linux container (Docker). Absent: any system.
    platform: str | None = None
    scaffold: Scaffold = Scaffold.MINIMAL
    target: Target = Target.FUNCTION
    goal: str
    # Markdown shown right after the goal: a step-by-step walkthrough for
    # guided units (first contact with a language or a tool).
    lesson: str | None = None
    # The lesson is a step-by-step walkthrough (first contact with a language).
    guided: bool = False
    # Reading only (a project's introduction): nothing to hand in, `check` passes once unlocked.
    reading: bool = False
    # Program target: what a run prints, shown when no test case gives an
    # exact output to use as an example.
    example: str | None = None
    # Edge-case categories worth deciding about, shown without answers.
    think_about: list[str] = field(default_factory=list)
    functions: list[Func] = field(default_factory=list)
    files: list[FileRule] = field(default_factory=list)
    # Only what the functions table, Allowed, the examples and RULES.md
    # cannot say: markdown text, or a list of bullets.
    requirements: Requirements = field(default_factory=list)
    # Absent = unrestricted. Present = strict whitelist (built-ins implied).
    allowed: list[str] | None = None
    # Items the exercise's code may not use even though `allowed` covers
    # them (`std` allowed, `std::mem::swap` forbidden). Prefix match; the
    # student's tests in `tests/` are not checked.
    forbidden: list[str] = field(default_factory=list)
    environment: str | None = None
    env: dict[str, str] = field(default_factory=dict)
    # Python: default module for undotted function names.
    module: str | None = None
    # Where `proctor next` writes empty mandatory functions.
    stubs: str | None = None
    # Program target: how to run the program ({exe}, {python}, {cargo_bin}).
    run: str | None = None
    # Overrides the preset build command.
    build: str | None = None
    static_checks: list[StaticCheck] = field(default_factory=list, metadata={"yaml": "static"})
    # Extra import-detection regexes (group 1 = module name), for languages
    # without a built-in scanner.
    imports: list[str] = field(default_factory=list)
    # Test-only functions provided by support files (never required from
    # the student, never shown), e.g. a switch that makes malloc fail.
    helpers: list[Func] = field(default_factory=list)
    # Overrides the language's wrap for this unit.
    wrap: str | None = None
    # Added to the language's `support` files.
    support: list[str] = field(default_factory=list)
    # Appended to the language's compiler flags for this unit, e.g.
    # `-Dmalloc=sc_malloc` to route allocations through a support shim.
    extra_flags: list[str] = field(default_factory=list)
    cases: str | None = None
    # Rust: `#[test]` functions compiled against the student's library
    # crate, which they reach as `student` (see `rtests`).
    rust_tests: str | None = None
    random: Random = field(default_factory=Random)
    timeout_ms: U64 | None = None

    def is_stage(self) -> bool:
        return self.project is not None

    def case_lines(self) -> list[tuple[int, str]]:
        """Non-empty, non-comment lines of `cases`, with their 1-based line numbers."""
        out: list[tuple[int, str]] = []
        for i, line in enumerate(_lines(self.cases or "")):
            t = line.strip(_WS)
            if t and not t.startswith("#"):
                out.append((i + 1, t))
        return out

    def has_rust_tests(self) -> bool:
        return self.rust_tests is not None and self.rust_tests.strip(_WS) != ""

    def has_tests(self) -> bool:
        return (
            bool(self.case_lines())
            or self.has_rust_tests()
            or bool(self.random.fuzz)
            or bool(self.random.laws)
        )
