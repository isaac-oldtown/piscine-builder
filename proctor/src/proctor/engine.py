"""One `proctor check`: structure, prototypes, allowed, build, tests,
regression. Results stream to a `Reporter` as they happen."""

from __future__ import annotations

import abc
import copy
import enum
import shutil
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from proctor.command import Command
from proctor.compat import as_millis, lines, trim_end_matches, trim_start_matches
from proctor.errors import Err, Error, Ok, Result
from proctor.unit import FuzzSpec, Law, Target, Unit
from proctor.value import Value, ValueList, ValueStr

DEFAULT_ROUNDS = 200
DEFAULT_TIMEOUT_MS = 10_000
SHRINK_BUDGET = 300

# `usize::MAX`: a method's receiver may or may not be passed.
_USIZE_MAX = (1 << 64) - 1


@dataclass(kw_only=True)
class Item:
    ok: bool
    name: str
    detail: str
    # Randomized: the seed replays it.
    random: bool
    # The spec, not the student, is at fault.
    course_error: bool

    @staticmethod
    def pass_(name: str, detail: str) -> Item:
        return Item(ok=True, name=name, detail=detail, random=False, course_error=False)

    @staticmethod
    def fail(name: str, detail: str) -> Item:
        return Item(ok=False, name=name, detail=detail, random=False, course_error=False)


class Status(enum.Enum):
    PASS = "pass"
    FAIL = "fail"
    SKIP = "skip"


@dataclass(kw_only=True)
class Phase:
    name: str
    status: Status
    summary: str
    items: list[Item]
    # Items were streamed one by one (tests); otherwise only failures show.
    streamed: bool


@dataclass(kw_only=True)
class Verdict:
    ok: bool
    passed: int
    total: int
    first_fail: tuple[str, Item] | None
    warnings: int
    phases: list[Phase]


class Reporter(abc.ABC):
    @abc.abstractmethod
    def start(self, unit: Unit, seed: int, against: str) -> None: ...

    @abc.abstractmethod
    def phase_begin(self, name: str, streamed: bool) -> None: ...

    @abc.abstractmethod
    def item(self, item: Item) -> None: ...

    @abc.abstractmethod
    def phase_end(self, phase: Phase) -> None: ...

    @abc.abstractmethod
    def log(self, text: str) -> None: ...


# `report.Term` subclasses `Reporter` while `report` loads, and `harness`,
# `lang` and `program` load `report`: import them once `Reporter` exists.
from proctor import (  # noqa: E402
    course,
    expr,
    gens,
    harness,
    lang,
    program,
    rtests,
    run,
    scan,
    sig,
    structure,
)


class Against:
    """Base of `AgainstStudent`, `AgainstReference` and `AgainstDir`."""


@dataclass(frozen=True)
class AgainstStudent(Against):
    pass


@dataclass(frozen=True)
class AgainstReference(Against):
    pass


@dataclass(frozen=True)
class AgainstDir(Against):
    path: Path


@dataclass(kw_only=True)
class Options:
    seed: int
    against: Against
    # Only build and run tests (self-test against stubs).
    tests_only: bool


# ------------------------------------------------------------------ tests


class Kind:
    """Base of `KindCheck`, `KindProg`, `KindRust`, `KindFuzz`, `KindFuzzProg`
    and `KindLaw`."""


@dataclass(frozen=True)
class KindCheck(Kind):
    expr: expr.Expr


@dataclass(frozen=True)
class KindProg(Kind):
    case: program.Case


@dataclass(frozen=True)
class KindRust(Kind):
    """A `#[test]` function of a unit's `rust_tests`."""

    unit: str
    test: str
    # Its body, comments and literals blanked.
    code: str


@dataclass(frozen=True)
class KindFuzz(Kind):
    func: str
    params: list[tuple[str, gens.Gen]]
    rounds: int


@dataclass(frozen=True)
class KindFuzzProg(Kind):
    argv: gens.Gen | None
    stdin: gens.Gen | None
    rounds: int


@dataclass(frozen=True)
class KindLaw(Kind):
    vars: list[tuple[str, gens.Gen]]
    checks: list[tuple[str, expr.Expr]]
    rounds: int
    time_ms: int | None


@dataclass(kw_only=True)
class Test:
    name: str
    kind: Kind
    target: Target

    def is_random(self) -> bool:
        return not isinstance(self.kind, KindCheck | KindProg | KindRust)


def helpers_for(c: course.CourseDir, units: list[Unit]) -> list[sig.Proto]:
    """Test-only helper functions of `units` (provided by support files)."""
    out: list[sig.Proto] = []
    for u in units:
        language = c.language(u)
        for f in u.helpers:
            try:
                p = sig.parse(language, f.sig, f.name)
            except Error as e:
                raise Error(f"{u.id}: helper {e.msg}") from None
            out = [x for x in out if x.name != p.name]
            out.append(p)
    return out


def protos_for(c: course.CourseDir, units: list[Unit]) -> list[sig.Proto]:
    out: list[sig.Proto] = []
    for u in units:
        language = c.language(u)
        for f in u.functions:
            try:
                p = sig.parse(language, f.sig, f.name)
            except Error as e:
                raise Error(f"{u.id}: {e.msg}") from None
            out = [x for x in out if x.name != p.name]
            out.append(p)
    return out


