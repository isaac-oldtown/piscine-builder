"""The `proctor` command: what the student and the course author run."""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

from proctor import (
    ask,
    compat,
    course,
    docker,
    engine,
    gens,
    ide,
    journal,
    progress,
    report,
    scaffold,
    selftest,
)
from proctor.errors import Err, Error, Ok
from proctor.unit import Scaffold, Target, Unit

USAGE = """proctor: PiscineBuilder's grader. Works offline, never calls an LLM.

  proctor check [unit] [--seed HEX] [--verbose] [--no-prompt]
                               check your work (default: the current unit)
  proctor next              reveal the next unit once the current one passes
  proctor skip              skip the current unit if it is a stretch goal (optional)
  proctor status            where you are in the course
  proctor progress          regenerate and summarize PROGRESS.md
  proctor explain [unit]    show exactly what the checks run (generators, rounds)
  proctor init [pwsh|bash|zsh|fish]
                               a shell function that moves you to each new unit's
                               folder; add it to your shell profile (see the README)
  proctor shell [unit] [--port N]
                               a terminal in the Linux container, for Linux units
                               (--port publishes port N on 127.0.0.1 of this machine)

  Course authors: proctor validate | proctor selftest [unit...]

Exit codes: 0 OK, 1 KO, 2 usage or course error. NO_COLOR and PROCTOR_ASCII are honoured."""


def main() -> int:
    compat.setup_stdio()
    # Rust's stdout is line-buffered wherever it goes: each line is out before
    # a program proctor starts (the Linux container, a build) writes its own.
    reconfigure = getattr(sys.stdout, "reconfigure", None)
    if reconfigure is not None:
        reconfigure(line_buffering=True)
    try:
        return run_main(sys.argv[1:])
    except KeyboardInterrupt:
        return 130


def run_main(args: list[str]) -> int:
    """Rust's `main` after reading the arguments: the exit code."""
    try:
        match args[0] if args else None:
            case None | "-h" | "--help" | "help":
                print(USAGE)
                return 0
            case "check":
                return cmd_check(args[1:])
            case "next" | "start":
                return cmd_next(args[1:])
            case "skip":
                return cmd_skip()
            case "status":
                return cmd_status()
            case "progress":
                return cmd_progress()
            case "explain":
                return cmd_explain(args[1:])
            case "validate":
                return cmd_validate()
            case "selftest":
                return cmd_selftest(args[1:])
            case "shell":
                return cmd_shell(args[1:])
            case "init":
                s = ide.shell_init(args[1] if len(args) > 1 else "pwsh")
                print(s, end="")
                return 0
            case other:
                raise Error(f"unknown command `{other}`\n\n{USAGE}")
    except Error as e:
        print(f"error: {e.msg}", file=sys.stderr)
        return 2


def course_() -> course.CourseDir:
    """Rust's `course()`: the course the current directory is in."""
    try:
        cwd = Path.cwd()
    except OSError as e:
        raise Error(compat.io_error(e)) from None
    return course.CourseDir.discover(cwd)


def random_seed() -> int:
    nanos = max(time.time_ns(), 0) & compat.U64_MASK
    return gens.Rng.new(nanos ^ ((os.getpid() << 32) & compat.U64_MASK)).next_u64()


def parse_hex_u64(s: str) -> int | None:
    """Rust `u64::from_str_radix(s, 16)`: an optional `+`, hex digits, no overflow."""
    digits = s[1:] if s.startswith("+") else s
    if not digits or any(ch not in "0123456789abcdefABCDEF" for ch in digits):
        return None
    v = int(digits, 16)
    return v if v <= compat.U64_MASK else None


def _canonical(p: Path) -> Path:
    """`strip_verbatim(&p.canonicalize().unwrap_or(p))`."""
    try:
        resolved = p.resolve(strict=True)
    except (OSError, RuntimeError):
        resolved = p
    return course.strip_verbatim(resolved)


