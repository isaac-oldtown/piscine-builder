"""Course self-test, used by coursecrafter's quality gate and by course
authors: the reference solution must pass every check, and every
function test must fail against empty stubs (otherwise it proves nothing)."""

from __future__ import annotations

import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from proctor import course, engine, expr, lang, sig
from proctor.compat import U64_MASK, io_error, write_text
from proctor.errors import Err, Error, Ok, Result
from proctor.unit import Target, Unit


class Silent(engine.Reporter):
    """Reports nothing; keeps the build logs.

    The Rust struct's `log` field cannot share its name with the `log`
    method in Python: `Silent(log="")` still builds one, and the text is
    in `log_text`."""

    def __init__(self, *, log: str = "") -> None:
        self.log_text = log

    def start(self, unit: Unit, seed: int, against: str) -> None:
        pass

    def phase_begin(self, name: str, streamed: bool) -> None:
        pass

    def item(self, item: engine.Item) -> None:
        pass

    def phase_end(self, phase: engine.Phase) -> None:
        pass

    def log(self, text: str) -> None:
        self.log_text += text


@dataclass(kw_only=True)
class UnitReport:
    unit: str
    reference: Result[tuple[int, int]]
    # Weak spots: functions no test catches on an empty stub, tests that test nothing.
    vacuous: list[str]
    vacuity_checked: int
    notes: list[str]

    def ok(self) -> bool:
        return isinstance(self.reference, Ok) and not self.vacuous

    def to_json(self) -> dict[str, Any]:
        """One JSON line, how the Linux container reports back. The keys are
        in serde_json's (sorted) order."""
        passed: list[int] | None
        error: str | None
        match self.reference:
            case Ok((p, t)):
                passed, error = [p, t], None
            case Err(e):
                passed, error = None, e
        return {
            "error": error,
            "notes": list(self.notes),
            "passed": passed,
            "unit": self.unit,
            "vacuity_checked": self.vacuity_checked,
            "vacuous": list(self.vacuous),
        }

    @staticmethod
    def from_json(v: Any) -> UnitReport | None:
        def get(k: str) -> Any:
            return v.get(k) if isinstance(v, dict) else None

        def as_u64(x: Any) -> int | None:
            return x if type(x) is int and 0 <= x <= U64_MASK else None

        def strings(k: str) -> list[str]:
            a = get(k)
            if not isinstance(a, list):
                return []
            return [x for x in a if isinstance(x, str)]

        pt, e = get("passed"), get("error")
        reference: Result[tuple[int, int]]
        if isinstance(pt, list):
            p = as_u64(pt[0]) if len(pt) > 0 else None
            if p is None:
                return None
            t = as_u64(pt[1]) if len(pt) > 1 else None
            if t is None:
                return None
            reference = Ok((p, t))
        elif isinstance(e, str):
            reference = Err(e)
        else:
            return None
        unit = get("unit")
        if not isinstance(unit, str):
            return None
        checked = as_u64(get("vacuity_checked"))
        return UnitReport(
            unit=unit,
            reference=reference,
            vacuous=strings("vacuous"),
            vacuity_checked=checked if checked is not None else 0,
            notes=strings("notes"),
        )


def stub_workspace(
    lang: str,
    protos: list[sig.Proto],
    module: str | None,
    dir: Path,
    reference: Path,
) -> None:
    """Writes a workspace holding only mechanical stubs for `protos`. `module` is
    the unit's `module:`, the Python module of undotted function names."""
    # The `lang` parameter hides the module of the same name.
    from proctor import lang as lang_mod

    shutil.rmtree(dir, ignore_errors=True)
    try:
        dir.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        raise Error(io_error(e)) from None

    def w(rel: str, body: str) -> None:
        p = dir / rel
        try:
            p.parent.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            raise Error(io_error(e)) from None
        try:
            write_text(p, body)
        except OSError as e:
            raise Error(f"{p}: {io_error(e)}") from None

    match lang:
        case "c":
            body = lang_mod.stub_source(lang, protos) or ""
            w(
                "stubs.c",
                f"#include <stddef.h>\n#include <stdbool.h>\n#include <stdint.h>\n\n{body}",
            )
        case "python":
            by_module: dict[str, list[sig.Proto]] = {}
            for p in protos:
                if "." in p.name:
                    m = p.name.rsplit(".", 1)[0]
                else:
                    m = module if module is not None else ""
                by_module.setdefault(m, []).append(p)
            for m, ps in sorted(by_module.items()):
                body = lang_mod.stub_source(lang, ps) or ""
                if not m:
                    raise Error("python stubs need dotted names or a `module:`")
                rel = f"{m.replace('.', '/')}/__init__.py"
                w(rel, body)
        case "rust":
            try:
                pkg = lang_mod.cargo_package(reference)
            except lang_mod.BuildError as e:
                raise Error(e.msg) from None
            w(
                "Cargo.toml",
                f'[package]\nname = "{pkg}"\nversion = "0.0.0"\nedition = "2024"\n\n[workspace]\n',
            )
            w("src/lib.rs", lang_mod.stub_source(lang, protos) or "")
        case other:
            raise Error(f"no stub generator for {other}")


