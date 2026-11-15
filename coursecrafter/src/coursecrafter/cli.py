"""The `coursecrafter` command: creates PiscineBuilder courses. The only part
of PiscineBuilder that uses an LLM."""

from __future__ import annotations

import os
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from proctor.command import Command
from proctor.compat import (
    U64_MASK,
    io_error,
    is_terminal,
    lines,
    parse_uint,
    read_text,
    trim,
    write_text,
)
from proctor.errors import Error

from coursecrafter import build, config, curriculum, env, guided, linux, llm, prompts, show
from proctor import compat

USAGE = """coursecrafter: creates PiscineBuilder courses. The only part of PiscineBuilder that uses an LLM.

  coursecrafter prompt [--lang L] [--level beginner] [--hours 10]
        print a prompt for any chat assistant; it ends with a curriculum file
  coursecrafter import <curriculum.md> [--course DIR] [--yes] [--paste]
        validate a curriculum, show it, and approve it into the course folder
  coursecrafter new "<what you want to learn>" [--from FILE]... [--course DIR] [--yes] [--debug]
        plan a curriculum in a short terminal conversation (uses the LLM)
  coursecrafter convert <file>... [--course DIR] [--yes] [--debug]
        rewrite a curriculum you made elsewhere in the curriculum format,
        keeping its units and content (uses the LLM)
  coursecrafter revise "<changes>" [--course DIR] [--yes] [--debug]
        have the planner rewrite curriculum.md with your changes (uses the LLM)
  coursecrafter show [curriculum.md] [--course DIR] [--full]
        display the curriculum: units, hours, difficulties, prerequisites, approval
  coursecrafter preview <unit>... [--course DIR]
        print the instructions of built units as proctor renders them, without
        unlocking anything (`d02` or `d02-control-flow`; a project id shows its stages)
  coursecrafter env
        show the environment coursecrafter assumes the student has (detected here,
        or declared with `environment:` in the curriculum front matter)
  coursecrafter setup [--course DIR]
        for a curriculum with Linux units: set up Docker Desktop or WSL here
        (installs or starts it after asking; `new` offers the same)
  coursecrafter build [--course DIR] [--yes] [--tests | --no-tests | --text-only] [--only ID]... [--retries N] [--debug]
        write the unit specs, notes, tests and hidden references (uses the LLM)

new, revise, convert and build log every call (time, tokens, errors, fixes) to .proctor/coursecrafter/logs/;
  --debug also prints it here.

LLM options (build, new, revise, convert): --provider anthropic|meridian|openai  --base-url URL  --model NAME
  --api-key-env VAR. Defaults come from PISCINEBUILDER_* variables, then the config file,
  then: provider anthropic, model claude-sonnet-5-5. Run `coursecrafter config` to see the file path."""


def ask_yes(question: str) -> bool:
    if not is_terminal(sys.stdin):
        return False
    a = read_line(f"{question} [Y/n] ")
    a = a.lower()
    return not a or a == "y" or a == "yes"


def read_line(prompt: str) -> str:
    print(prompt, end="", flush=True)
    try:
        line = sys.stdin.readline()
    except (OSError, ValueError):
        line = ""
    return trim(line)


def open_editor(path: Path) -> None:
    from proctor import run

    editor = os.environ.get("VISUAL")
    if editor is None:
        editor = os.environ.get("EDITOR")
    if editor is None:
        editor = "notepad" if sys.platform == "win32" else "vi"
    words = run.split_words(editor)
    if not words:
        raise Error("no editor")
    prog = words.pop(0)
    try:
        Command(prog).args(words).arg(path).status()
    except OSError as e:
        raise Error(f"cannot start the editor `{editor}`: {io_error(e)} (set EDITOR)") from None


def _push(base: str, part: str) -> str:
    """Rust `PathBuf::push` on the text of a path: an absolute `part`
    replaces `base`, a rooted one keeps only its drive, and a relative one
    is added after a separator unless `base` is empty or ends with one."""
    seps = "\\/" if sys.platform == "win32" else "/"
    if os.path.isabs(part) or os.path.splitdrive(part)[0]:
        return part
    if part[:1] and part[0] in seps:
        return os.path.splitdrive(base)[0] + part
    if base and base[-1] not in seps:
        return base + os.sep + part
    return base + part