def pick(c: course.CourseDir, state: course.State, explicit: str | None) -> Unit:
    """The unit to check: explicit id, else the workspace the cwd is in, else the current unit."""
    if explicit is not None:
        found = next(
            (u for u in c.units if u.id == explicit or u.id.split("-")[0] == explicit), None
        )
        if found is None:
            raise Error(f"no unit `{explicit}` in this course")
        if found.id not in state.unlocked:
            raise Error(f"`{found.id}` is still locked")
        return found
    try:
        cwd = Path.cwd()
    except OSError as e:
        raise Error(compat.io_error(e)) from None
    cwd = _canonical(cwd)
    in_ws = next(
        (
            u
            for u in reversed(c.units)
            if u.id in state.unlocked and cwd.is_relative_to(_canonical(c.workspace(u)))
        ),
        None,
    )
    if in_ws is not None:
        # Inside a project workspace, prefer the first stage not passed yet.
        wsn = c.workspace_name(in_ws)
        pending = next(
            (
                x
                for x in c.units
                if c.workspace_name(x) == wsn and x.id in state.unlocked and not state.done(x.id)
            ),
            None,
        )
        return pending if pending is not None else in_ws
    if state.current is not None:
        cur = c.unit(state.current)
        if cur is None:
            raise Error(f"unknown current unit `{state.current}`")
        return cur
    raise Error("nothing unlocked yet: run `proctor next` to start")


def record(c: course.CourseDir, ev: str, unit: str) -> None:
    journal.append(c.root, journal.Event(t=journal.now(), ev=ev, unit=unit))


def offer_next(root: Path, after: str | None, prompt: bool) -> None:
    """After a pass or a skip: offers the next unit. Stretch goals can be skipped
    on the spot, in which case the one after is offered."""
    while True:
        c = course.CourseDir.load(root)
        state = c.state()
        style = report.Style.detect()
        nxt = state.next_locked(c)
        if nxt is None:
            print(style.green("Course complete. Well played."))
            return
        prev = c.unit(after) if after is not None else None
        same_project = nxt.is_stage() and prev is not None and prev.project == nxt.project
        label = "stage" if same_project else "exercise"
        if not prompt or not ask.interactive():
            hint = " (a stretch goal: `proctor skip` skips it)" if nxt.stretch else ""
            print(f"Run `proctor next` when you are ready for {nxt.id}{hint}.")
            return
        if nxt.stretch:
            options = [
                f"Start {nxt.id}",
                "Skip it: it is an optional stretch goal",
                "Not now",
            ]
            match ask.choose(
                f"The next {label}, {nxt.id}, is a stretch goal. What now?",
                options,
                0,
                None,
            ):
                case ask.AnswerPick(index=0):
                    reveal(c, nxt)
                    return
                case ask.AnswerPick(index=1):
                    record(c, "unlock", nxt.id)
                    record(c, "skip", nxt.id)
                    print(f"Skipped {nxt.id}.")
                    progress.write(course.CourseDir.load(root))
                    continue
                case _:
                    print(f"Run `proctor next` when you are ready for {nxt.id}.")
                    return
        if ask.yes_no(
            f"Go to the next {label} ({nxt.id})?",
            "Yes, unlock it now",
            "Not now",
            False,
        ):
            reveal(c, nxt)
        else:
            print(f"Run `proctor next` when you are ready for {nxt.id}.")
        return


def review_reminder(c: course.CourseDir) -> None:
    """At the start of a new week, writes the review of the week that ended."""
    try:
        path = progress.weekly_review(c, journal.now())
    except Error:
        return
    if path is not None:
        style = report.Style.detect()
        print(
            f"{style.bold('A week has ended:')} take five minutes for {path}: what you can do on your own, what is still unclear, whether the schedule holds."
        )


def cmd_skip() -> int:
    c = course_()
    state = c.state()
    cur = c.unit(state.current) if state.current is not None else None
    if cur is not None and not state.done(cur.id):
        target = cur
    else:
        # Current unit done: skipping applies to the next one, still locked.
        locked = state.next_locked(c)
        if locked is None:
            raise Error("nothing left to skip")
        target = locked
    if not target.stretch:
        raise Error(f"`{target.id}` is required; only units marked as stretch goals can be skipped")
    if target.id not in state.unlocked:
        record(c, "unlock", target.id)
    record(c, "skip", target.id)
    print(
        f"Skipped {target.id} (stretch goal). You can still come back to it with `proctor check {target.id}`."
    )
    progress.write(course.CourseDir.load(c.root))
    review_reminder(c)
    offer_next(c.root, target.id, True)
    return 0