def rust_suites(units: list[Unit]) -> list[tuple[str, str]]:
    """The `rust_tests` of `units`, as (unit id, source)."""
    return [(u.id, u.rust_tests) for u in units if u.rust_tests is not None and u.has_rust_tests()]


def expression_calls(t: Test) -> list[str]:
    """The functions a case, a fuzz entry or a law calls through the harness."""
    names: list[str] = []
    match t.kind:
        case KindCheck(e):
            expr.calls(e, names)
        case KindFuzz(func=func):
            names.append(func)
        case KindLaw(checks=checks):
            for _, e in checks:
                expr.calls(e, names)
    return names


def arities(protos: list[sig.Proto]) -> list[expr.Arity]:
    out: list[expr.Arity] = []
    for p in protos:
        max_ = len(p.params)
        min_ = sum(1 for x in p.params if not x.has_default)
        # A method's receiver may or may not be passed: no count check.
        if p.is_method:
            min_, max_ = 0, _USIZE_MAX
        out.append((p.name, min_, max_))
    return out


def parse_check(src: str, arities: list[expr.Arity], vars: list[str]) -> expr.Expr:
    """Parses one check and validates its names against the expected functions."""
    e = expr.resolve_methods(expr.parse(src), arities)
    expr.static_check(e, arities, vars)
    return e


def rounds_of(u: Unit, own: int | None) -> int:
    r = own if own is not None else u.random.rounds
    return max(r if r is not None else DEFAULT_ROUNDS, 1)


def parse_gen(c: course.CourseDir, y: Any) -> gens.Gen:
    return gens.parse(y, c.lib)


def tests_of(c: course.CourseDir, u: Unit, protos: list[sig.Proto]) -> list[Test]:
    """Builds the test list of one unit. Errors are spec errors."""
    from proctor import report

    out: list[Test] = []

    def find(name: str) -> sig.Proto | None:
        return next((p for p in protos if p.name == name), None)

    ars = arities(protos)
    for n, line in u.case_lines():
        short = report.truncate(line, 70)
        kind: Kind
        match u.target:
            case Target.FUNCTION:
                try:
                    kind = KindCheck(parse_check(line, ars, []))
                except Error as e:
                    raise Error(f"{u.id} cases line {n}: {e.msg}") from None
            case Target.PROGRAM:
                try:
                    kind = KindProg(program.parse_case(line))
                except Error as e:
                    raise Error(f"{u.id} cases line {n}: {e.msg}") from None
        out.append(Test(name=f"case {short}", kind=kind, target=u.target))
    if u.rust_tests is not None and u.has_rust_tests():
        if c.language(u) != "rust":
            raise Error(f"{u.id}: `rust_tests` is for Rust units only")
        found = rtests.parse(u.rust_tests)
        why = rtests.problem(u.rust_tests, found)
        if why is not None:
            raise Error(f"{u.id} rust_tests: {why}")
        for rt in found:
            out.append(
                Test(
                    name=f"test {rt.name}",
                    kind=KindRust(unit=u.id, test=rt.name, code=rt.code),
                    target=u.target,
                )
            )
    for f in u.random.fuzz:
        if isinstance(f, str) and u.target == Target.FUNCTION:
            spec = FuzzSpec(func=f)
        elif isinstance(f, str):
            spec = FuzzSpec(name=f)
        else:
            spec = copy.copy(f)
        rounds = rounds_of(u, spec.rounds)
        if u.target == Target.FUNCTION:
            fname = spec.func
            if fname is None:
                raise Error(f"{u.id}: fuzz entry needs `fn:`")
            p = find(fname)
            if p is None:
                raise Error(f"{u.id}: fuzz `{fname}` is not an expected function")
            params: list[tuple[str, gens.Gen]] = []
            for prm in p.params:
                if prm.name in spec.args:
                    try:
                        g = parse_gen(c, spec.args[prm.name])
                    except Error as e:
                        raise Error(f"{u.id}: fuzz {fname} arg {prm.name}: {e.msg}") from None
                else:
                    d = sig.default_gen(prm.ty)
                    if d is None:
                        raise Error(
                            f"{u.id}: fuzz {fname}: no default generator for `{prm.name}: {prm.raw}`; give one under args:"
                        )
                    g = d
                params.append((prm.name, g))
            for k in sorted(spec.args):
                if not any(x.name == k for x in p.params):
                    raise Error(f"{u.id}: fuzz {fname}: `{k}` is not a parameter")
            out.append(
                Test(
                    name=f"fuzz {fname}",
                    kind=KindFuzz(func=fname, params=params, rounds=rounds),
                    target=u.target,
                )
            )
        else:
            argv: gens.Gen | None = None
            if spec.argv is not None:
                try:
                    argv = parse_gen(c, spec.argv)
                except Error as e:
                    raise Error(f"{u.id}: fuzz argv: {e.msg}") from None
            stdin: gens.Gen | None = None
            if spec.stdin is not None:
                try:
                    stdin = parse_gen(c, spec.stdin)
                except Error as e:
                    raise Error(f"{u.id}: fuzz stdin: {e.msg}") from None
            if argv is None and stdin is None:
                raise Error(f"{u.id}: a program fuzz entry needs `argv:` and/or `stdin:`")
            name = spec.name if spec.name is not None else "program"
            out.append(
                Test(
                    name=f"fuzz {name}",
                    kind=KindFuzzProg(argv=argv, stdin=stdin, rounds=rounds),
                    target=u.target,
                )
            )
    for i, law in enumerate(u.random.laws):
        out.append(law_test(c, u, law, i, protos))
    return out


