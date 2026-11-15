"""Append-only event log (`.proctor/journal.jsonl`). PROGRESS.md and the
course state are both derived from it."""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, fields
from pathlib import Path
from typing import Any

from proctor import course
from proctor.compat import U64_MASK, io_error, lines, read_text
from proctor.errors import Error


@dataclass(kw_only=True)
class Event:
    # Unix seconds.
    t: int = 0
    # `unlock`, `check` or `pass`.
    ev: str = ""
    unit: str = ""
    ok: bool | None = None
    passed: int | None = None
    total: int | None = None
    # Check category of the first failure (`structure`, `tests`, ...).
    cat: str | None = None
    # Name of the first failing item.
    item: str | None = None
    msg: str | None = None
    seed: str | None = None
    ms: int | None = None
    # Compiler / linter warnings seen in the build step.
    warnings: int | None = None


# serde's view of `Event`: each field's JSON type, and whether it may be
# absent (`#[serde(default)]`); absent optional fields are not written
# (`skip_serializing_if = "Option::is_none"`).
_FIELDS: list[tuple[str, str, bool]] = [
    ("t", "u64", False),
    ("ev", "str", False),
    ("unit", "str", False),
    ("ok", "bool", True),
    ("passed", "u64", True),
    ("total", "u64", True),
    ("cat", "str", True),
    ("item", "str", True),
    ("msg", "str", True),
    ("seed", "str", True),
    ("ms", "u64", True),
    ("warnings", "u64", True),
]


class _Object(list[tuple[str, Any]]):
    """A JSON object as its key/value pairs, so duplicate keys can be seen."""


class _Invalid(Exception):
    pass


def _reject_constant(name: str) -> Any:
    # serde_json does not read `NaN` or `Infinity`.
    raise _Invalid(name)


def _int(text: str) -> int | float:
    # serde_json reads `-0` as the float -0.0, which no integer field accepts.
    return -0.0 if text == "-0" else int(text)


def _typed(v: Any, kind: str, optional: bool) -> Any:
    if v is None and optional:
        return None
    if kind == "u64":
        if type(v) is int and 0 <= v <= U64_MASK:
            return v
    elif kind == "bool":
        if type(v) is bool:
            return v
    elif kind == "str":
        # serde_json rejects lone surrogates, which Python's json accepts.
        if type(v) is str and not any("\ud800" <= c <= "\udfff" for c in v):
            return v
    raise _Invalid(kind)


def _decode(line: str) -> Event | None:
    """`serde_json::from_str::<Event>(line).ok()`."""
    try:
        v = json.loads(
            line, object_pairs_hook=_Object, parse_constant=_reject_constant, parse_int=_int
        )
    except (ValueError, RecursionError, _Invalid):
        return None
    values: dict[str, Any] = {}
    try:
        if isinstance(v, _Object):
            known = {name: (kind, optional) for name, kind, optional in _FIELDS}
            for key, x in v:
                if key not in known:
                    continue
                if key in values:
                    return None
                values[key] = _typed(x, *known[key])
            if any(name not in values and not optional for name, _, optional in _FIELDS):
                return None
        elif isinstance(v, list):
            # serde also reads a struct from a sequence of its fields, in order.
            if len(v) > len(_FIELDS):
                return None
            for (name, kind, optional), x in zip(_FIELDS, v, strict=False):
                values[name] = _typed(x, kind, optional)
            if any(not optional for _, _, optional in _FIELDS[len(v) :]):
                return None
        else:
            return None
    except _Invalid:
        return None
    return Event(**values)


def _encode(e: Event) -> str:
    """`serde_json::to_string(e)`."""
    obj: dict[str, Any] = {}
    for f in fields(e):
        x = getattr(e, f.name)
        if x is None and f.name not in ("t", "ev", "unit"):
            continue
        obj[f.name] = x
    return json.dumps(obj, separators=(",", ":"), ensure_ascii=False)


def now() -> int:
    t = time.time()
    return int(t) if t >= 0 else 0


def path(root: Path) -> Path:
    return root / course.HIDDEN / "journal.jsonl"


def read(root: Path) -> list[Event]:
    try:
        text = read_text(path(root))
    except OSError:
        return []
    return [e for e in (_decode(line) for line in lines(text)) if e is not None]


def append(root: Path, e: Event) -> None:
    p = path(root)
    line = _encode(e)
    try:
        with open(p, "ab") as f:
            f.write(f"{line}\n".encode())
    except OSError as err:
        raise Error(f"{p}: {io_error(err)}") from None


def civil(days: int) -> tuple[int, int, int]:
    """(year, month, day) for days since 1970-01-01 (proleptic Gregorian)."""
    z = days + 719_468
    era = z // 146_097
    doe = z % 146_097
    yoe = (doe - doe // 1460 + doe // 36_524 - doe // 146_096) // 365
    y = yoe + era * 400
    doy = doe - (365 * yoe + yoe // 4 - yoe // 100)
    mp = (5 * doy + 2) // 153
    d = doy - (153 * mp + 2) // 5 + 1
    m = mp + 3 if mp < 10 else mp - 9
    return (y + 1 if m <= 2 else y, m, d)


def day_of(t: int) -> int:
    return t // 86_400


def date(t: int) -> str:
    y, m, d = civil(day_of(t))
    return f"{y:04}-{m:02}-{d:02}"


def datetime(t: int) -> str:
    s = t % 86_400
    return f"{date(t)} {s // 3600:02}:{(s // 60) % 60:02}"


def week_start(t: int) -> int:
    """Day number of the Monday starting `t`'s week."""
    d = day_of(t)
    # 1970-01-01 was a Thursday: Monday-based weekday = (d + 3) mod 7.
    return d - (d + 3) % 7


def duration(secs: int) -> str:
    h = secs // 3600
    m = (secs // 60) % 60
    match (h, m):
        case (0, 0) if secs > 0:
            return "< 1 min"
        case (0, m):
            return f"{m} min"
        case (h, 0):
            return f"{h} h"
        case (h, m):
            return f"{h} h {m:02}"
    raise AssertionError