def cmd_check(args: list[str]) -> int:
    seed: int | None = None
    verbose = False
    prompt = True
    explicit: str | None = None
    it = iter(args)
    for a in it:
        match a:
            case "--seed":
                hex_seed = next(it, None)
                if hex_seed is None:
                    raise Error("--seed needs a value")
                seed = parse_hex_u64(compat.trim_start_matches(hex_seed, "0x"))
                if seed is None:
                    raise Error("--seed takes a hex number")
            case "--verbose" | "-v":
                verbose = True
            case "--no-prompt":
                prompt = False
            # From the host running a Linux unit in the container: it offers
            # the next unit itself, so it can move the shell there.
            case "--host-offers-next":
                prompt = False
            case s if s.startswith("-"):
                raise Error(f"unknown option {s}")
            case s:
                explicit = s
    c = course_()
    state = c.state()
    u = pick(c, state, explicit)
    if docker.needed(u):
        full = ["check", *args, "--host-offers-next"]
        code = docker.delegate(c, full)
        was_passed = u.id in state.passed
        if code == 0 and not was_passed and u.id in course.CourseDir.load(c.root).state().passed:
            offer_next(c.root, u.id, prompt)
        return code
    if seed is None:
        seed = random_seed()
    opts = engine.Options(seed=seed, against=engine.AgainstStudent(), tests_only=False)
    term = report.Term(style=report.Style.detect(), verbose=verbose, quiet=False)
    started = time.monotonic()
    v = engine.Engine.new(c, u, opts).check(term)
    print(report.verdict_line(v, seed, term.style))
    cat: str | None = None
    item: str | None = None
    msg: str | None = None
    if v.first_fail is not None:
        cat, i = v.first_fail
        item = i.name
        msg = report.truncate(i.detail, 300)
    journal.append(
        c.root,
        journal.Event(
            t=journal.now(),
            ev="check",
            unit=u.id,
            ok=v.ok,
            passed=v.passed,
            total=v.total,
            cat=cat,
            item=item,
            msg=msg,
            seed=f"{seed:016x}",
            ms=compat.as_millis(time.monotonic() - started),
            warnings=v.warnings,
        ),
    )
    newly_passed = v.ok and u.id not in state.passed
    if newly_passed:
        journal.append(c.root, journal.Event(t=journal.now(), ev="pass", unit=u.id))
    c = course.CourseDir.load(c.root)
    progress.write(c)
    review_reminder(c)
    if newly_passed and "--host-offers-next" not in args:
        offer_next(c.root, u.id, prompt)
    return 0 if v.ok else 1


def reveal(c: course.CourseDir, u: Unit) -> None:
    """Unlocks `u` on a clear screen, adds the default VS Code tasks to the
    course, and asks the shell function (`proctor init`) to move there."""
    r = scaffold.unlock(c, u)
    ide.clear_screen()
    print_reveal(c, u, r)
    vscode = ide.install_vscode(c)
    if vscode:
        print(f"  created {' and '.join(vscode)} (in VS Code, Ctrl+Shift+B runs `proctor check`)")
    ide.request_cd(c, u)


def _slashes(p: Path) -> str:
    """`p.to_string_lossy().replace('\\\\', "/")`."""
    return str(p).replace("\\", "/")


def print_reveal(c: course.CourseDir, u: Unit, r: scaffold.Revealed) -> None:
    style = report.Style.detect()
    print(f"{style.bold('Unlocked')} {u.id}: {u.title}")
    print(f"  instructions: {_slashes(c.subject_rel(u))}")
    for f in r.created:
        print(f"  created {f}")
    for path, body in r.kept:
        add = "\n".join(f"    {line}" for line in compat.lines(body))
        print(f"  kept your {path}; add these if they are missing:\n{add}")
    if docker.needed(u):
        print(
            "  this unit needs Linux: `proctor check` builds and tests it in a Linux container (Docker Desktop must be running), and `proctor shell` opens a terminal there to run your program by hand"
        )
    if u.scaffold == Scaffold.NONE:
        print(
            f"  no workspace was created: setting it up is part of the exercise ({c.workspace_name(u)}/)"
        )