def law_test(c: course.CourseDir, u: Unit, law: Law, i: int, protos: list[sig.Proto]) -> Test:
    def find(name: str) -> sig.Proto:
        p = next((p for p in protos if p.name == name), None)
        if p is None:
            raise Error(f"{u.id}: law uses `{name}`, which is not an expected function")
        return p

    vars: list[tuple[str, gens.Gen]] = []
    for k, y in sorted(law.vars.items()):
        try:
            gen = parse_gen(c, y)
        except Error as e:
            raise Error(f"{u.id}: law gen `{k}`: {e.msg}") from None
        vars.append((k, gen))

    def defaults(p: sig.Proto, vars: list[tuple[str, gens.Gen]]) -> list[str]:
        names: list[str] = []
        for prm in p.params:
            if not any(n == prm.name for n, _ in vars):
                g = sig.default_gen(prm.ty)
                if g is None:
                    raise Error(
                        f"{u.id}: law: no default generator for `{prm.name}: {prm.raw}`; add it under gen:"
                    )
                vars.append((prm.name, g))
            names.append(prm.name)
        return names

    checks: list[str] = list(law.check)
    label = law.name
    if law.roundtrip is not None:
        pair = law.roundtrip
        if len(pair) != 2:
            raise Error(f"{u.id}: roundtrip takes [f, g]")
        f, g = pair
        names = defaults(find(f), vars)
        if not names:
            raise Error(f"{u.id}: roundtrip: `{f}` takes no argument")
        first = names[0]
        checks.append(f"{g}({f}({', '.join(names)})) == {first}")
        if label is None:
            label = f"roundtrip {f} / {g}"
    if law.idempotent is not None:
        f = law.idempotent
        names = defaults(find(f), vars)
        if len(names) != 1:
            raise Error(f"{u.id}: idempotent `{f}` must take exactly one argument")
        checks.append(f"{f}({f}({names[0]})) == {f}({names[0]})")
        if label is None:
            label = f"idempotent {f}"
    if law.agree is not None:
        fs_ = law.agree
        if len(fs_) != 2:
            raise Error(f"{u.id}: agree takes [f, g]")
        f, g = fs_
        names = defaults(find(f), vars)
        joined = ", ".join(names)
        checks.append(f"{f}({joined}) == {g}({joined})")
        if label is None:
            label = f"agree {f} / {g}"
    if not checks:
        raise Error(f"{u.id}: law #{i + 1} has no check")
    parsed: list[tuple[str, expr.Expr]] = []
    ars = arities(protos)
    names = [n for n, _ in vars]
    for src in checks:
        try:
            check = parse_check(src, ars, names)
        except Error as e:
            raise Error(f"{u.id}: law check `{src}`: {e.msg}") from None
        parsed.append((src, check))
    label = label if label is not None else f"law {i + 1}"
    return Test(
        name=f"law {label}",
        kind=KindLaw(
            vars=vars,
            checks=parsed,
            rounds=rounds_of(u, law.rounds),
            time_ms=law.time_ms,
        ),
        target=u.target,
    )


# ------------------------------------------------------------------- hosts


def args_short(args: list[Value]) -> str:
    return ", ".join(a.short(40) for a in args)


@dataclass(kw_only=True)
class EvalHost(expr.Host):
    vars: list[tuple[str, Value]]
    harness: harness.Harness | None
    program: program.Program | None

    def var(self, name: str) -> Value | None:
        return next((v for n, v in self.vars if n == name), None)

    def call(self, name: str, args: expr.Args) -> Value | None:
        if name == "run":
            if self.program is not None:
                return self.program.call(args)
            raise expr.EvalErrorSpec("run() is only available for program targets")
        h = self.harness
        if h is None:
            return None
        if name not in h.funcs:
            return None
        vals = [v for _, v in args]
        try:
            return h.call(name, vals)
        except harness.CallError as e:
            raise expr.EvalErrorStudent(f"{name}({args_short(vals)}) {e.message()}") from None

    def is_external(self, name: str) -> bool:
        return name == "run" or (self.harness is not None and name in self.harness.funcs)


def describe_vars(vars: list[tuple[str, Value]]) -> str:
    return ", ".join(f"{n} = {v.short(60)}" for n, v in vars)


def shrink(
    gens: list[tuple[str, gens.Gen]],
    values: list[tuple[str, Value]],
    fails: Callable[[list[tuple[str, Value]]], bool],
) -> None:
    """Greedy shrinking: keep any smaller value that still fails."""
    budget = SHRINK_BUDGET
    while True:
        improved = False
        for i in range(len(values)):
            g = next((g for n, g in gens if n == values[i][0]), None)
            if g is None:
                continue
            for cand in g.shrink(values[i][1]):
                if budget == 0:
                    return
                budget -= 1
                trial = list(values)
                trial[i] = (trial[i][0], cand)
                if fails(trial):
                    values[:] = trial
                    improved = True
                    break
            if improved:
                break
        if not improved:
            return


# -------------------------------------------------------------- the runner


@dataclass(kw_only=True)
class Built:
    harness: harness.Harness | None
    program: program.Program | None
    rust: rtests.Suites | None = None

    def close(self) -> None:
        """Rust drops the harness here, which stops its process."""
        if self.harness is not None:
            self.harness.close()


