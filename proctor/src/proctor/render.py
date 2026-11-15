"""Renders instruction files from unit records, in one fixed design. The
markdown is never written by an LLM: it is derived from the same record
proctor grades against, at the moment the unit is revealed."""

from __future__ import annotations

from proctor import compat, course, engine, program, rules
from proctor.compat import float_display, is_whitespace, trim, trim_end
from proctor.errors import Error
from proctor.unit import Rule, Target, Unit, requirements_is_empty, requirements_markdown


def display_language(c: course.CourseDir, lang: str) -> str:
    """The course's display name for a language if it gives one, else the preset name."""
    l = c.course.languages.get(lang)  # noqa: E741
    if l is not None and l.name is not None:
        return l.name
    return language_name(lang)


def language_name(lang: str) -> str:
    match lang:
        case "c":
            return "C"
        case "cpp" | "c++":
            return "C++"
        case "python":
            return "Python 3"
        case "rust":
            return "Rust"
        case "bash" | "sh" | "shell":
            return "Bash"
        case "javascript" | "js":
            return "JavaScript"
        case "typescript" | "ts":
            return "TypeScript"
        case "go":
            return "Go"
        case "java":
            return "Java"
        case "ruby":
            return "Ruby"
        case other:
            if not other:
                return ""
            return other[0].upper() + other[1:]


def cell(s: str) -> str:
    return trim(s).replace("|", "\\|").replace("\n", " ")


def label(u: Unit) -> str:
    return u.id.split("-")[0]


def header_table(rows: list[tuple[str, str]]) -> str:
    s = "| Field | Value |\n|-------|-------|\n"
    for k, v in rows:
        if v:
            s += f"| {k} | {cell(v)} |\n"
    return s


def list_or_none(v: list[str]) -> str:
    if not v:
        return "none"
    return ", ".join(v)


def code_list(v: list[str]) -> str:
    return ", ".join(f"`{x}`" for x in v)


def unit_md(c: course.CourseDir, u: Unit) -> str:
    lang = c.language(u)
    chain = c.chain(u)
    s = f"# {label(u)}: {trim(u.title)}\n\n"
    found = c.project(u)
    if found is not None:
        pid, p = found
        k = len(chain)
        if k == 1:
            builds_on = "First stage."
        elif k == 2:
            builds_on = f"Builds on {label(chain[0])}."
        else:
            builds_on = f"Builds on {label(chain[0])} to {label(chain[k - 2])}."
        code = pid.split("-")[0]
        overview = str(c.overview_rel(pid)).replace("\\", "/")
        s += f"Part of project {code} ({p.title}). {builds_on} See `{overview}`.\n\n"
    if u.concept_difficulty is not None and u.difficulty is not None:
        difficulty = f"concepts {u.concept_difficulty} / 5, implementation {u.difficulty} / 5"
    elif u.concept_difficulty is None and u.difficulty is not None:
        difficulty = f"{u.difficulty} / 5"
    else:
        difficulty = ""
    s += header_table(
        [
            ("Language", display_language(c, lang)),
            ("Difficulty", difficulty),
            ("Prerequisites", "" if u.is_stage() else list_or_none(u.prereqs)),
            ("Concepts", ", ".join(u.concepts)),
            (
                "Status",
                "Stretch goal: optional, `proctor skip` moves on without it" if u.stretch else "",
            ),
        ]
    )
    s += f"\n## Goal\n\n{trim(u.goal)}\n"
    lesson = u.lesson
    if lesson is not None and trim(lesson) != "":
        heading = "Walkthrough" if u.guided else "Before you start"
        s += f"\n## {heading}\n\n{trim(lesson)}\n"

    if u.functions:
        s += (
            "\n## Expected functions (minimal)\n\n"
            "| Name | Signature | Returns |\n|------|-----------|---------|\n"
        )
        for f in u.functions:
            s += f"| `{cell(f.name)}` | `{cell(f.sig)}` | {cell(f.returns)} |\n"
        s += "\nThis is a minimum: add any helper you need."
        if len(chain) > 1 and any(x.functions for x in chain[:-1]):
            s += " Everything required by the earlier stages stays required."
        s += "\n"

    if u.files:
        # With `**` free, other free entries add nothing: they are left out.
        all_free = any(f.rule == Rule.FREE and f.path == "**" for f in u.files)
        s += "\n## Expected files\n\n| Path | Rule | Notes |\n|------|------|-------|\n"
        for fr in u.files:
            if all_free and fr.rule == Rule.FREE and fr.path != "**":
                continue
            match fr.rule:
                case Rule.REQUIRED:
                    rule = "required"
                case Rule.ONE_OR_MORE:
                    rule = "one or more"
                case Rule.FREE:
                    rule = "free"
                case Rule.FORBIDDEN:
                    rule = "forbidden"
            path = "anything else" if fr.path == "**" else f"`{cell(fr.path)}`"
            s += f"| {path} | {rule} | {cell(fr.note)} |\n"
        if not all_free:
            s += "| anything else | not allowed | build output is ignored |\n"

    if not requirements_is_empty(u.requirements):
        s += "\n## Requirements\n\n"
        s += requirements_markdown(u.requirements)

    examples = examples_md(c, u)
    if examples:
        s += f"\n## Examples\n\n{examples}"
    if u.think_about:
        s += "\n### Think about\n\n"
        for t in u.think_about:
            s += f"- {trim(t)}\n"

    s += "\n## Allowed\n\n"
    allowed = c.allowed(u)
    if allowed is None:
        s += "No restriction: use whatever you need.\n"
    elif not allowed:
        s += "Only your own code and the language's built-ins.\n"
    else:
        s += code_list(allowed)
        s += "\n"
    if u.forbidden:
        s += "\n**Forbidden even so:** "
        s += code_list(u.forbidden)
        s += "\n"

    if u.environment is not None or u.env:
        s += "\n## Environment\n\n"
        if u.environment is not None:
            s += trim(u.environment)
            s += "\n"
        if u.env:
            if u.environment is not None:
                s += "\n"
            s += "proctor sets these variables when it builds and runs your work:\n\n"
            for k2, v in sorted(u.env.items()):
                s += f"- `{k2}={v}`\n"

    s += "\n## Checking your work\n\n"
    s += checking(c, u, lang)
    return s