class RustPath(Path):
    """A path that formats (in f-strings) as Rust displays a `PathBuf` built
    with `join`: `PathBuf::from(".").join("curriculum.md")` shows
    `.\\curriculum.md` and `--course C:/x` shows `C:/x\\curriculum.md`, where
    pathlib would print `curriculum.md` and `C:\\x\\curriculum.md`. Everything
    else (`str`, `os.fspath`, comparisons) is a plain `Path`'s."""

    shown: str

    def __init__(self, *args: str | os.PathLike[str]) -> None:
        super().__init__(*args)
        shown = ""
        for a in args:
            shown = _push(shown, a.shown if isinstance(a, RustPath) else os.fspath(a))
        self.shown = shown

    def __format__(self, spec: str) -> str:
        return format(self.shown, spec)


@dataclass(kw_only=True)
class Args:
    positional: list[str]
    values: list[tuple[str, str]]
    flags: list[str]

    @staticmethod
    def parse(raw: list[str], valued: Sequence[str], flags: Sequence[str]) -> Args:
        a = Args(positional=[], values=[], flags=[])
        it = iter(raw)
        for x in it:
            if x.startswith("--"):
                name = x[2:]
                if name in flags:
                    a.flags.append(name)
                elif name in valued:
                    v = next(it, None)
                    if v is None:
                        raise Error(f"--{name} needs a value")
                    a.values.append((name, v))
                else:
                    raise Error(f"unknown option --{name}")
            else:
                a.positional.append(x)
        return a

    def get(self, k: str) -> str | None:
        return next((v for n, v in reversed(self.values) if n == k), None)

    def all(self, k: str) -> list[str]:
        return [v for n, v in self.values if n == k]

    def has(self, k: str) -> bool:
        return any(f == k for f in self.flags)

    def llm(self) -> llm.Llm:
        cfg = config.load()
        return llm.Llm.resolve(
            llm.Choice(
                provider=self.get("provider"),
                base_url=self.get("base-url"),
                model=self.get("model"),
                key_env=self.get("api-key-env"),
            ),
            cfg,
        )

    def course(self) -> Path:
        from proctor import course

        d = self.get("course")
        dir = RustPath(d if d is not None else ".")
        try:
            course.migrate(dir)
        except Error as e:
            print(f"warning: {e.msg}", file=sys.stderr)
        return dir


LLM_OPTS = ["provider", "base-url", "model", "api-key-env"]


def main() -> int:
    compat.setup_stdio()
    # Rust's stdout is line-buffered wherever it goes: each line is out before
    # a program coursecrafter starts (an installer, an editor) writes its own.
    reconfigure = getattr(sys.stdout, "reconfigure", None)
    if reconfigure is not None:
        reconfigure(line_buffering=True)
    # Answers are read as UTF-8, as Rust reads them, whatever a pipe (Git
    # Bash, mintty) would otherwise be decoded with.
    reconfigure = getattr(sys.stdin, "reconfigure", None)
    if reconfigure is not None:
        reconfigure(encoding="utf-8", errors="replace")
    try:
        return run_main(sys.argv[1:])
    except KeyboardInterrupt:
        return 130


def run_main(raw: list[str]) -> int:
    """Rust's `main` after reading the arguments: the exit code."""
    try:
        res = dispatch(raw)
    except Error as e:
        print(f"error: {e.msg}", file=sys.stderr)
        return 2
    return 0 if res else 1


def dispatch(raw: list[str]) -> bool:
    match raw[0] if raw else None:
        case None | "-h" | "--help" | "help":
            print(USAGE)
            return True
        case "prompt":
            return cmd_prompt(raw[1:])
        case "import":
            return cmd_import(raw[1:])
        case "new":
            return guided.cmd_new(raw[1:])
        case "revise":
            return guided.cmd_revise(raw[1:])
        case "convert":
            return guided.cmd_convert(raw[1:])
        case "show" | "display":
            return show.cmd_show(raw[1:])
        case "build":
            return cmd_build(raw[1:])
        case "env":
            declared: str | None = None
            try:
                t = read_text(Path("curriculum.md"))
            except OSError:
                pass
            else:
                try:
                    declared = curriculum.parse(t).meta.environment
                except curriculum.ParseError:
                    pass
            print(env.detect(declared).summary(), end="")
            print("\n" + "\n".join(lines(linux.detect().planning_block())[:2]), end="")
            print()
            return True
        case "setup":
            return cmd_setup(raw[1:])
        case "preview":
            return cmd_preview(raw[1:])
        case "config":
            p = config.path()
            if p is not None:
                print(f"{p}{'' if p.is_file() else '  (not created yet)'}")
            else:
                print("no config directory on this system")
            print(
                "keys: provider, base_url, model, api_key_env (the NAME of the variable holding the key)"
            )
            return True
        case other:
            raise Error(f"unknown command `{other}`\n\n{USAGE}")