@dataclass(kw_only=True)
class Reference:
    built: Result[Built] | None


@dataclass(kw_only=True)
class Engine:
    c: course.CourseDir
    unit: Unit
    lang: str
    opts: Options
    env: dict[str, str]
    timeout: float

    @staticmethod
    def new(c: course.CourseDir, unit: Unit, opts: Options) -> Engine:
        env = c.env(unit)
        ms = c.timeout_ms(unit)
        return Engine(
            c=c,
            unit=unit,
            lang=c.language(unit),
            opts=opts,
            env=env,
            timeout=(ms if ms is not None else DEFAULT_TIMEOUT_MS) / 1000,
        )

    def source_dir(self) -> Path:
        match self.opts.against:
            case AgainstStudent():
                return self.c.workspace(self.unit)
            case AgainstReference():
                return self.c.ref_dir(self.unit)
            case AgainstDir(p):
                return p
        raise AssertionError(self.opts.against)

    def cache_key(self, what: str) -> str:
        return f"{what}-{self.unit.id}"

    def stage(self, src: Path, key: str) -> tuple[Path, Path, Path]:
        """Prepares a buildable tree for `src`: in place for Rust, else a fresh copy."""
        copy_, build, base = lang.work_paths(self.c.hidden() / "cache", key)
        if lang.builds_in_place(self.lang):
            return (src, build, base)
        shutil.rmtree(copy_, ignore_errors=True)
        run.copy_tree(src, copy_)
        return (copy_, build, base)

    def build(
        self,
        ws: Path,
        work: Path,
        cache: Path,
        protos: list[sig.Proto],
        need_fn: bool,
        need_prog: bool,
        suites: list[tuple[str, str]] | None = None,
    ) -> tuple[Built, str]:
        """Raises `lang.BuildError`. `suites` are the `rust_tests` to compile,
        as (unit id, source)."""
        # A copy: the flags below must not leak into a shared config.
        cfg = self.c.lang_config(self.unit).copy()
        extra: list[str] = [f for u in self.c.chain(self.unit) for f in u.extra_flags]
        if extra:
            flags = list(cfg.flags) if cfg.flags is not None else list(lang.C_FLAGS)
            flags.extend(extra)
            cfg.flags = flags
        support = self.c.support(self.unit)
        wrap = self.c.wrap(self.unit)
        ctx = lang.Ctx(
            lang=self.lang,
            ws=ws,
            work=work,
            env=self.env,
            cache=cache,
            cfg=cfg,
            support=support,
            wrap=wrap,
        )
        if self.lang == "rust":
            # Cargo trusts mtimes; a copied file can look older than the last
            # build. Always rebuild the graded crate itself (deps stay cached).
            try:
                pkg = lang.cargo_package(ws)
            except lang.BuildError:
                pass
            else:
                c = Command("cargo")
                c.args(["clean", "--quiet", "-p", pkg]).current_dir(ws).env(
                    "CARGO_TARGET_DIR", cache / "target"
                )
                try:
                    run.run(c, None, lang.BUILD_TIMEOUT)
                except Error:
                    pass
        built = Built(harness=None, program=None)
        log = ""
        if need_prog:
            cmd, l_ = lang.build_program(ctx, self.unit_run(), self.c.build_cmd(self.unit))
            log += l_
            built.program = program.Program(
                cmd=cmd, cwd=ws, env=dict(self.env), timeout=self.timeout
            )
        if need_fn:
            module = next(
                (u.module for u in reversed(self.c.chain(self.unit)) if u.module is not None),
                None,
            )
            h, l_ = lang.build_harness(ctx, protos, module, self.timeout)
            log += l_
            built.harness = h
        if suites:
            try:
                built.rust = rtests.build(ctx, suites, self.timeout)
            except lang.BuildError:
                built.close()
                raise
        return (built, log)

    def src_opts(self) -> scan.SrcOpts:
        lc = self.c.lang_config(self.unit)
        return scan.SrcOpts(
            exts=list(lc.extensions) if lc.extensions is not None else [],
            comment=lc.comment,
        )

    def unit_run(self) -> str | None:
        return self.c.run_cmd(self.unit)

    def check(self, rep: Reporter) -> Verdict:
        chain = self.c.chain(self.unit)
        protos = protos_for(self.c, chain)
        # What the harness exposes: the student's functions plus test helpers.
        callable = list(protos)
        callable.extend(helpers_for(self.c, chain))
        unit_tests: list[tuple[Unit, list[Test]]] = []
        for u in chain:
            unit_tests.append((u, tests_of(self.c, u, callable)))
        match self.opts.against:
            case AgainstStudent():
                against = "your work"
            case AgainstReference():
                against = "the reference solution"
            case AgainstDir(d):
                against = str(d)
        rep.start(self.unit, self.opts.seed, against)
        phases: list[Phase] = []
        blocked = False
        warnings = 0
        src = self.source_dir()

        # structure
        if not src.is_dir():
            ws_name = self.c.workspace_name(self.unit)
            ph = Phase(
                name="structure",
                status=Status.FAIL,
                summary=f"{ws_name}/ not found",
                items=[Item.fail(f"{ws_name}/", "the workspace directory does not exist")],
                streamed=False,
            )
            rep.phase_end(ph)
            phases.append(ph)
            return self.verdict(phases, 0)
        if self.unit.files and not self.opts.tests_only:
            items = [
                Item(ok=f.ok, name=f.name, detail=f.detail, random=False, course_error=False)
                for f in structure.check(src, self.unit.files)
            ]
            ph = phase("structure", items, False, lambda n, t: f"{n}/{t} rules")
            blocked |= ph.status == Status.FAIL
            rep.phase_end(ph)
            phases.append(ph)

        # prototypes
        if protos and not self.opts.tests_only:
            srcs = scan.sources_ext(self.lang, src, self.src_opts())
            items = []
            for p in protos:
                match scan.find_prototype(self.lang, srcs, p):
                    case scan.ProtoFindingOk():
                        items.append(Item.pass_(p.name, "found"))
                    case scan.ProtoFindingMissing():
                        owner = p.owner()
                        where = "" if owner is None else f" in an `impl {owner}` block"
                        items.append(
                            Item.fail(p.name, f"not found{where} (expected `{p.sig_text}`)")
                        )
                    case scan.ProtoFindingMismatch(m):
                        items.append(Item.fail(p.name, f"expected `{p.norm}`, {m}"))
            ph = phase(
                "prototypes",
                items,
                False,
                lambda _n, _t: ", ".join(
                    x.short_name() if x.owner() is None else x.name for x in protos
                ),
            )
            blocked |= ph.status == Status.FAIL
            rep.phase_end(ph)
            phases.append(ph)

        # allowed
        allowed = None if self.opts.tests_only else self.c.allowed(self.unit)
        if allowed is not None:
            extra = self.c.import_patterns(self.unit)
            opts = self.src_opts()
            # The current stage's forbidden list: a later stage may lift a ban.
            r = scan.check_rules(self.lang, src, allowed, self.unit.forbidden, extra, opts)
            checked = r.checked
            if not r.violations:
                items = [Item.pass_("whitelist", f"{checked} use(s) checked")]
            else:
                items = [Item.fail(v.what, f"at {v.at} is not in Allowed") for v in r.violations]
            ph = phase(
                "allowed",
                items,
                False,
                lambda _n, _t: (
                    "only built-ins and your own code"
                    if checked == 0
                    else f"{checked} use(s) checked, all allowed"
                ),
            )
            rep.phase_end(ph)
            phases.append(ph)

        if blocked:
            for name in ["build", "tests"]:
                ph = Phase(
                    name=name,
                    status=Status.SKIP,
                    summary="skipped (fix the steps above first)",
                    items=[],
                    streamed=False,
                )
                rep.phase_end(ph)
                phases.append(ph)
            return self.verdict(phases, warnings)

        # build
        # Tests written in Rust need no harness: they call the crate themselves.
        need_fn = any(
            t.target == Target.FUNCTION and not isinstance(t.kind, KindRust)
            for _, ts in unit_tests
            for t in ts
        ) and bool(protos)
        need_prog = any(u.target == Target.PROGRAM for u in chain)
        suites = rust_suites(chain) if self.lang == "rust" else []
        match self.opts.against:
            case AgainstStudent():
                what_key = "student"
            case AgainstReference():
                what_key = "ref"
            case AgainstDir(_):
                what_key = "dir"
        key = self.cache_key(what_key)
        ws, work, base = self.stage(src, key)
        items = []
        built = Built(harness=None, program=None)
        reference = Reference(built=None)
        try:
            build_ok = True
            if need_fn or need_prog or lang.has_harness(self.lang) or self.lang == "bash":
                try:
                    b, log = self.build(ws, work, base, callable, need_fn, need_prog, suites)
                except lang.BuildError as e:
                    rep.log(e.log)
                    items.append(Item.fail("compile", e.msg))
                    build_ok = False
                else:
                    warnings += lang.count_warnings(log)
                    match (need_fn, need_prog):
                        case (True, True):
                            what = "functions and program"
                        case (True, False):
                            what = "functions loaded by proctor's own main"
                        case (False, True):
                            what = "program"
                        case _:
                            what = "sources"
                    if suites:
                        what = (
                            "your crate and proctor's tests"
                            if what == "sources"
                            else f"{what}, and proctor's tests"
                        )
                    items.append(Item.pass_("compile", what))
                    built = b
            if build_ok and not self.opts.tests_only:
                for name, cmd in self.static_checks():
                    words = run.split_words(cmd)
                    if not words:
                        continue
                    command = Command(lang.resolve(words[0]))
                    command.args(words[1:]).current_dir(ws).envs(self.env)
                    if self.lang == "rust":
                        command.env("CARGO_TARGET_DIR", base / "target")
                    try:
                        o = run.run(command, None, lang.BUILD_TIMEOUT)
                    except Error as e:
                        items.append(Item.fail(name, e.msg))
                        continue
                    if o.success:
                        items.append(Item.pass_(name, "clean"))
                        continue
                    all_out = f"{o.stdout}{o.stderr}"
                    rep.log(all_out)
                    missing = "no such command" in all_out or "is not installed" in all_out
                    if missing:
                        msg = f"`{' '.join(words[: min(2, len(words))])}` is not installed"
                    elif name == "fmt":
                        diff = next((x for x in lines(all_out) if x.startswith("Diff in ")), None)
                        if diff is not None:
                            path = trim_end_matches(trim_start_matches(diff, "Diff in "), ":")
                            msg = f"not formatted: {relativize(path, ws)}"
                        else:
                            msg = "not formatted (run the formatter)"
                    else:
                        first = lang.first_error(relativize(all_out, ws))
                        msg = first if first is not None else "failed"
                    items.append(Item.fail(name, msg))
            if items:
                ph = phase(
                    "build",
                    items,
                    False,
                    lambda _n, _t: (
                        f"ok ({warnings} warning(s))" if warnings > 0 else "ok, no warning"
                    ),
                )
                blocked |= ph.status == Status.FAIL
                rep.phase_end(ph)
                phases.append(ph)
            if blocked:
                ph = Phase(
                    name="tests",
                    status=Status.SKIP,
                    summary="skipped (the build failed)",
                    items=[],
                    streamed=False,
                )
                rep.phase_end(ph)
                phases.append(ph)
                return self.verdict(phases, warnings)

            # tests, then regression
            n = len(unit_tests)
            # Current stage first, then regression on earlier stages in order.
            order = [n - 1, *range(n - 1)]
            for idx in order:
                u, tests = unit_tests[idx]
                is_current = idx + 1 == n
                if not tests:
                    continue
                name = "tests" if is_current else "regression"
                rep.phase_begin(name, True)
                items = []
                for t in tests:
                    item = self.run_test(t, built, reference, callable)
                    if not is_current:
                        item.name = f"{u.id.split('-')[0]} {item.name}"
                    rep.item(item)
                    items.append(item)
                ph = phase(name, items, True, lambda ok, total: f"{ok}/{total}")
                rep.phase_end(ph)
                phases.append(ph)
            # Leak checkers and sanitizers report when the process ends.
            if built.harness is not None:
                finished = built.harness.finish()
                if finished is not None:
                    ph = phase(
                        "exit",
                        [Item.fail("clean exit", finished)],
                        False,
                        lambda _n, _t: "",
                    )
                    rep.phase_end(ph)
                    phases.append(ph)
            return self.verdict(phases, warnings)
        finally:
            # Rust drops `reference`, then `built`, at the end of `check`.
            match reference.built:
                case Ok(rb):
                    rb.close()
            built.close()

    def static_checks(self) -> list[tuple[str, str]]:
        return [(s.name, s.cmd) for s in self.c.static_checks(self.unit)]

    def verdict(self, phases: list[Phase], warnings: int) -> Verdict:
        passed = 0
        total = 0
        first_fail: tuple[str, Item] | None = None
        for p in phases:
            if p.status == Status.SKIP:
                continue
            for i in p.items:
                total += 1
                if i.ok:
                    passed += 1
                elif first_fail is None:
                    first_fail = (p.name, copy.copy(i))
        ok = first_fail is None and all(p.status != Status.FAIL for p in phases)
        return Verdict(
            ok=ok,
            passed=passed,
            total=total,
            first_fail=first_fail,
            warnings=warnings,
            phases=phases,
        )

    def reference(self, r: Reference, protos: list[sig.Proto]) -> Built:
        if r.built is None:

            def res() -> Built:
                d = self.c.ref_dir(self.unit)
                if not d.is_dir():
                    raise Error(f"no reference solution at .proctor/refs/{self.unit.id}")
                key = self.cache_key("oracle")
                ws, work, base = self.stage(d, key)
                chain = self.c.chain(self.unit)
                need_fn = bool(protos) and any(
                    u.target == Target.FUNCTION and u.has_tests() for u in chain
                )
                need_prog = any(u.target == Target.PROGRAM for u in chain)
                try:
                    b, _ = self.build(ws, work, base, protos, need_fn, need_prog)
                except lang.BuildError as e:
                    raise Error(f"the reference solution does not build: {e.msg}") from None
                return b

            built: Result[Built]
            try:
                b = res()
            except Error as e:
                built = Err(e.msg)
            else:
                built = Ok(b)
            r.built = built
        match r.built:
            case Ok(b):
                return b
            case Err(msg):
                raise Error(msg)
        raise Error("internal: reference not built")

    def run_test(
        self,
        t: Test,
        built: Built,
        reference: Reference,
        protos: list[sig.Proto],
    ) -> Item:
        rng = gens.Rng.new(gens.sub_seed(self.opts.seed, t.name))
        match t.kind:
            case KindCheck(e):
                host = EvalHost(vars=[], harness=built.harness, program=built.program)
                match expr.check(e, host):
                    case expr.OutcomePass():
                        item = Item.pass_(t.name, "")
                    case expr.OutcomeFail(m):
                        item = Item.fail(t.name, m)
                    case expr.OutcomeSpecError(m):
                        item = course_error(t.name, m)
            case KindProg(pc):
                if built.program is None:
                    item = course_error(t.name, "no program was built")
                else:
                    try:
                        rr = built.program.run(pc.args, pc.stdin, pc.files)
                    except Error as err:
                        item = course_error(t.name, err.msg)
                    else:
                        why = program.judge(pc, rr)
                        item = Item.pass_(t.name, "") if why is None else Item.fail(t.name, why)
            case KindRust(unit=unit_id, test=test_name):
                if built.rust is None:
                    item = course_error(t.name, "proctor's tests were not built")
                else:
                    try:
                        failed = built.rust.run(unit_id, test_name)
                    except Error as err:
                        item = course_error(t.name, err.msg)
                    else:
                        item = (
                            Item.pass_(t.name, "") if failed is None else Item.fail(t.name, failed)
                        )
            case KindLaw(vars, checks, rounds, time_ms):
                item = self.run_law(t, vars, checks, rounds, time_ms, built, rng)
            case KindFuzz(func, params, rounds):
                try:
                    rb = self.reference(reference, protos)
                except Error as err:
                    item = course_error(t.name, err.msg)
                else:
                    rh = rb.harness
                    if rh is None:
                        return course_error(t.name, "reference has no harness")
                    sh = built.harness
                    if sh is None:
                        return course_error(t.name, "no harness was built")
                    item = fuzz_function(t.name, func, params, rounds, sh, rh, rng)
            case KindFuzzProg(argv, stdin, rounds):
                try:
                    rb = self.reference(reference, protos)
                except Error as err:
                    item = course_error(t.name, err.msg)
                else:
                    if built.program is not None and rb.program is not None:
                        item = fuzz_program(
                            t.name, argv, stdin, rounds, built.program, rb.program, rng
                        )
                    else:
                        item = course_error(t.name, "no program was built")
            case _:
                raise AssertionError(t.kind)
        item.random = t.is_random()
        return item

    def run_law(
        self,
        t: Test,
        vars: list[tuple[str, gens.Gen]],
        checks: list[tuple[str, expr.Expr]],
        rounds: int,
        time_ms: int | None,
        built: Built,
        rng: gens.Rng,
    ) -> Item:
        # Returns the first failing check's message, or None; raises for a spec error.
        def eval_round(vals: list[tuple[str, Value]], built: Built) -> str | None:
            started = time.monotonic()
            for src, e in checks:
                host = EvalHost(vars=vals, harness=built.harness, program=built.program)
                match expr.check(e, host):
                    case expr.OutcomePass():
                        pass
                    case expr.OutcomeFail(m):
                        return f"{src}: {m}"
                    case expr.OutcomeSpecError(m):
                        raise Error(f"{src}: {m}")
            if time_ms is not None:
                ms = as_millis(time.monotonic() - started)
                if ms > time_ms:
                    return f"took {ms} ms (limit {time_ms} ms)"
            return None

        def fails(trial: list[tuple[str, Value]]) -> bool:
            try:
                return eval_round(trial, built) is not None
            except Error:
                return False

        for round in range(1, rounds + 1):
            vals = [(n, g.generate(rng)) for n, g in vars]
            try:
                first = eval_round(vals, built)
            except Error as e:
                return course_error(t.name, e.msg)
            if first is None:
                continue
            if time_ms is None:
                shrink(vars, vals, fails)
            try:
                again = eval_round(vals, built)
            except Error:
                again = None
            msg = again if again is not None else "failed"
            with_ = "" if not vals or time_ms is not None else f" with {describe_vars(vals)}"
            return Item.fail(t.name, f"{msg}{with_} (round {round})")
        return Item.pass_(t.name, f"{rounds} rounds")


