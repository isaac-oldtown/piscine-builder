"""Revealing a unit: its instructions, its notes, and a light workspace.
Never overwrites anything the student wrote."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from proctor import course, engine, journal, lang, render, rules
from proctor.compat import io_error, read_text, write_text
from proctor.errors import Error
from proctor.unit import Rule, Scaffold, Unit


@dataclass(kw_only=True)
class Revealed:
    created: list[str] = field(default_factory=list)
    # Files that already existed and were left alone, with what to add.
    kept: list[tuple[str, str]] = field(default_factory=list)


def _create_dir_all(d: Path, show: bool) -> None:
    """`fs::create_dir_all`, failing as `{dir}: {e}` (or `{e}` when not `show`)."""
    try:
        d.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        raise Error(f"{d}: {io_error(e)}" if show else io_error(e)) from None


def write_new(root: Path, rel: str, body: str, r: Revealed, shown: str) -> None:
    p = root / rel
    if os.path.exists(p):
        return
    d = p.parent
    _create_dir_all(d, True)
    try:
        write_text(p, body)
    except OSError as e:
        raise Error(f"{p}: {io_error(e)}") from None
    slashed = rel.replace("\\", "/")
    r.created.append(f"{shown}{slashed}")


def write_doc(root: Path, rel: Path, body: str, r: Revealed) -> None:
    """Writes the rendered instructions (and overview) even if they exist: the
    student never edits them, and the record is the source of truth."""
    p = root / rel
    _create_dir_all(p.parent, True)
    existed = os.path.exists(p)
    try:
        write_text(p, body)
    except OSError as e:
        raise Error(f"{p}: {io_error(e)}") from None
    if not existed:
        r.created.append(str(rel).replace("\\", "/"))


def copy_scaffold(from_: Path, to: Path, prefix: str, r: Revealed, shown: str) -> None:
    try:
        rd = list(os.scandir(from_))
    except OSError:
        return
    for e in rd:
        name = e.name
        rel = name if not prefix else f"{prefix}/{name}"
        path = Path(e.path)
        if os.path.isdir(path):
            copy_scaffold(path, to, rel, r, shown)
        else:
            try:
                body = path.read_bytes()
            except OSError as err:
                raise Error(io_error(err)) from None
            dst = to / rel
            if not os.path.exists(dst):
                _create_dir_all(dst.parent, False)
                try:
                    dst.write_bytes(body)
                except OSError as err:
                    raise Error(io_error(err)) from None
                r.created.append(f"{shown}{rel}")


def reveal(c: course.CourseDir, u: Unit) -> Revealed:
    r = Revealed(created=[], kept=[])
    root = c.root
    if u.project is not None:
        pid = u.project
        write_doc(root, c.overview_rel(pid), render.overview_md(c, pid), r)
    write_doc(root, c.subject_rel(u), render.unit_md(c, u), r)
    write_doc(root, Path(rules.FILE), rules.rules_md(c), r)
    for n in u.notes:
        src = c.hidden() / "notes" / f"{n}.md"
        try:
            body = read_text(src)
        except OSError as e:
            raise Error(f"note `{n}`: {io_error(e)}") from None
        write_new(root, f"notes/{n}.md", body, r, "")
    if u.scaffold == Scaffold.NONE:
        return r
    ws_name = c.workspace_name(u)
    ws = c.workspace(u)
    if not os.path.exists(ws):
        _create_dir_all(ws, True)
        r.created.append(f"{ws_name}/")
    copy_scaffold(c.hidden() / "scaffold" / u.id, ws, "", r, f"{ws_name}/")
    language = c.language(u)
    own = engine.protos_for(c, [u])
    # Types with methods in earlier stages exist already.
    earlier = {o for p in engine.protos_for(c, c.chain(u)[:-1]) if (o := p.owner()) is not None}
    # The types the subject gives come with the stubs.
    for x in c.chain(u):
        earlier |= lang.type_names(x.types)
    types = f"{u.types.rstrip()}\n\n" if u.types and u.types.strip() else ""
    stubs = u.stubs
    if stubs is not None and (own or types):
        path = ws / stubs
        if os.path.exists(path):
            body = types + (lang.stub_source(language, own, earlier) or "")
            r.kept.append((f"{ws_name}/{stubs}", body))
        else:
            stub = lang.stub_source(language, own, earlier)
            if stub is not None:
                stub = types + stub
                match language:
                    case "c":
                        header = next(
                            (
                                f.path.rsplit("/", 1)[-1]
                                for f in u.files
                                if f.rule == Rule.REQUIRED
                                and f.path.endswith(".h")
                                and "*" not in f.path
                            ),
                            None,
                        )
                        if header is not None:
                            stub = f'#include "{header}"\n\n{stub}'
                    case _:
                        pass
                write_new(ws, stubs, stub, r, f"{ws_name}/")
    if u.scaffold == Scaffold.FULL:
        chain_protos = engine.protos_for(c, c.chain(u))
        for f in u.files:
            if not (f.rule == Rule.REQUIRED and "*" not in f.path):
                continue
            if language == "c" and f.path.endswith(".h"):
                body = lang.c_header(f.path, chain_protos)
            else:
                body = ""
            write_new(ws, f.path, body, r, f"{ws_name}/")
    return r


def unlock(c: course.CourseDir, u: Unit) -> Revealed:
    """Unlocks `u`: reveals it and records the event."""
    r = reveal(c, u)
    journal.append(c.root, journal.Event(t=journal.now(), ev="unlock", unit=u.id))
    return r
