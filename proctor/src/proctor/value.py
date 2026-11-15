"""Dynamic values: what harnesses return, what generators produce, what the
expression language computes with. Also the line-based wire format that
every language harness speaks."""

from __future__ import annotations

import math
from collections.abc import Iterator
from dataclasses import dataclass

from proctor.compat import (
    debug_char,
    debug_str,
    float_display,
    float_exp,
    from_utf8_lossy,
    parse_float,
    parse_int,
    parse_uint,
    split_ascii_whitespace,
)
from proctor.errors import Error


class Value:
    """Base of the value variants (`ValueNull`, `ValueInt`, ...)."""

    __slots__ = ()

    def type_name(self) -> str:
        match self:
            case ValueNull():
                return "null"
            case ValueBool():
                return "bool"
            case ValueInt():
                return "int"
            case ValueFloat():
                return "float"
            case ValueStr():
                return "string"
            case ValueChar():
                return "char"
            case ValueList():
                return "list"
            case ValueMap():
                return "map"
            case _:
                return "object"

    def truthy(self) -> bool:
        match self:
            case ValueNull():
                return False
            case ValueBool(b):
                return b
            case ValueInt(i):
                return i != 0
            case ValueFloat(f):
                return f != 0.0
            case ValueStr(s):
                return s != ""
            case ValueList(items):
                return len(items) > 0
            case ValueMap(pairs):
                return len(pairs) > 0
            case _:
                return True

    def as_f64(self) -> float | None:
        match self:
            case ValueInt(i):
                return float(i)
            case ValueFloat(f):
                return f
            case _:
                return None

    def text(self) -> str | None:
        """Text content of strings, chars and opaque reprs."""
        match self:
            case ValueStr(s) | ValueOpaque(s):
                return s
            case ValueChar(c):
                return c
            case _:
                return None

    def rank(self) -> int:
        match self:
            case ValueNull():
                return 0
            case ValueBool():
                return 1
            case ValueInt() | ValueFloat():
                return 2
            case ValueChar() | ValueStr() | ValueOpaque():
                return 3
            case ValueList():
                return 4
            case _:
                return 5

    def cmp_values(self, other: Value) -> int:
        """Total order used by `sorted`, `min`, `max` and `<`: -1, 0 or 1."""
        match (self, other):
            case (ValueInt(a), ValueInt(b)):
                return _cmp(a, b)
            case (ValueBool(a), ValueBool(b)):
                return _cmp(a, b)
            case (ValueList(a), ValueList(b)):
                for x, y in zip(a, b, strict=False):
                    o = x.cmp_values(y)
                    if o != 0:
                        return o
                return _cmp(len(a), len(b))
        fa, fb = self.as_f64(), other.as_f64()
        if fa is not None and fb is not None:
            # partial_cmp: NaN compares equal to anything.
            if fa < fb:
                return -1
            if fa > fb:
                return 1
            return 0
        ta, tb = self.text(), other.text()
        if ta is not None and tb is not None:
            return _cmp(ta, tb)
        return _cmp(self.rank(), other.rank())

    def short(self, max: int) -> str:
        """Display form, cut to roughly `max` characters."""
        s = str(self)
        if len(s) <= max:
            return s
        return s[:max] + "..."

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, Value):
            return NotImplemented
        return _eq(self, other)

    def __ne__(self, other: object) -> bool:
        if not isinstance(other, Value):
            return NotImplemented
        return not _eq(self, other)

    __hash__ = None  # type: ignore[assignment]

    def __str__(self) -> str:
        match self:
            case ValueNull():
                return "null"
            case ValueBool(b):
                return "true" if b else "false"
            case ValueInt(i):
                return str(i)
            case ValueFloat(x):
                if math.isfinite(x) and x == math.trunc(x) and abs(x) < 1e15:
                    return f"{x:.1f}"
                return float_display(x)
            case ValueStr(s):
                return debug_str(s)
            case ValueChar(c):
                return debug_char(c)
            case ValueOpaque(s):
                return s
            case ValueList(items):
                return "[" + ", ".join(str(v) for v in items) + "]"
            case ValueMap(pairs):
                return "{" + ", ".join(f"{k}: {v}" for k, v in pairs) + "}"
        raise AssertionError(type(self))

    def __repr__(self) -> str:
        return f"{type(self).__name__}({self})"


@dataclass(frozen=True, eq=False, repr=False, slots=True)
class ValueNull(Value):
    pass


@dataclass(frozen=True, eq=False, repr=False, slots=True)
class ValueBool(Value):
    b: bool


@dataclass(frozen=True, eq=False, repr=False, slots=True)
class ValueInt(Value):
    """An integer (i128 in the Rust tools)."""

    i: int


@dataclass(frozen=True, eq=False, repr=False, slots=True)
class ValueFloat(Value):
    f: float


@dataclass(frozen=True, eq=False, repr=False, slots=True)
class ValueStr(Value):
    s: str


@dataclass(frozen=True, eq=False, repr=False, slots=True)
class ValueChar(Value):
    """One Unicode scalar value (a one-character `str`)."""

    c: str


@dataclass(frozen=True, eq=False, repr=False, slots=True)
class ValueList(Value):
    items: list[Value]


@dataclass(frozen=True, eq=False, repr=False, slots=True)
class ValueMap(Value):
    """Key/value pairs in order; equality ignores the order."""

    pairs: list[tuple[Value, Value]]


@dataclass(frozen=True, eq=False, repr=False, slots=True)
class ValueOpaque(Value):
    """A value the harness could only describe (e.g. a Python object repr)."""

    s: str


NULL = ValueNull()


def _cmp(a: object, b: object) -> int:
    return (a > b) - (a < b)  # type: ignore[operator]