def course_error(name: str, msg: str) -> Item:
    return Item(
        ok=False,
        name=name,
        detail=f"course error: {msg}",
        random=False,
        course_error=True,
    )


def relativize(s: str, base: Path) -> str:
    b = str(base)
    out = s.replace(f"{b}\\", "").replace(f"{b}/", "")
    out = out.replace("\\\\?\\", "")
    return out.replace("\\", "/")


def phase(
    name: str,
    items: list[Item],
    streamed: bool,
    summary: Callable[[int, int], str],
) -> Phase:
    ok = sum(1 for i in items if i.ok)
    total = len(items)
    status = Status.PASS if ok == total else Status.FAIL
    if status == Status.PASS:
        text = summary(ok, total)
    elif streamed:
        text = f"{ok}/{total}"
    else:
        failing = [i for i in items if not i.ok]
        if len(failing) == 1 and failing[0].name == "compile":
            text = failing[0].detail
        elif len(failing) == 1:
            text = f"{failing[0].name} {failing[0].detail}"
        else:
            text = f"{len(failing)} problems"
    return Phase(name=name, status=status, summary=text, items=items, streamed=streamed)


def call_result(h: harness.Harness, f: str, args: list[Value]) -> Value:
    try:
        return h.call(f, args)
    except harness.CallError as e:
        raise Error(e.message()) from None