def unit(c: course.CourseDir, u: Unit, seed: int) -> UnitReport:
    r = UnitReport(unit=u.id, reference=Err(""), vacuous=[], vacuity_checked=0, notes=[])
    ref_dir = c.ref_dir(u)
    if not ref_dir.is_dir():
        if u.has_tests() or u.functions:
            r.reference = Err(f"no reference solution in .proctor/refs/{u.id}/")
        else:
            r.reference = Ok((0, 0))
    else:
        opts = engine.Options(seed=seed, against=engine.AgainstReference(), tests_only=False)
        silent = Silent(log="")
        try:
            v = engine.Engine.new(c, u, opts).check(silent)
        except Error as e:
            r.reference = Err(e.msg)
        else:
            if v.ok:
                r.reference = Ok((v.passed, v.total))
            elif v.first_fail is not None:
                cat, item = v.first_fail
                r.reference = Err(f"{cat} {item.name}: {item.detail}")
            else:
                r.reference = Err("failed")
    language = c.language(u)
    if u.target != Target.FUNCTION or not u.has_tests():
        return r
    if not lang.has_harness(language):
        r.notes.append(f"no stub check for {language}")
        return r
    chain = c.chain(u)
    try:
        protos = engine.protos_for(c, chain)
    except Error as e:
        r.notes.append(e.msg)
        return r
    d = c.hidden() / "cache" / f"stubs-{u.id}" / "tree"
    try:
        stub_workspace(language, protos, u.module, d, ref_dir)
    except Error as e:
        r.notes.append(f"stubs: {e.msg}")
        return r
    # Tests may also call test-only helpers; the stubs cover only the student's functions.
    callable = list(protos)
    try:
        callable.extend(engine.helpers_for(c, chain))
    except Error:
        pass
    try:
        tests = engine.tests_of(c, u, callable)
    except Error as e:
        r.notes.append(e.msg)
        return r
    known = [p.name for p in protos]

    def called(t: engine.Test) -> list[str]:
        names: list[str] = []
        match t.kind:
            case engine.KindCheck(e):
                expr.calls(e, names)
            case engine.KindFuzz(func=func):
                names.append(func)
            case engine.KindLaw(checks=checks):
                for _, e in checks:
                    expr.calls(e, names)
            case engine.KindProg() | engine.KindFuzzProg():
                pass
        return [n for n in names if n in known]

    opts = engine.Options(seed=seed, against=engine.AgainstDir(d), tests_only=True)
    silent = Silent(log="")
    try:
        v = engine.Engine.new(c, u, opts).check(silent)
    except Error as e:
        r.notes.append(e.msg)
        return r
    failed_on_stub = [
        i.name
        for p in v.phases
        if p.name == "tests" and p.status != engine.Status.SKIP
        for i in p.items
        if not i.ok
    ]
    for t in tests:
        names = called(t)
        if not names and isinstance(t.kind, engine.KindCheck | engine.KindLaw):
            r.vacuous.append(f"`{t.name}` calls no student function")
    for f in u.functions:
        calling = [t for t in tests if f.name in called(t)]
        r.vacuity_checked += 1
        if not calling:
            r.vacuous.append(f"`{f.name}` is never tested")
        elif not any(t.name in failed_on_stub for t in calling):
            r.vacuous.append(f"no test of `{f.name}` fails on an empty stub")
    failed_build = next(
        (p for p in v.phases if p.name == "build" and p.status == engine.Status.FAIL), None
    )
    if failed_build is not None:
        r.notes.append(f"stubs do not build: {failed_build.summary}")
    return r