def cmd_next(args: list[str]) -> int:
    force = "--force" in args
    c = course_()
    state = c.state()
    cur = state.current
    if cur is not None and not state.done(cur) and not force:
        cu = c.unit(cur)
        stretch = cu is not None and cu.stretch
        hint = " (it is a stretch goal: `proctor skip` skips it)" if stretch else ""
        raise Error(f"finish `{cur}` first: `proctor check` must print OK{hint}")
    nxt = state.next_locked(c)
    if nxt is None:
        print("Nothing left to unlock: the course is complete.")
        return 0
    reveal(c, nxt)
    c = course.CourseDir.load(c.root)
    progress.write(c)
    review_reminder(c)
    return 0


def cmd_status() -> int:
    c = course_()
    state = c.state()
    style = report.Style.detect()
    print(f"{style.bold(c.course.title)}  ({len(state.passed)} / {len(c.units)} units passed)")
    for u in c.units:
        if u.id in state.passed:
            mark = style.green("passed  ")
        elif u.id in state.skipped:
            mark = style.dim("skipped ")
        elif u.id in state.unlocked:
            mark = style.yellow("current ")
        else:
            mark = style.dim("locked  ")
        if u.id in state.unlocked:
            stretch = " (stretch)" if u.stretch else ""
            shown = f"{u.id}: {u.title}{stretch}"
        else:
            shown = style.dim("?")
        print(f"  {mark} {shown}")
    current = c.unit(state.current) if state.current is not None else None
    if current is not None and state.done(current.id):
        nxt = state.next_locked(c)
        if nxt is not None:
            print(f"\n{current.id} is done. Run `proctor next` to unlock {nxt.id}.")
        else:
            print("\nCourse complete.")
    elif current is not None:
        print(
            f"\nCurrent: {current.id}  workspace {c.workspace_name(current)}/  instructions {_slashes(c.subject_rel(current))}"
        )
    else:
        print("\nRun `proctor next` to start.")
    return 0


def cmd_progress() -> int:
    c = course_()
    progress.write(c)
    text = progress.render(c, journal.read(c.root))
    all_lines = compat.lines(text)
    i = 0
    while i < len(all_lines) and not all_lines[i].startswith("## Dashboard"):
        i += 1
    i += 4
    dashboard: list[str] = []
    while i < len(all_lines) and all_lines[i].startswith("|"):
        dashboard.append(all_lines[i])
        i += 1
    for line in dashboard:
        cells = [compat.trim(x) for x in line.strip("|").split(" | ")]
        if len(cells) == 2:
            k, v = cells
            print(f"  {k:<24} {v}")
    print(f"\nFull report: {Path('PROGRESS.md')}")
    return 0


def cmd_explain(args: list[str]) -> int:
    c = course_()
    state = c.state()
    u = pick(c, state, args[0] if args else None)
    chain = c.chain(u)
    protos = engine.protos_for(c, chain)
    # `{:?}` of the target.
    target = "Function" if u.target == Target.FUNCTION else "Program"
    print(f"{u.id} ({c.language(u)}, target: {target})")
    for p in protos:
        print(f"  function {p.name}   checked as `{p.norm}`")
    callable_ = list(protos)
    callable_.extend(engine.helpers_for(c, chain))
    for x in chain:
        tests = engine.tests_of(c, x, callable_)
        if not tests:
            continue
        print(f"\n{'tests of ' if x.id == u.id else 'regression from '}{x.id}")
        for t in tests:
            match t.kind:
                case engine.KindCheck() | engine.KindProg():
                    print(f"  {t.name}")
                case engine.KindFuzz(func=func, params=params, rounds=rounds):
                    print(f"  fuzz {func}: {rounds} rounds, compared with the reference")
                    for n, g in params:
                        print(f"      {n}: {gens.describe(g)}")
                case engine.KindFuzzProg(argv=argv, stdin=stdin, rounds=rounds):
                    print(
                        f"  {t.name}: {rounds} rounds, stdout and exit code compared with the reference"
                    )
                    if argv is not None:
                        print(f"      argv: {gens.describe(argv)}")
                    if stdin is not None:
                        print(f"      stdin: {gens.describe(stdin)}")
                case engine.KindLaw(vars=vars_, checks=checks, rounds=rounds, time_ms=time_ms):
                    per_round = f", {time_ms} ms per round" if time_ms is not None else ""
                    print(f"  {t.name}: {rounds} rounds{per_round}")
                    for n, g in vars_:
                        print(f"      {n}: {gens.describe(g)}")
                    for src, _ in checks:
                        print(f"      check {src}")
    return 0