def fuzz_function(
    name: str,
    func: str,
    params: list[tuple[str, gens.Gen]],
    rounds: int,
    student: harness.Harness,
    reference: harness.Harness,
    rng: gens.Rng,
) -> Item:
    skipped = 0

    # None if the reference rejects the input; else (None,) when the student
    # agrees with the reference, (message,) when they differ.
    def differs(
        vals: list[tuple[str, Value]],
        student: harness.Harness,
        reference: harness.Harness,
    ) -> tuple[str | None] | None:
        args = [v for _, v in vals]
        try:
            want = call_result(reference, func, args)
        except Error:
            # Outside the reference's domain: not a meaningful input.
            return None
        call = f"{func}({args_short(args)})"
        try:
            got = call_result(student, func, args)
        except Error as e:
            return (f"{call} {e.msg}",)
        if got == want:
            return (None,)
        return (f"{call}: expected {want.short(100)}, got {got.short(100)}",)

    def fails(trial: list[tuple[str, Value]]) -> bool:
        d = differs(trial, student, reference)
        return d is not None and d[0] is not None

    for round in range(1, rounds + 1):
        vals = [(n, g.generate(rng)) for n, g in params]
        d = differs(vals, student, reference)
        if d is None:
            skipped += 1
        elif d[0] is None:
            pass
        else:
            shrink(params, vals, fails)
            again = differs(vals, student, reference)
            msg = (
                again[0]
                if again is not None and again[0] is not None
                else "differs from the reference"
            )
            return Item.fail(name, f"{msg} (round {round})")
    if skipped == rounds:
        return course_error(
            name,
            "the reference rejected every generated input; check the generators",
        )
    return Item.pass_(name, f"{rounds} rounds vs reference")