def rules_link(c: course.CourseDir, u: Unit) -> str:
    """Path from a unit's instructions to the course's RULES.md."""
    depth = max(len(c.subject_rel(u).parts) - 1, 0)
    return "../" * depth + rules.FILE


def checking(c: course.CourseDir, u: Unit, lang: str) -> str:
    """What is specific to grading this unit; the rest is in RULES.md."""
    chain = c.chain(u)
    lines: list[str] = []
    match u.target:
        case Target.FUNCTION:
            match lang:
                case "python":
                    how = 'proctor imports your code and calls them itself; keep scripts behind `if __name__ == "__main__":`.'
                case "rust":
                    how = "proctor calls them from your library crate (`src/lib.rs`), where they must be `pub`; a `main` is not run."
                case "c":
                    how = "proctor compiles your files with its own `main` and calls them; yours is never run."
                case _:
                    how = "proctor calls them itself; your `main`, if any, is not run."
            lines.append(f"**Graded: your functions.** {how}")
        case Target.PROGRAM:
            line = "**Graded: your program as a whole:** its output and its exit status."
            r = c.run_cmd(u)
            if r is not None and "{" not in r:
                line += f" It is run as `{r}`."
            lines.append(line)
    if len(chain) > 1 and any(x.has_tests() for x in chain[:-1]):
        lines.append(
            f"**Regression:** the tests of {label(chain[0])} to {label(chain[len(chain) - 2])} run again, so earlier behaviour must keep working."
        )
    if u.random.fuzz or u.random.laws:
        rounds = u.random.rounds if u.random.rounds is not None else engine.DEFAULT_ROUNDS
        lines.append(
            f"**Randomized tests:** about {rounds} generated inputs each, compared with a hidden reference or checked against invariants."
        )
    w = c.wrap(u)
    if w is not None:
        lines.append(
            f"Every run goes through `{w}`: anything it reports (memory errors, leaks) fails the run."
        )
    ms = c.timeout_ms(u)
    if ms is not None and ms != engine.DEFAULT_TIMEOUT_MS:
        lines.append(f"**Time limit:** {float_display(float(ms) / 1000.0)} s per call or run.")
    ws = c.workspace_name(u)
    is_last_stage = False
    if u.is_stage():
        stages = c.stages_of(u.project or "")
        is_last_stage = bool(stages) and stages[-1].id == u.id
    if is_last_stage:
        next_ = "the project is complete"
    elif u.is_stage():
        next_ = "proctor offers the next stage"
    else:
        next_ = "proctor offers the next exercise"
    lines.append(f"Work in `{ws}/` and run `proctor check` until it prints `OK`; then {next_}.")
    s = "".join(f"- {line}\n" for line in lines)
    s += f"\nThis course is graded at **{rules.rigor(c)}** rigor: edge cases, exact output, linters and time limits are in [`{rules.FILE}`]({rules_link(c, u)}).\n"
    return s


