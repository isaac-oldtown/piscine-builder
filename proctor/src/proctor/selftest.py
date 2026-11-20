"""Course self-test, used by coursecrafter's quality gate and by course
authors. There is no reference solution: the tests run against empty stubs,
where they must build and run without a course error, and every function
needs a test that fails there (otherwise it proves nothing)."""

from __future__ import annotations

import os
import re
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from proctor import course, engine, lang, rtests, sig
from proctor.compat import U64_MASK, io_error, write_text
from proctor.errors import Error
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
    # Why the tests cannot run on stubs: they do not build, or a test is wrong.
    error: str | None
    # Weak spots: functions no test catches on an empty stub, tests that test nothing.
    vacuous: list[str]
    vacuity_checked: int
    notes: list[str]

    def ok(self) -> bool:
        return self.error is None and not self.vacuous

    def to_json(self) -> dict[str, Any]:
        """One JSON line, how the Linux container reports back (sorted keys)."""
        return {
            "error": self.error,
            "notes": list(self.notes),
            "unit": self.unit,
            "vacuity_checked": self.vacuity_checked,
            "vacuous": list(self.vacuous),
        }

    @staticmethod
    def from_json(v: Any) -> UnitReport | None:
        def get(k: str) -> Any:
            return v.get(k) if isinstance(v, dict) else None

        def strings(k: str) -> list[str]:
            a = get(k)
            if not isinstance(a, list):
                return []
            return [x for x in a if isinstance(x, str)]

        unit, error, checked = get("unit"), get("error"), get("vacuity_checked")
        if not isinstance(unit, str) or not (error is None or isinstance(error, str)):
            return None
        return UnitReport(
            unit=unit,
            error=error,
            vacuous=strings("vacuous"),
            vacuity_checked=checked if type(checked) is int and 0 <= checked <= U64_MASK else 0,
            notes=strings("notes"),
        )


def crate_name(unit_id: str) -> str:
    """A Cargo package name for a unit id: `d03-longest-word` -> `d03_longest_word`."""
    return re.sub(r"[^A-Za-z0-9_]", "_", unit_id)


def rust_types(chain: list[Unit]) -> str:
    """The `types` blocks of a project chain, in order."""
    return "".join(f"{t.rstrip()}\n\n" for u in chain if (t := u.types) and t.strip())


def stub_workspace(
    lang: str,
    protos: list[sig.Proto],
    module: str | None,
    dir: Path,
    package: str,
    types: str = "",
) -> None:
    """Writes a workspace holding only mechanical stubs for `protos`. `module` is
    the unit's `module:`, the Python module of undotted function names; Rust
    stubs are package `package` and start with `types`."""
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
            w(
                "Cargo.toml",
                f'[package]\nname = "{package}"\nversion = "0.0.0"\nedition = "2024"\n\n[workspace]\n',
            )
            stubs = lang_mod.stub_source(lang, protos, lang_mod.type_names(types)) or ""
            w("src/lib.rs", f"{types}{stubs}")
        case other:
            raise Error(f"no stub generator for {other}")


def package_of(c: course.CourseDir, chain: list[Unit]) -> str:
    """The package the student's workspace declares: the scaffold's, else one
    named after the project (or drill)."""
    for u in chain:
        d = c.hidden() / "scaffold" / u.id
        if os.path.isfile(d / "Cargo.toml"):
            try:
                return lang.cargo_package(d)
            except lang.BuildError:
                break
    return crate_name(chain[0].id)


def unit(c: course.CourseDir, u: Unit, seed: int) -> UnitReport:
    r = UnitReport(unit=u.id, error=None, vacuous=[], vacuity_checked=0, notes=[])
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
        r.error = e.msg
        return r
    d = c.hidden() / "cache" / f"stubs-{u.id}" / "tree"
    try:
        stub_workspace(language, protos, u.module, d, package_of(c, chain), rust_types(chain))
    except Error as e:
        r.error = f"stubs: {e.msg}"
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
        r.error = e.msg
        return r
    known = [p.name for p in protos]

    def called(t: engine.Test) -> list[str]:
        if isinstance(t.kind, engine.KindRust):
            return rtests.calls(t.kind.code, known)
        return [n for n in engine.expression_calls(t) if n in known]

    opts = engine.Options(seed=seed, against=engine.AgainstDir(d), tests_only=True)
    try:
        v = engine.Engine.new(c, u, opts).check(Silent(log=""))
    except Error as e:
        r.error = e.msg
        return r
    failed_build = next(
        (p for p in v.phases if p.name == "build" and p.status == engine.Status.FAIL), None
    )
    if failed_build is not None:
        r.error = f"the tests do not build against empty stubs: {failed_build.summary}"
        return r
    ran = [
        i for p in v.phases if p.name == "tests" and p.status != engine.Status.SKIP for i in p.items
    ]
    wrong = next((i for i in ran if i.course_error), None)
    if wrong is not None:
        r.error = f"{wrong.name}: {wrong.detail}"
        return r
    failed_on_stub = [i.name for i in ran if not i.ok]
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
    return r