def cmd_preview(raw: list[str]) -> bool:
    """The rendered instructions of built units, locked or not; the student's
    progress is not touched."""
    from proctor import course, render

    a = Args.parse(raw, ["course"], [])
    if not a.positional:
        raise Error("say which unit: coursecrafter preview d02 [d03 ...]")
    c = course.CourseDir.load(a.course())
    pages: list[str] = []
    for want in a.positional:
        matched = [
            u for u in c.units if u.id == want or u.id.split("-")[0] == want or u.project == want
        ]
        if not matched:
            raise Error(f"no built unit `{want}` (run `coursecrafter build` first?)")
        pages.extend(render.unit_md(c, u) for u in matched)
    print("\n---\n\n".join(pages), end="")
    return True


def cmd_setup(raw: list[str]) -> bool:
    a = Args.parse(raw, ["course"], [])
    file = a.course() / "curriculum.md"
    try:
        text = read_text(file)
    except OSError:
        raise Error(f"no curriculum at {file}") from None
    try:
        plan = curriculum.parse(text)
    except curriculum.ParseError as p:
        raise Error(curriculum.report(p.problems)) from None
    if all(plan.platform(u) is None for u in plan.units()):
        print("No unit needs Linux (`platform: linux`): nothing to set up.")
        return True
    return linux.offer(plan, is_terminal(sys.stdin))


def cmd_prompt(raw: list[str]) -> bool:
    a = Args.parse(raw, ["lang", "level", "hours"], [])
    print(
        prompts.browser_prompt(
            a.get("lang"),
            a.get("level"),
            a.get("hours"),
            f"{env.detect(None).summary()}\n{linux.detect().planning_block()}",
        )
    )
    return True


def unwrap_curriculum(text: str) -> str:
    """Extracts the curriculum when it is wrapped in a ``` block or tags."""
    b = llm.block(text, "curriculum")
    if b is not None:
        return b
    t = trim(text)
    start = t.find("```")
    if not t.startswith("---") and start >= 0:
        body = t[start:]
        nl = body.find("\n")
        after = body[nl + 1 :] if nl >= 0 else ""
        end = after.rfind("```")
        if end >= 0:
            return after[:end]
    return t


def check_plan(text: str, paste: bool) -> curriculum.Plan | None:
    """Parses and validates; prints problems. `None` when there are errors."""
    try:
        plan = curriculum.parse(text)
    except curriculum.ParseError as p:
        print_problems(p.problems, paste)
        return None
    problems = plan.validate()
    problems.extend(plan.preflight(env.detect(plan.meta.environment)))
    if any(not p.warning for p in problems):
        print_problems(problems, paste)
        return None
    if problems:
        print(curriculum.report(problems), end="")
    return plan


def print_problems(problems: list[curriculum.Problem], paste: bool) -> None:
    errors = sum(1 for p in problems if not p.warning)
    print(f"The curriculum has {errors} problem(s):\n{curriculum.report(problems)}")
    if paste:
        print(
            f"----- paste this back into your chat -----\n{curriculum.paste_block(problems)}"
            "------------------------------------------"
        )
    else:
        print("Use --paste to get a block to paste back into the chat that wrote it.")


def approval_path(course: Path) -> Path:
    return course / ".proctor" / "coursecrafter" / "approved"


def text_hash(text: str) -> str:
    h = 0xCBF2_9CE4_8422_2325
    for b in text.replace("\r\n", "\n").encode("utf-8"):
        h ^= b
        h = (h * 0x0100_0000_01B3) & U64_MASK
    return f"{h:016x}"


def approve(course: Path, text: str) -> None:
    p = approval_path(course)
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        raise Error(io_error(e)) from None
    try:
        write_text(p, text_hash(text))
    except OSError as e:
        raise Error(io_error(e)) from None