def _debug_code(code: int | None) -> str:
    """`{:?}` of an `Option<i32>`."""
    return "None" if code is None else f"Some({code})"


def fuzz_program(
    name: str,
    argv: gens.Gen | None,
    stdin: gens.Gen | None,
    rounds: int,
    student: program.Program,
    reference: program.Program,
    rng: gens.Rng,
) -> Item:
    def to_args(v: Value) -> list[str]:
        match v:
            case ValueList(items):
                out: list[str] = []
                for x in items:
                    t = x.text()
                    out.append(t if t is not None else str(x))
                return out
            case other:
                t = other.text()
                return run.split_words(t if t is not None else "")

    gens_: list[tuple[str, gens.Gen]] = []
    if argv is not None:
        gens_.append(("argv", argv))
    if stdin is not None:
        gens_.append(("stdin", stdin))

    # Raises `Error` when a program cannot run.
    def differs(vals: list[tuple[str, Value]]) -> str | None:
        a = next((v for n, v in vals if n == "argv"), None)
        args = to_args(a) if a is not None else []
        s = next((v for n, v in vals if n == "stdin"), None)
        input = s.text() if s is not None else None
        want = reference.run(args, input, [])
        got = student.run(args, input, [])
        if got.stopped:
            return got.status
        if got.out != want.out:
            return (
                f"stdout: expected {ValueStr(want.out).short(80)}, "
                f"got {ValueStr(got.out).short(80)}"
            )
        if got.exit != want.exit:
            return f"exit code: expected {_debug_code(want.exit)}, got {_debug_code(got.exit)}"
        return None

    def fails(trial: list[tuple[str, Value]]) -> bool:
        try:
            return differs(trial) is not None
        except Error:
            return False

    for round in range(1, rounds + 1):
        vals = [(n, g.generate(rng)) for n, g in gens_]
        try:
            d = differs(vals)
        except Error as e:
            return course_error(name, e.msg)
        if d is None:
            continue
        shrink(gens_, vals, fails)
        try:
            again = differs(vals)
        except Error:
            again = None
        msg = again if again is not None else "differs from the reference"
        return Item.fail(name, f"{msg} with {describe_vars(vals)} (round {round})")
    return Item.pass_(name, f"{rounds} rounds vs reference")