def _float_eq(a: float, b: float) -> bool:
    if math.isnan(a) and math.isnan(b):
        return True
    if a == b:
        return True
    scale = max(abs(a), abs(b), 1.0)
    return abs(a - b) <= 1e-9 * scale


def _eq(a: Value, b: Value) -> bool:
    match (a, b):
        case (ValueNull(), ValueNull()):
            return True
        case (ValueBool(x), ValueBool(y)):
            return x == y
        case (ValueInt(x), ValueInt(y)):
            return x == y
        case (ValueFloat(x), ValueFloat(y)):
            return _float_eq(x, y)
        case (ValueInt(i), ValueFloat(f)) | (ValueFloat(f), ValueInt(i)):
            return _float_eq(float(i), f)
        case (ValueStr(x), ValueStr(y)):
            return x == y
        case (ValueChar(x), ValueChar(y)):
            return x == y
        case (ValueChar(c), ValueStr(s)) | (ValueStr(s), ValueChar(c)):
            return s == c
        case (
            (ValueOpaque(x), ValueOpaque(y))
            | (ValueOpaque(x), ValueStr(y))
            | (ValueStr(y), ValueOpaque(x))
        ):
            return x == y
        case (ValueList(x), ValueList(y)):
            return len(x) == len(y) and all(_eq(p, q) for p, q in zip(x, y, strict=True))
        case (ValueMap(x), ValueMap(y)):
            return len(x) == len(y) and all(
                any(_eq(k, k2) and _eq(v, v2) for k2, v2 in y) for k, v in x
            )
    return False


# ------------------------------------------------------------------ wire format
#
# One value is a sequence of space-separated tokens:
#   n            null            b0 / b1      bool
#   i<int>       integer         f<float>     float (Rust `{:e}` / C `%.17g`)
#   s<hex>       UTF-8 string    c<code>      char (Unicode scalar)
#   l<n> ...     list of n values that follow
#   m<n> ...     map: n key/value pairs that follow
#   o<hex>       opaque repr
# Requests:  `call <name> <nargs> <values...>`
# Responses: `ok <value>` | `exc <hex type> <hex message>` | `err <hex message>`

_HEX = "0123456789abcdefABCDEF"


def hex(data: bytes) -> str:  # noqa: A001
    return data.hex()


def unhex(s: str) -> bytes:
    if len(s.encode("utf-8")) % 2 != 0:
        raise Error(f"odd hex length in `{s}`")
    out = bytearray()
    for i in range(0, len(s), 2):
        pair = s[i : i + 2]
        if len(pair) != 2 or pair[0] not in _HEX or pair[1] not in _HEX:
            raise Error("bad hex: invalid digit found in string")
        out.append(int(pair, 16))
    return bytes(out)


def unhex_text(s: str) -> str:
    try:
        return from_utf8_lossy(unhex(s))
    except Error:
        return s


def encode(v: Value) -> str:
    out: list[str] = []
    _enc(v, out)
    return "".join(out)


def _enc(v: Value, out: list[str]) -> None:
    match v:
        case ValueNull():
            out.append("n")
        case ValueBool(b):
            out.append("b1" if b else "b0")
        case ValueInt(i):
            out.append(f"i{i}")
        case ValueFloat(f):
            out.append("f" + float_exp(f))
        case ValueStr(s):
            out.append("s" + s.encode("utf-8").hex())
        case ValueChar(c):
            out.append(f"c{ord(c)}")
        case ValueOpaque(s):
            out.append("o" + s.encode("utf-8").hex())
        case ValueList(items):
            out.append(f"l{len(items)}")
            for x in items:
                out.append(" ")
                _enc(x, out)
        case ValueMap(pairs):
            out.append(f"m{len(pairs)}")
            for k, x in pairs:
                out.append(" ")
                _enc(k, out)
                out.append(" ")
                _enc(x, out)


def _parse_wire_float(s: str) -> float:
    f = parse_float(s)
    if f is not None:
        return f
    low = s.lower()
    if "nan" in low:
        return math.nan
    if low.startswith("-"):
        return -math.inf
    return math.inf


def decode(it: Iterator[str]) -> Value:
    t = next(it, None)
    if t is None:
        raise Error("truncated value")
    tag, rest = t[:1], t[1:]
    match tag:
        case "n":
            return NULL
        case "b":
            return ValueBool(rest == "1")
        case "i":
            i = parse_int(rest)
            return ValueInt(i) if i is not None else ValueOpaque(rest)
        case "f":
            return ValueFloat(_parse_wire_float(rest))
        case "s":
            return ValueStr(from_utf8_lossy(unhex(rest)))
        case "c":
            code = parse_uint(rest, (1 << 32) - 1)
            ok = code is not None and code <= 0x10FFFF and not 0xD800 <= code <= 0xDFFF
            return ValueChar(chr(code) if ok and code is not None else "\ufffd")
        case "o":
            return ValueOpaque(from_utf8_lossy(unhex(rest)))
        case "l":
            n = parse_uint(rest)
            if n is None:
                raise Error(f"bad list length `{rest}`")
            return ValueList([decode(it) for _ in range(n)])
        case "m":
            n = parse_uint(rest)
            if n is None:
                raise Error(f"bad map length `{rest}`")
            pairs: list[tuple[Value, Value]] = []
            for _ in range(n):
                k = decode(it)
                pairs.append((k, decode(it)))
            return ValueMap(pairs)
    raise Error(f"unknown value tag in `{t}`")


def decode_str(s: str) -> Value:
    it = iter(split_ascii_whitespace(s))
    v = decode(it)
    if next(it, None) is not None:
        raise Error("trailing tokens after value")
    return v