def split_eq(line: str) -> tuple[str, str] | None:
    """Splits `lhs == rhs` at its only top-level `==` (outside strings and brackets)."""
    depth = 0
    quote: str | None = None
    at: int | None = None
    i = 0
    while i < len(line):
        ch = line[i]
        if quote is not None:
            if ch == "\\":
                i += 1
            elif ch == quote:
                quote = None
        elif ch == '"' or ch == "'":
            quote = ch
        elif ch in "([{":
            depth += 1
        elif ch in ")]}":
            depth -= 1
        elif ch == "=" and depth == 0 and line[i + 1 : i + 2] == "=":
            if at is not None:
                return None
            at = i
            i += 1
        elif ch in "&|" and depth == 0 and line[i + 1 : i + 2] == ch:
            return None
        i += 1
    if at is None:
        return None
    return (trim(line[:at]), trim(line[at + 2 :]))


def _eq_ignore_ascii_case(a: str, b: str) -> bool:
    """Rust `str::eq_ignore_ascii_case`."""

    def low(s: str) -> str:
        return "".join(chr(ord(c) + 32) if "A" <= c <= "Z" else c for c in s)

    return low(a) == low(b)


def marked_cases(u: Unit) -> list[tuple[str, bool]]:
    """Case lines and whether a `# example` comment line marks them."""
    out: list[tuple[str, bool]] = []
    mark = False
    for l in compat.lines(u.cases or ""):  # noqa: E741
        t = trim(l)
        if not t:
            continue
        if t.startswith("#"):
            mark = _eq_ignore_ascii_case(trim(t[1:]), "example")
            continue
        out.append((t, mark))
        mark = False
    return out


def looks_empty(s: str) -> bool:
    """Inputs too small to show: they belong to the edge cases the student finds."""
    return any(e in s for e in ['""', "''", "[]", "{}", "vec![]", "String::new()"])


def in_place(sig: str) -> bool:
    """A function that returns nothing and changes an argument (`&mut`, `char *`):
    the harness evaluates the call to that argument."""
    s = trim(sig)
    if s.startswith("fn ") or s.startswith("pub fn "):
        no_return = "->" not in s
    else:
        no_return = s.startswith("void ")
    return no_return and ("&mut" in s or ("char" in s and "*" in s and "const" not in s))


def _insert(seen: set[str], x: str) -> bool:
    """`BTreeSet::insert`: true when `x` was not there yet."""
    if x in seen:
        return False
    seen.add(x)
    return True