def validate(c: course.CourseDir) -> list[str]:
    """Parses every unit's tests and signatures without running anything."""
    problems: list[str] = []
    for u in c.units:
        chain = c.chain(u)
        language = c.language(u)
        if u.forbidden and c.allowed(u) is None:
            problems.append(
                f"{u.id}: `forbidden` needs an `allowed` list in this unit or an earlier stage of its project (for example `allowed: [std]` to allow all of std but the forbidden items)"
            )
        if u.platform is not None and u.platform != "linux":
            problems.append(f"{u.id}: unknown platform `{u.platform}` (only `linux`)")
        # Regression runs earlier stages with this one, on the same system.
        if u.platform is None:
            prev = next((x for x in chain if x.platform is not None), None)
            if prev is not None:
                problems.append(
                    f"{u.id}: `{prev.id}` before it needs Linux, so this stage needs `platform: linux` too"
                )
        try:
            protos = protos_for(c, chain)
        except Error as e:
            problems.append(e.msg)
            continue
        try:
            protos.extend(helpers_for(c, chain))
        except Error as e:
            problems.append(e.msg)
            continue
        for s in c.support(u):
            if not s.is_file():
                problems.append(f"{u.id}: support file {s} is missing")
        # With tests in Rust, only the functions that cases call must suit the harness.
        in_rust = language == "rust" and bool(rust_suites(chain))
        unsupported: dict[str, str] = {}
        if u.target == Target.FUNCTION and u.has_tests():
            if not lang.has_harness(language):
                problems.append(
                    f"{u.id}: `target: function` is not available for {language}; use `target: program`"
                )
            for p in protos:
                try:
                    lang.harness_supports(language, p)
                except Error as e:
                    if in_rust:
                        unsupported[p.name] = e.msg
                    else:
                        problems.append(f"{u.id}: {e.msg}")
        if u.target == Target.PROGRAM and c.run_cmd(u) is None and language not in ("c", "rust"):
            problems.append(f"{u.id}: `target: program` needs `run:` for {language}")
        for key, cmd in (("run", c.run_cmd(u)), ("build", c.build_cmd(u))):
            if cmd is not None and not run.split_words(cmd):
                problems.append(f"{u.id}: `{key}:` is empty; remove it or give a command")
        try:
            tests = tests_of(c, u, protos)
        except Error as e:
            problems.append(e.msg)
        else:
            for name in dict.fromkeys(n for t in tests for n in expression_calls(t)):
                if name in unsupported:
                    problems.append(f"{u.id}: {unsupported[name]} (test it in `rust_tests`)")
        needs_ref = bool(u.random.fuzz)
        if needs_ref and not c.ref_dir(u).is_dir():
            problems.append(f"{u.id}: fuzz needs a reference solution in .proctor/refs/{u.id}/")
    return problems