def cmd_validate() -> int:
    c = course_()
    problems = engine.validate(c)
    if not problems:
        print(f"OK {len(c.units)} unit(s) parse cleanly")
        return 0
    for p in problems:
        print(f"KO {p}")
    return 1


def cmd_shell(args: list[str]) -> int:
    port: int | None = None
    explicit: str | None = None
    it = iter(args)
    for a in it:
        match a:
            case "--port":
                v = next(it, None)
                if v is None:
                    raise Error("--port needs a number")
                port = compat.parse_uint(v, 0xFFFF)
                if port is None:
                    raise Error("--port takes a port number")
            case s if s.startswith("-"):
                raise Error(f"unknown option {s}")
            case s:
                explicit = s
    c = course_()
    state = c.state()
    try:
        u = pick(c, state, explicit)
    except Error:
        dir_ = c.root
    else:
        dir_ = c.workspace(u)
    if sys.platform.startswith("linux"):
        raise Error("you are already on Linux: run your program in this terminal")
    return docker.shell(c, dir_, port)


def cmd_selftest(args: list[str]) -> int:
    seed: int | None = None
    json_ = False
    validate = True
    names: list[str] = []
    it = iter(args)
    for a in it:
        match a:
            case "--seed":
                v = next(it, None)
                if v is None:
                    raise Error("--seed needs a value")
                seed = parse_hex_u64(v)
                if seed is None:
                    raise Error("--seed takes a hex number")
            # Used by the Linux container to report back.
            case "--json":
                json_ = True
            case "--no-validate":
                validate = False
            case s if s.startswith("-"):
                raise Error(f"unknown option {s}")
            case s:
                names.append(s)
    c = course_()
    problems = engine.validate(c) if validate else []
    for p in problems:
        print(f"KO spec: {p}")
    ids: list[Unit] = []
    if not names:
        ids = list(c.units)
    else:
        for name in names:
            found = c.unit(name)
            if found is None:
                raise Error(f"no unit `{name}`")
            ids.append(found)
    if seed is None:
        seed = random_seed()
    all_ok = not problems
    style = report.Style.detect()
    linux: list[Unit] = []
    here: list[Unit] = []
    for u in ids:
        (linux if docker.needed(u) else here).append(u)
    reports = [selftest.unit(c, u, seed) for u in here]
    if linux:
        print(f"{len(linux)} Linux unit(s): testing in the Linux container...", file=sys.stderr)
        reports.extend(docker.selftest(c, linux, seed))

    # Back in course order (a report of no listed unit first, as `None` sorts in Rust).
    def course_order(r: selftest.UnitReport) -> int:
        return next((i for i, u in enumerate(ids) if u.id == r.unit), -1)

    reports.sort(key=course_order)
    for r in reports:
        if json_:
            # serde_json's maps are sorted by key.
            print(
                json.dumps(r.to_json(), separators=(",", ":"), ensure_ascii=False, sort_keys=True)
            )
            all_ok &= r.ok()
            continue
        name = r.unit
        match r.reference:
            case Ok((passed, total)):
                reference = style.green(f"reference OK {passed}/{total}")
            case Err(e):
                reference = style.red(f"reference KO {report.truncate(e, 160)}")
        if r.vacuity_checked == 0:
            vac = ""
        elif not r.vacuous:
            vac = style.green(f"  every function caught on stubs ({r.vacuity_checked})")
        else:
            vac = style.red(f"  weak: {'; '.join(r.vacuous)}")
        print(f"{name:<28} {reference}{vac}")
        for n in r.notes:
            print(f"{'':<28} {style.yellow(n)}")
        all_ok &= r.ok()
    if not json_:
        print(f"seed {seed:016x}")
    return 0 if all_ok else 1