def examples_md(c: course.CourseDir, u: Unit) -> str:
    """Examples built from the unit's own test cases, so they cannot disagree
    with the grading: the cases marked `# example`, else the first ordinary
    case of each function (or the first runs with an exact output)."""
    cases = marked_cases(u)
    any_marked = any(m for _, m in cases)
    match u.target:
        case Target.FUNCTION:

            def short(n: str) -> str:
                return n.rsplit(".", 1)[-1]

            rows: list[tuple[str, str, bool]] = []
            seen: set[str] = set()
            for line, marked in cases:
                split = split_eq(line)
                if split is None:
                    continue
                lhs, rhs = split
                open_ = lhs.find("(")
                if open_ < 0:
                    continue
                name = trim(lhs[:open_])
                f = next(
                    (f for f in u.functions if f.name == name or short(f.name) == short(name)),
                    None,
                )
                if any_marked:
                    ok = marked
                else:
                    ok = (
                        "raises(" not in lhs
                        and not looks_empty(lhs)
                        and not looks_empty(rhs)
                        and lhs.endswith(")")
                        and (f is not None or not u.functions)
                        and _insert(seen, name)
                    )
                if ok:
                    rows.append((lhs, rhs, f is not None and in_place(f.sig)))
                if len(rows) >= 8:
                    break
            if not rows:
                return ""
            s = "| Call | Result |\n|------|--------|\n"
            for call, result, inplace in rows:
                if inplace:
                    shown_result = f"the argument becomes `{cell(result)}`"
                else:
                    shown_result = f"`{cell(result)}`"
                s += f"| `{cell(call)}` | {shown_result} |\n"
            return s
        case Target.PROGRAM:
            if c.language(u) == "rust":
                cmd = "cargo run --quiet --"
            else:
                r = c.run_cmd(u)
                cmd = r if r is not None and "{" not in r else "./program"
            shown: list[str] = []
            for line, marked in cases:
                if any_marked and not marked:
                    continue
                try:
                    case = program.parse_case(line)
                except Error:
                    continue
                out = next(
                    (
                        a.text
                        for a in case.asserts
                        if isinstance(a, program.AssertEq) and a.stream == program.Stream.OUT
                    ),
                    None,
                )
                if out is None or out == "":
                    continue
                if not marked and (case.files or case.stdin == ""):
                    continue
                exit = next(
                    (a.code for a in case.asserts if isinstance(a, program.AssertExit)), None
                )
                args = [
                    f'"{a}"' if a == "" or any(is_whitespace(ch) for ch in a) else a
                    for a in case.args
                ]
                run = cmd if not args else f"{cmd} {' '.join(args)}"
                s = f"Running `{run}`"
                for name, body in case.files:
                    s += f" with a file `{name}` holding `{cell(body)}`"
                if case.stdin is not None:
                    given = case.stdin.rstrip("\n")
                    s += f" with this input:\n\n```text\n{given}\n```\n\nprints:\n"
                else:
                    s += " prints:\n"
                printed = out.rstrip("\n")
                s += f"\n```text\n{printed}\n```\n"
                if exit is not None and exit != 0:
                    s += f"\nand exits with status `{exit}`.\n"
                shown.append(s)
                if len(shown) >= 2:
                    break
            if not shown:
                ex = u.example
                if ex is not None and trim(ex) != "":
                    return f"```text\n{trim_end(ex)}\n```\n"
                return ""
            return "\n".join(shown)
    raise AssertionError(u.target)


def overview_md(c: course.CourseDir, pid: str) -> str:
    p = c.course.projects.get(pid)
    if p is None:
        return ""
    code = pid.split("-")[0]
    stages = c.stages_of(pid)
    if stages:
        lang = c.language(stages[0])
    elif p.language is not None:
        lang = p.language
    else:
        lang = ""
    s = f"# {code}: {trim(p.title)}\n\n"
    s += header_table(
        [
            ("Language", display_language(c, lang)),
            ("Difficulty", f"{p.difficulty} / 5" if p.difficulty is not None else ""),
            ("Prerequisites", list_or_none(p.prereqs)),
            ("Concepts", ", ".join(p.concepts)),
        ]
    )
    s += f"\n## Goal\n\n{trim(p.goal)}\n"
    m = p.minimum
    if m is not None and trim(m) != "":
        s += f"\n## Minimum viable result\n\n{trim(m)}\n\nThe required stages deliver this. Stretch stages add to it, only once it is stable.\n"
    s += "\n## Stages\n\n"
    for u in stages:
        stretch = " (stretch goal)" if u.stretch else ""
        s += f"- {label(u)} {trim(u.title)}: {first_sentence(u.goal)}{stretch}\n"
    s += (
        f"\n## How a project works\n\nYou keep one workspace, `{pid}/`, from the first stage to the last. A stage's\n"
        "instructions appear only once the previous stage passes, and each stage adds to the\n"
        "same workspace: proctor creates missing files and stubs and never touches a file\n"
        "you already wrote.\n\n"
        "Each stage's check also re-runs the tests of the earlier stages. The Allowed list of a\n"
        "stage always includes the previous ones.\n\n"
        f"## Submission\n\nWork in `{pid}/` and run `proctor check` from there. When it prints `OK`,\n"
        "proctor offers the next stage. The course's rules are in [`RULES.md`](../../RULES.md).\n"
    )
    return s


def first_sentence(s: str) -> str:
    t = trim(s)
    i = t.find(". ")
    if i >= 0:
        return t[: i + 1]
    return t
