"""`RULES.md`: what every unit of a course expects without saying it. It is
written from a template and the course's data (never by an LLM), so it
states exactly what proctor checks."""

from __future__ import annotations

from proctor import course, engine, render, run
from proctor.compat import trim

FILE = "RULES.md"


def rigor(c: course.CourseDir) -> str:
    """The course's rigor level: `relaxed`, `standard` or `strict` (the default)."""
    r = c.course.rigor
    match trim(r) if r is not None else None:
        case "relaxed":
            return "relaxed"
        case "standard":
            return "standard"
        case _:
            return "strict"


RELAXED = "Hidden tests check the behaviour the units state and the cases their examples show. Random inputs stay within the ranges the units state."

STANDARD = """Beyond what a unit states, hidden tests cover:

- **the usual edge cases**: empty input, a single element, zero, a missing trailing newline;
- **invalid input** the unit describes, handled without crashing;
- **random input**: a few hundred generated cases per function or program, checked against properties the result must keep (the seed `proctor check` prints replays them)."""

STRICT = """You have unlimited attempts, the lessons and the documentation: the work is expected to be complete, not merely to pass the examples. Beyond what a unit states, hidden tests cover:

- **every edge case**: empty input, a single element, zero, the limits of the types involved, text outside ASCII, a missing trailing newline;
- **adversarial input**: very long text or large counts, malformed input; a program never crashes, panics or hangs;
- **random input**: a few hundred generated cases per function or program, checked against properties the result must keep (the seed `proctor check` prints replays them);
- **performance**: a reasonable algorithm is expected; one that is needlessly slow on large input fails on the time limit."""


def rules_md(c: course.CourseDir) -> str:
    level = rigor(c)
    s = f"# Rules of {trim(c.course.title)}\n\nThese rules hold for every unit. A unit's instructions say what is specific to it; when they are silent, this page applies.\n"
    match level:
        case "relaxed":
            text = RELAXED
        case "standard":
            text = STANDARD
        case _:
            text = STRICT
    s += f"\n## Rigor: {level}\n\n{text}\n"
    s += "\nEvery hidden test follows from a unit's instructions, its examples or this page: nothing is graded that you could not have known. Each unit's **Think about** list points at the cases worth deciding about, without the answers.\n"
    s += "\n## When the instructions are silent\n\n- Do what the obvious reading, or the standard tool of the same name, would do.\n- Never crash, panic or hang, whatever the input.\n- Report errors on standard error and, for a program, exit with a non-zero status.\n"
    s += "\n## Output\n\n- Output is compared **exactly**: every character, space and newline counts.\n- Print nothing that was not asked for, on standard output or standard error.\n- A line of output ends with a newline, the last one included.\n"

    # Code quality, per language actually used.
    langs: set[str] = set()
    quality: list[str] = []
    for u in c.units:
        lang = c.language(u)
        if lang in langs:
            continue
        langs.add(lang)
        parts: list[str] = []
        flags = c.lang_config(u).flags
        if flags:
            werror = any(f == "-Werror" for f in flags)
            fails = ", so a warning fails the build" if werror else ""
            parts.append(f"compiled with `{' '.join(flags)}`{fails}")
        statics = [f"`{x.cmd}`" for x in c.static_checks(u)]
        if statics:
            parts.append(f"{' and '.join(statics)} must pass")
        if parts:
            quality.append(f"- **{render.display_language(c, lang)}**: {'; '.join(parts)}.")
    if quality:
        s += "\n## Code quality\n\n"
        s += "\n".join(quality)
        s += "\n\nRun them yourself before `proctor check`: their warnings are errors here.\n"

    secs = float(engine.DEFAULT_TIMEOUT_MS) / 1000.0
    mb = run.MAX_OUTPUT >> 20
    s += f"\n## Limits\n\n- Each call or run is stopped after {secs:.0f} s unless a unit says otherwise; a hang or a crash fails the test.\n- A run that writes more than {mb} MB of output is stopped and fails the test.\n- **Allowed** lists what your code may use besides your own code and the language's built-ins. An entry covers everything inside it; **Forbidden even so** removes items from it. Your own tests in `tests/` may use anything.\n- Randomized tests use a new seed on every run. A failure prints the seed, and `proctor check --seed <seed>` replays it.\n"
    if c.course.projects:
        s += "\n## Projects\n\nA project's stages share one workspace. Each stage's check re-runs the tests of every earlier stage, so earlier behaviour must keep working.\n"
    s += "\n## When a check fails\n\n`proctor check` stops at the first failing step and prints what it expected and what it got. `proctor check --verbose` shows every test, and `proctor explain` shows what the tests generate and how many rounds they run.\n"
    conv = c.course.conventions
    if conv is not None and trim(conv) != "":
        s += f"\n## Conventions of this course\n\n{trim(conv)}\n"
    s += "\n## Fair play\n\nDocumentation, books, forums and search engines are fine. AI assistants are not: the point is that you can do it.\n"
    return s