def review(course: Path, text: str, yes: bool) -> str | None:
    """Shows the plan and asks: approve, edit, or stop. Returns the approved text."""
    file = course / "curriculum.md"
    while True:
        try:
            course.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            raise Error(io_error(e)) from None
        try:
            write_text(file, text)
        except OSError as e:
            raise Error(f"{file}: {io_error(e)}") from None
        plan = check_plan(text, False)
        if plan is None:
            if yes or not is_terminal(sys.stdin):
                return None
            a = read_line("[e]dit the file, or [q]uit? ")
            if a.startswith("e"):
                open_editor(file)
                text = _reread(file)
                continue
            return None
        print(f"\n{plan.meta.title}\n{curriculum.table(plan)}")
        if yes:
            approve(course, text)
            return text
        a = read_line("Approve this curriculum? [Y]es / [e]dit / [q]uit: ").lower()
        if a in ("", "y", "yes"):
            approve(course, text)
            print(f"Approved: {file}")
            return text
        if a.startswith("e"):
            open_editor(file)
            text = _reread(file)
        else:
            print(f"Not approved. The draft is in {file}.")
            return None


def _reread(file: Path) -> str:
    """`fs::read_to_string(file).map_err(|e| e.to_string())`."""
    try:
        return read_text(file)
    except OSError as e:
        raise Error(io_error(e)) from None


def cmd_import(raw: list[str]) -> bool:
    a = Args.parse(raw, ["course"], ["yes", "paste"])
    match a.positional:
        case [file]:
            pass
        case _:
            raise Error("usage: coursecrafter import <curriculum.md> [--course DIR]")
    try:
        body = read_text(file)
    except OSError as e:
        raise Error(f"{file}: {io_error(e)}") from None
    text, fixed = curriculum.autofix(unwrap_curriculum(body))
    for f in fixed:
        print(f"fixed: {f}")
    curriculum.log_validation(a.course(), "import", fixed, curriculum.problems_of(text))
    if a.has("paste") and check_plan(text, True) is None:
        return False
    ok = review(a.course(), text, a.has("yes")) is not None
    if not ok:
        try:
            curriculum.parse(text)
        except curriculum.ParseError:
            print(
                f"If {file} is a plan in your own format, `coursecrafter convert {file}` rewrites it in the curriculum format."
            )
    return ok


def cmd_build(raw: list[str]) -> bool:
    from proctor import course as proctor_course

    valued = ["course", "only", "retries"]
    valued.extend(LLM_OPTS)
    a = Args.parse(raw, valued, ["yes", "tests", "no-tests", "text-only", "debug"])
    course = a.course()
    file = course / "curriculum.md"
    try:
        text = read_text(file)
    except OSError:
        raise Error(
            f"no curriculum at {file}: run `coursecrafter import` or `coursecrafter new` first"
        ) from None
    try:
        approved = read_text(approval_path(course))
    except OSError:
        approved = ""
    if trim(approved) != text_hash(text):
        print("curriculum.md changed since it was approved (or was never approved).")
        t = review(course, text, a.has("yes"))
        if t is None:
            return False
        text = t
    plan = check_plan(text, False)
    if plan is None:
        return False
    llm_ = a.llm()
    retries = 3
    r = a.get("retries")
    if r is not None:
        n = parse_uint(r, (1 << 32) - 1)
        if n is None:
            raise Error("--retries takes a number")
        retries = n
    tests: bool | None = None
    if a.has("tests"):
        tests = True
    elif a.has("no-tests"):
        tests = False
    opts = build.Options(
        retries=retries,
        tests=tests,
        only=a.all("only"),
        yes=a.has("yes"),
        text_only=a.has("text-only"),
        debug=a.has("debug"),
    )
    try:
        root = course.resolve(strict=True)
    except OSError as e:
        raise Error(io_error(e)) from None
    root = proctor_course.strip_verbatim(root)
    environment = env.detect(plan.meta.environment)
    env_file = root / ".proctor" / "coursecrafter" / "environment.txt"
    try:
        env_file.parent.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass
    try:
        write_text(env_file, environment.summary())
    except OSError:
        pass
    debug = opts.debug
    builder = build.Builder.new(root, plan, llm_, opts, environment.prompt_block())
    failure: Error | None = None
    ok = False
    try:
        ok = builder.run()
    except Error as e:
        failure = e
    if failure is not None:
        outcome = f"stopped: {failure.msg}"
    elif ok:
        outcome = "complete"
    else:
        outcome = "finished with units to retry"
    builder.log.finish(llm_, outcome)
    if debug or not ok:
        print(f"log: {builder.log.path}", file=sys.stderr)
    if failure is not None:
        raise failure
    print(f"\nCourse ready in {root}. Students start with: cd {root} && proctor next")
    return ok
