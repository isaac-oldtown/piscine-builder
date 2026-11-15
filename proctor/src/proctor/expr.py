"""The small expression language used by function cases and laws:
  str_len("abc") == 3
  join(nonempty(split(s, ",")), ",") == join(str_split(s, ','), ",")
  raises(parse(""))
Calls resolve to the student's functions (through the harness), to
`run(...)` for programs, or to built-ins. There are no user-defined
functions or lambdas, so a spec cannot smuggle in a reference solution."""

from __future__ import annotations

import abc
import enum
import functools
import math
import re
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import NoReturn

from proctor import compat
from proctor.compat import I128_MAX, I128_MIN, U64_MASK, byte_len, is_whitespace
from proctor.errors import Error
from proctor.value import (
    NULL,
    Value,
    ValueBool,
    ValueChar,
    ValueFloat,
    ValueInt,
    ValueList,
    ValueMap,
    ValueNull,
    ValueOpaque,
    ValueStr,
)

_U32_MAX = (1 << 32) - 1


def _wrap_i128(r: int) -> int:
    """An i128 operation Rust does not check (release builds wrap)."""
    r &= (1 << 128) - 1
    return r - (1 << 128) if r > I128_MAX else r


def _checked_i128(r: int) -> int | None:
    """Rust's `checked_*` on i128: `None` outside the i128 range."""
    return r if I128_MIN <= r <= I128_MAX else None


def _to_digit(c: str, radix: int) -> int | None:
    """Rust `char::to_digit`: ASCII digits and letters only."""
    if "0" <= c <= "9":
        d = ord(c) - ord("0")
    elif "a" <= c <= "z":
        d = ord(c) - ord("a") + 10
    elif "A" <= c <= "Z":
        d = ord(c) - ord("A") + 10
    else:
        return None
    return d if d < radix else None


def _from_str_radix(s: str, radix: int, hi: int) -> int | None:
    """Rust `uN::from_str_radix`: an optional `+`, then at least one digit."""
    if s.startswith("+"):
        s = s[1:]
    if not s:
        return None
    v = 0
    for c in s:
        d = _to_digit(c, radix)
        if d is None:
            return None
        v = v * radix + d
    return v if v <= hi else None


class Tok:
    __slots__ = ()


@dataclass(frozen=True)
class TokInt(Tok):
    f0: int


@dataclass(frozen=True)
class TokFloat(Tok):
    f0: float


@dataclass(frozen=True)
class TokStr(Tok):
    f0: str


@dataclass(frozen=True)
class TokChar(Tok):
    f0: str


@dataclass(frozen=True)
class TokIdent(Tok):
    f0: str


@dataclass(frozen=True)
class TokP(Tok):
    f0: str


PUNCT3: list[tuple[str, str]] = [("===", "=="), ("!==", "!=")]
PUNCT2: list[str] = ["==", "!=", "<=", ">=", "&&", "||", "::"]
PUNCT1: list[str] = [
    "(",
    ")",
    "[",
    "]",
    "{",
    "}",
    ",",
    ":",
    "=",
    "<",
    ">",
    "!",
    "+",
    "-",
    "*",
    "/",
    "%",
    ".",
    "&",
]


def hex_digits(cs: str, i: int, n: int, what: str) -> tuple[int, int]:
    if i + n > len(cs):
        raise Error(f"bad {what} escape")
    h = cs[i : i + n]
    i += n
    v = _from_str_radix(h, 16, _U32_MAX)
    if v is None:
        raise Error(f"bad {what} escape")
    return v, i


def _scalar(n: int) -> str:
    """Rust `char::from_u32`, with the error message of `read_escape`."""
    if n > 0x10FFFF or 0xD800 <= n <= 0xDFFF:
        raise Error(f"bad code point {n:#x}")
    return chr(n)


def read_escape(cs: str, i: int) -> tuple[str, int]:
    """Escapes of the common languages: `\\n \\t \\r \\0 \\a \\b \\f \\v \\e \\\\ \\" \\'`,
    octal `\\NNN`, `\\xHH`, `\\uXXXX` (with surrogate pairs), `\\u{X..}`, `\\UXXXXXXXX`."""
    if i >= len(cs):
        raise Error("unterminated escape")
    c = cs[i]
    i += 1
    match c:
        case "n":
            return "\n", i
        case "t":
            return "\t", i
        case "r":
            return "\r", i
        case "a":
            return "\x07", i
        case "b":
            return "\x08", i
        case "f":
            return "\x0c", i
        case "v":
            return "\x0b", i
        case "e":
            return "\x1b", i
        case "\\":
            return "\\", i
        case '"':
            return '"', i
        case "'":
            return "'", i
        case "`":
            return "`", i
        case "/":
            return "/", i
        case "0" | "1" | "2" | "3" | "4" | "5" | "6" | "7":
            n = ord(c) - ord("0")
            for _ in range(2):
                d = _to_digit(cs[i], 8) if i < len(cs) else None
                if d is None:
                    break
                n = n * 8 + d
                i += 1
            return _scalar(n), i
        case "x":
            v, i = hex_digits(cs, i, 2, "\\x")
            return chr(v & 0xFF), i
        case "U":
            v, i = hex_digits(cs, i, 8, "\\U")
            return _scalar(v), i
        case "u" if cs[i : i + 1] == "{":
            end = cs.find("}", i)
            if end < 0:
                raise Error("bad \\u escape")
            h = cs[i + 1 : end]
            i = end + 1
            v2 = _from_str_radix(h, 16, _U32_MAX)
            if v2 is None:
                raise Error("bad \\u escape")
            return _scalar(v2), i
        case "u":
            hi, i = hex_digits(cs, i, 4, "\\u")
            if 0xD800 <= hi < 0xDC00 and cs[i : i + 1] == "\\" and cs[i + 1 : i + 2] == "u":
                j = i + 2
                lo, j = hex_digits(cs, j, 4, "\\u")
                if 0xDC00 <= lo < 0xE000:
                    i = j
                    return _scalar(0x10000 + ((hi - 0xD800) << 10) + (lo - 0xDC00)), i
            return _scalar(hi), i
        case other:
            raise Error(f"unknown escape \\{other}")


def read_quoted(cs: str, i: int) -> tuple[str, int]:
    """Reads a string quoted by `cs[i]` (`"` or `'`), with escapes."""
    q = cs[i]
    i += 1
    s: list[str] = []
    while True:
        if i >= len(cs):
            raise Error("unterminated string")
        c = cs[i]
        if c == q:
            i += 1
            return "".join(s), i
        if c == "\\":
            i += 1
            ch, i = read_escape(cs, i)
            s.append(ch)
        else:
            s.append(c)
            i += 1


def read_string(cs: str, i: int) -> tuple[str, int]:
    """Reads a "double-quoted" string starting at `cs[i] == '"'`."""
    return read_quoted(cs, i)


def read_raw(cs: str, i: int) -> tuple[str, int]:
    """Rust raw string at `cs[i]` (after the `r`): `"..."` or `#"..."#`."""
    hashes = 0
    while cs[i : i + 1] == "#":
        hashes += 1
        i += 1
    if cs[i : i + 1] != '"':
        raise Error("bad raw string")
    i += 1
    start = i
    while i < len(cs):
        if cs[i] == '"' and cs[i + 1 : i + 1 + hashes] == "#" * hashes:
            s = cs[start:i]
            i += 1 + hashes
            return s, i
        i += 1
    raise Error("unterminated raw string")


# Type suffixes on number literals (`10u8`, `2.5f64`, `10L`, `10n`).
NUM_SUFFIXES: list[str] = [
    "u8",
    "u16",
    "u32",
    "u64",
    "u128",
    "usize",
    "i8",
    "i16",
    "i32",
    "i64",
    "i128",
    "isize",
    "f32",
    "f64",
    "L",
    "l",
    "u",
    "U",
    "UL",
    "ul",
    "LL",
    "ll",
    "ULL",
    "ull",
    "n",
    "f",
    "F",
    "d",
    "D",
]


def read_number(cs: str, i: int) -> tuple[Tok, int]:
    start = i
    nxt = cs[i + 1 : i + 2]
    if cs[i] == "0" and nxt in ("x", "X"):
        radix = 16
    elif cs[i] == "0" and nxt in ("b", "B"):
        radix = 2
    elif cs[i] == "0" and nxt in ("o", "O"):
        radix = 8
    else:
        radix = 10
    if radix != 10:
        i += 2
        s = i
        while i < len(cs) and (_to_digit(cs[i], radix) is not None or cs[i] == "_"):
            i += 1
        digits = cs[s:i].replace("_", "")
        v = _from_str_radix(digits, radix, I128_MAX)
        if v is None:
            raise Error("bad number")
        _, i = skip_suffix(cs, i)
        return TokInt(v), i
    while i < len(cs) and ("0" <= cs[i] <= "9" or cs[i] == "_"):
        i += 1
    float = False
    if i + 1 < len(cs) and cs[i] == "." and "0" <= cs[i + 1] <= "9":
        float = True
        i += 1
        while i < len(cs) and ("0" <= cs[i] <= "9" or cs[i] == "_"):
            i += 1
    if i < len(cs) and (cs[i] == "e" or cs[i] == "E"):
        j = i + 1
        if j < len(cs) and (cs[j] == "+" or cs[j] == "-"):
            j += 1
        if j < len(cs) and "0" <= cs[j] <= "9":
            float = True
            i = j
            while i < len(cs) and "0" <= cs[i] <= "9":
                i += 1
    text = cs[start:i].replace("_", "")
    suffix, i = skip_suffix(cs, i)
    float |= suffix in ("f32", "f64", "f", "F", "d", "D")
    if float:
        f = compat.parse_float(text)
        if f is None:
            raise Error(f"bad number `{text}`")
        return TokFloat(f), i
    n = compat.parse_int(text)
    if n is None:
        raise Error(f"bad number `{text}`")
    return TokInt(n), i


def skip_suffix(cs: str, i: int) -> tuple[str, int]:
    """Skips a known type suffix right after a number; returns it."""
    j = i
    while j < len(cs) and ((cs[j].isascii() and cs[j].isalnum()) or cs[j] == "_"):
        j += 1
    word = cs[i:j]
    clean = word.lstrip("_")
    if clean in NUM_SUFFIXES:
        i += len(word)
        return clean, i
    return "", i


def lex(src: str) -> list[Tok]:
    cs = src
    i = 0
    out: list[Tok] = []
    while i < len(cs):
        c = cs[i]
        if is_whitespace(c):
            i += 1
        elif "0" <= c <= "9":
            t, i = read_number(cs, i)
            out.append(t)
        elif c == '"' or c == "`":
            s, i = read_quoted(cs, i)
            out.append(TokStr(s))
        elif c == "'":
            # One character is a char (Rust, C, Java); more is a string (Python, JS).
            s, i = read_quoted(cs, i)
            out.append(TokChar(s) if len(s) == 1 else TokStr(s))
        elif (
            (c == "r" or c == "R")
            and cs[i + 1 :].lstrip("#")[:1] == '"'
            and not (i > 0 and (cs[i - 1].isalnum() or cs[i - 1] == "_"))
        ):
            i += 1
            s, i = read_raw(cs, i)
            out.append(TokStr(s))
        elif c == "b" and cs[i + 1 : i + 2] == '"':
            i += 1
            s, i = read_quoted(cs, i)
            out.append(TokStr(s))
        elif c.isalpha() or c == "_" or c == "$":
            start = i
            while i < len(cs):
                d = cs[i]
                dotted = d == "." and i + 1 < len(cs) and (cs[i + 1].isalpha() or cs[i + 1] == "_")
                if d.isalnum() or d == "_" or d == "$" or dotted:
                    i += 1
                else:
                    break
            word = cs[start:i]
            # Word operators of Python and friends.
            match word:
                case "and":
                    out.append(TokP("&&"))
                case "or":
                    out.append(TokP("||"))
                case "in" if out and out[-1] == TokP("!"):
                    out.pop()
                    out.append(TokP("notin"))
                case "not":
                    out.append(TokP("!"))
                case "in":
                    out.append(TokP("in"))
                case "is":
                    j = i
                    while j < len(cs) and is_whitespace(cs[j]):
                        j += 1
                    negated = (
                        cs[j : j + 3] == "not" and j + 3 < len(cs) and is_whitespace(cs[j + 3])
                    )
                    if negated:
                        i = j + 3
                        out.append(TokP("!="))
                    else:
                        out.append(TokP("=="))
                case _:
                    out.append(TokIdent(word))
        else:
            three = cs[i : i + 3]
            two = cs[i : i + 2]
            p3 = next((p for s3, p in PUNCT3 if s3 == three), None)
            if p3 is not None:
                out.append(TokP(p3 if p3 in PUNCT2 else "=="))
                i += 3
            elif two in PUNCT2:
                out.append(TokP(two))
                i += 2
            elif c in PUNCT1:
                out.append(TokP(c))
                i += 1
            else:
                raise Error(f"unexpected character `{c}`")
    return out


class BinOp(enum.Enum):
    OR = enum.auto()
    AND = enum.auto()
    EQ = enum.auto()
    NE = enum.auto()
    LT = enum.auto()
    LE = enum.auto()
    GT = enum.auto()
    GE = enum.auto()
    ADD = enum.auto()
    SUB = enum.auto()
    MUL = enum.auto()
    DIV = enum.auto()
    REM = enum.auto()


class Expr:
    """Base of the expression variants (`ExprLit`, `ExprName`, ...)."""

    __slots__ = ()


@dataclass(frozen=True)
class ExprLit(Expr):
    value: Value


@dataclass(frozen=True)
class ExprName(Expr):
    name: str


@dataclass(frozen=True)
class ExprCall(Expr):
    name: str
    args: list[tuple[str | None, Expr]]


@dataclass(frozen=True)
class ExprList(Expr):
    items: list[Expr]


@dataclass(frozen=True)
class ExprMap(Expr):
    pairs: list[tuple[Expr, Expr]]


@dataclass(frozen=True)
class ExprNot(Expr):
    inner: Expr


@dataclass(frozen=True)
class ExprNeg(Expr):
    inner: Expr


@dataclass(frozen=True)
class ExprBin(Expr):
    op: BinOp
    left: Expr
    right: Expr


@dataclass(frozen=True)
class ExprField(Expr):
    target: Expr
    name: str


@dataclass(frozen=True)
class ExprIndex(Expr):
    target: Expr
    index: Expr


# Constructors that only wrap a value in some language: `Ok(v)` is `v`
# (harnesses unwrap them), `String::from("a")` is `"a"`.
WRAPPERS: list[str] = [
    "Ok",
    "Some",
    "Just",
    "Right",
    "Box::new",
    "Rc::new",
    "Arc::new",
    "RefCell::new",
    "Cell::new",
    "Cow::from",
    "String::from",
    "str::to_string",
    "Vec::from",
    "HashSet::from",
    "BTreeSet::from",
    "VecDeque::from",
    "HashMap::from",
    "BTreeMap::from",
    "std::string::String::from",
]

# Error results: a call compared with one of these must raise.
ERR_WRAPPERS: list[str] = ["Err", "Left"]


def empty_constructor(name: str) -> Value | None:
    """Empty constructors: `Vec::new()` is `[]`."""
    if name.endswith("::new"):
        ty = name.removesuffix("::new")
    elif name.endswith("::default"):
        ty = name.removesuffix("::default")
    else:
        return None
    ty = ty.rsplit("::", 1)[-1]
    match ty:
        case "String":
            return ValueStr("")
        case "Vec" | "VecDeque" | "HashSet" | "BTreeSet" | "LinkedList" | "BinaryHeap":
            return ValueList([])
        case "HashMap" | "BTreeMap":
            return ValueMap([])
        case _:
            return None


def is_err_wrapper(e: Expr) -> bool:
    return isinstance(e, ExprCall) and e.name in ERR_WRAPPERS


LEVELS: list[list[tuple[str, BinOp]]] = [
    [("||", BinOp.OR)],
    [("&&", BinOp.AND)],
    [
        ("==", BinOp.EQ),
        ("!=", BinOp.NE),
        ("<=", BinOp.LE),
        (">=", BinOp.GE),
        ("<", BinOp.LT),
        (">", BinOp.GT),
    ],
    [("+", BinOp.ADD), ("-", BinOp.SUB)],
    [("*", BinOp.MUL), ("/", BinOp.DIV), ("%", BinOp.REM)],
]


@dataclass(kw_only=True)
class Parser:
    toks: list[Tok]
    pos: int

    def peek(self) -> Tok | None:
        return self.toks[self.pos] if self.pos < len(self.toks) else None

    def is_(self, p: str) -> bool:
        t = self.peek()
        return isinstance(t, TokP) and t.f0 == p

    def eat(self, p: str) -> bool:
        if self.is_(p):
            self.pos += 1
            return True
        return False

    def expect(self, p: str) -> None:
        if not self.eat(p):
            raise Error(f"expected `{p}`")

    def expr(self) -> Expr:
        return self.binary(0)

    def binary(self, level: int) -> Expr:
        if level == len(LEVELS):
            return self.unary()
        left = self.binary(level + 1)
        while True:
            # `x in xs` / `x not in xs` (Python).
            if level == 2 and (self.is_("in") or self.is_("notin")):
                negated = self.is_("notin")
                self.pos += 1
                right = self.binary(level + 1)
                c = ExprCall("contains", [(None, right), (None, left)])
                return ExprNot(c) if negated else c
            op = next((op for p, op in LEVELS[level] if self.is_(p)), None)
            if op is None:
                return left
            self.pos += 1
            right = self.binary(level + 1)
            # `f(x) == Err(...)`: the call must raise (the payload is not compared:
            # error values differ between correct solutions).
            if op in (BinOp.EQ, BinOp.NE) and (is_err_wrapper(right) or is_err_wrapper(left)):
                call = left if is_err_wrapper(right) else right
                raises = ExprCall("raises", [(None, call)])
                return raises if op == BinOp.EQ else ExprNot(raises)
            left = ExprBin(op, left, right)
            # Comparisons do not chain.
            if level == 2:
                return left

    def unary(self) -> Expr:
        if self.eat("!"):
            return ExprNot(self.unary())
        if self.eat("-"):
            return ExprNeg(self.unary())
        if self.eat("+"):
            return self.unary()
        # Borrows and dereferences carry no value: `&[1, 2]`, `&mut v`, `*p`.
        if self.eat("&") or self.eat("*"):
            t = self.peek()
            if isinstance(t, TokIdent) and t.f0 == "mut":
                self.pos += 1
            return self.unary()
        if self.eat("&&"):
            return self.unary()
        return self.postfix()

    def call_args(self) -> list[tuple[str | None, Expr]]:
        args: list[tuple[str | None, Expr]] = []
        while not self.eat(")"):
            following = self.toks[self.pos + 1] if self.pos + 1 < len(self.toks) else None
            named: str | None = None
            match (self.peek(), following):
                case (TokIdent(n), TokP("=" | ":")):
                    named = n
            if named is not None:
                self.pos += 2
            args.append((named, self.expr()))
            if not self.eat(","):
                self.expect(")")
                break
        return args

    def postfix(self) -> Expr:
        e = self.primary()
        while True:
            if self.eat("."):
                match self.peek():
                    case TokIdent(f):
                        self.pos += 1
                        # The lexer joins `a.b.c`; split it back into steps.
                        parts = f.split(".")
                        last = parts.pop()
                        for p in parts:
                            e = ExprField(e, p)
                        if self.eat("("):
                            args = [a for _, a in self.call_args()]
                            e = method(e, last, args)
                        else:
                            e = ExprField(e, last)
                    # Rust tuple fields: `pair.0`.
                    case TokInt(n):
                        self.pos += 1
                        e = ExprIndex(e, ExprLit(ValueInt(n)))
                    case _:
                        raise Error("expected a field name after `.`")
            elif self.eat("["):
                i = self.expr()
                self.expect("]")
                e = ExprIndex(e, i)
            else:
                return e

    def list_items(self, close: str) -> list[Expr]:
        items: list[Expr] = []
        while not self.eat(close):
            items.append(self.expr())
            if not self.eat(","):
                self.expect(close)
                break
        return items

    def primary(self) -> Expr:
        t = self.peek()
        if t is None:
            raise Error("unexpected end of expression")
        self.pos += 1
        match t:
            case TokInt(i):
                return ExprLit(ValueInt(i))
            case TokFloat(f):
                return ExprLit(ValueFloat(f))
            case TokStr(s):
                return ExprLit(ValueStr(s))
            case TokChar(c):
                return ExprLit(ValueChar(c))
            case TokIdent(first):
                # Paths: `String::from`, `std::collections::HashMap::new`.
                name = first
                while self.is_("::"):
                    following = self.toks[self.pos + 1] if self.pos + 1 < len(self.toks) else None
                    match following:
                        case TokIdent(seg):
                            self.pos += 2
                            name = f"{name}::{seg}"
                        # Turbofish `::<T>` and the like: not supported, say so.
                        case _:
                            raise Error(f"unexpected `::` after `{name}`")
                if name in ("true", "True", "TRUE"):
                    return ExprLit(ValueBool(True))
                if name in ("false", "False", "FALSE"):
                    return ExprLit(ValueBool(False))
                if name in (
                    "null",
                    "None",
                    "NULL",
                    "nil",
                    "Nil",
                    "undefined",
                    "Nothing",
                    "nullptr",
                ):
                    return ExprLit(NULL)
                if name == "vec" and self.is_("!"):
                    self.pos += 1
                    if self.eat("["):
                        return ExprList(self.list_items("]"))
                    if self.eat("("):
                        return ExprList(self.list_items(")"))
                    raise Error("expected `vec![...]`")
                if self.eat("("):
                    args = self.call_args()
                    plain = all(n is None for n, _ in args)
                    if plain and len(args) == 1 and name in WRAPPERS:
                        return args[0][1]
                    if not args and (v := empty_constructor(name)) is not None:
                        return ExprLit(v)
                    # is_err(x) and friends, as free functions.
                    if (
                        plain
                        and len(args) == 1
                        and name in ("is_err", "is_ok", "is_some", "is_none")
                    ):
                        return method(args[0][1], name, [])
                    return ExprCall(name, args)
                return ExprName(name)
            # Tuples are lists; `()` is the unit value, i.e. nothing.
            case TokP("("):
                if self.eat(")"):
                    return ExprLit(NULL)
                first_item = self.expr()
                if self.eat(")"):
                    return first_item
                self.expect(",")
                items = [first_item]
                items.extend(self.list_items(")"))
                return ExprList(items)
            case TokP("["):
                return ExprList(self.list_items("]"))
            case TokP("{"):
                pairs: list[tuple[Expr, Expr]] = []
                while not self.eat("}"):
                    k = self.expr()
                    self.expect(":")
                    # `{a: 1}` (JS): a bare word key is a string.
                    if isinstance(k, ExprName):
                        k = ExprLit(ValueStr(k.name))
                    pairs.append((k, self.expr()))
                    if not self.eat(","):
                        self.expect("}")
                        break
                return ExprMap(pairs)
            case TokP(p):
                raise Error(f"unexpected `{p}`")
        raise AssertionError(t)


# Methods that only convert or borrow: `x.clone()` is `x`.
IDENTITY_METHODS: list[str] = [
    "clone",
    "to_owned",
    "into",
    "as_str",
    "as_ref",
    "as_slice",
    "as_deref",
    "borrow",
    "to_vec",
    "iter",
    "into_iter",
    "collect",
    "cloned",
    "copied",
    "unwrap",
    "expect",
    "valueOf",
]

# Other languages' method names for the built-ins.
METHOD_ALIASES: list[tuple[str, str]] = [
    ("length", "len"),
    ("size", "len"),
    ("to_string", "str"),
    ("toString", "str"),
    ("to_lowercase", "lower"),
    ("to_ascii_lowercase", "lower"),
    ("toLowerCase", "lower"),
    ("to_uppercase", "upper"),
    ("to_ascii_uppercase", "upper"),
    ("toUpperCase", "upper"),
    ("strip", "trim"),
    ("split_whitespace", "words"),
    ("startswith", "starts_with"),
    ("startsWith", "starts_with"),
    ("endswith", "ends_with"),
    ("endsWith", "ends_with"),
    ("includes", "contains"),
    ("contains_key", "contains"),
    ("rev", "reversed"),
]


def method(recv: Expr, name: str, args: list[Expr]) -> Expr:
    """`recv.name(args)` becomes a built-in call: `s.len()` is `len(s)`,
    `r.is_err()` is `raises(r)`. Only built-ins: a student's function is
    called as `f(x)`."""

    def no_args(args: list[Expr]) -> None:
        if args:
            raise Error(f"`.{name}()` takes no arguments")

    if name in IDENTITY_METHODS:
        return recv
    match name:
        case "is_err" | "is_ok":
            no_args(args)
            r = ExprCall("raises", [(None, recv)])
            return r if name == "is_err" else ExprNot(r)
        case "is_some" | "is_none":
            no_args(args)
            op = BinOp.EQ if name == "is_none" else BinOp.NE
            return ExprBin(op, recv, ExprLit(NULL))
        case "is_empty":
            no_args(args)
            length = ExprCall("len", [(None, recv)])
            return ExprBin(BinOp.EQ, length, ExprLit(ValueInt(0)))
    target = next((b for m, b in METHOD_ALIASES if m == name), name)
    callable = target in BUILTINS and target not in (
        "raises",
        "then",
        "Ok",
        "Some",
        "range",
        "approx",
    )
    if not callable:
        raise Error(
            f"unknown method `.{name}()`: only built-ins work as methods (`s.len()` is len(s)); call your own functions as {name}(x)"
        )
    args = [recv, *args]
    return ExprCall(target, [(None, a) for a in args])


def stray_err(e: Expr) -> str | None:
    """`Err(...)` left anywhere but beside `==` / `!=`."""
    found: str | None = None

    def visit(x: Expr) -> None:
        nonlocal found
        if found is None and isinstance(x, ExprCall) and x.name in ERR_WRAPPERS:
            n = x.name
            found = f"`{n}(...)` only works beside `==` or `!=` (f(x) == {n}(...) means the call fails); otherwise write raises(f(x))"

    walk(e, visit)
    return found


def walk(e: Expr, f: Callable[[Expr], None]) -> None:
    """Visits every node of `e`."""
    f(e)
    match e:
        case ExprCall(_, args):
            for _, a in args:
                walk(a, f)
        case ExprList(items):
            for x in items:
                walk(x, f)
        case ExprMap(pairs):
            for k, v in pairs:
                walk(k, f)
                walk(v, f)
        case ExprNot(x) | ExprNeg(x) | ExprField(x, _):
            walk(x, f)
        case ExprBin(_, a, b) | ExprIndex(a, b):
            walk(a, f)
            walk(b, f)
        case ExprLit() | ExprName():
            pass


def parse(src: str) -> Expr:
    p = Parser(toks=lex(src), pos=0)
    e = p.expr()
    if p.pos != len(p.toks):
        raise Error("unexpected text after the expression")
    m = stray_err(e)
    if m is not None:
        raise Error(m)
    return e


class EvalError(Error):
    """Base of `EvalErrorStudent` and `EvalErrorSpec`."""

    __match_args__ = ("msg",)


class EvalErrorStudent(EvalError):
    """The student's code failed (raised, crashed, timed out)."""


class EvalErrorSpec(EvalError):
    """The spec itself is wrong (unknown name, type error in a built-in)."""


Args = list[tuple[str | None, Value]]


class Host(abc.ABC):
    @abc.abstractmethod
    def var(self, name: str) -> Value | None: ...

    @abc.abstractmethod
    def call(self, name: str, args: Args) -> Value | None:
        """Student functions and `run`. `None` means "not mine, try built-ins".
        A failure raises `EvalError` (Rust: `Some(Err(e))`)."""

    @abc.abstractmethod
    def is_external(self, name: str) -> bool: ...


def spec(msg: str) -> NoReturn:
    raise EvalErrorSpec(msg)


def field(v: Value, name: str) -> Value:
    match v:
        case ValueMap(m):
            return next((x for k, x in m if k.text() == name), NULL)
        # JavaScript's `xs.length`.
        case ValueList() | ValueStr() if name == "length":
            return builtin("len", [v])
        case other:
            spec(f"`.{name}` on a {other.type_name()}")


def eval(e: Expr, h: Host) -> Value:  # noqa: A001
    match e:
        case ExprLit(lit):
            return lit
        case ExprName(n):
            found = h.var(n)
            if found is not None:
                return found
            parts = n.split(".")
            base = parts[0]
            v = h.var(base)
            if v is None:
                spec(f"unknown name `{n}`")
            for p in parts[1:]:
                v = field(v, p)
            return v
        case ExprList(items):
            return ValueList([eval(x, h) for x in items])
        case ExprMap(pairs):
            m: list[tuple[Value, Value]] = []
            for k, x in pairs:
                kv = eval(k, h)
                m.append((kv, eval(x, h)))
            return ValueMap(m)
        case ExprNot(x):
            return ValueBool(not eval(x, h).truthy())
        case ExprNeg(x):
            match eval(x, h):
                case ValueInt(i):
                    return ValueInt(_wrap_i128(-i))
                case ValueFloat(f):
                    return ValueFloat(-f)
                case neg:
                    spec(f"cannot negate a {neg.type_name()}")
        case ExprField(x, f):
            return field(eval(x, h), f)
        case ExprIndex(x, i):
            xv = eval(x, h)
            return index(xv, eval(i, h))
        case ExprBin(BinOp.AND, a, b):
            return ValueBool(eval(a, h).truthy() and eval(b, h).truthy())
        case ExprBin(BinOp.OR, a, b):
            return ValueBool(eval(a, h).truthy() or eval(b, h).truthy())
        case ExprBin(op, a, b):
            av, bv = eval(a, h), eval(b, h)
            return binop(op, av, bv)
        case ExprCall(name, args):
            if name == "raises":
                if len(args) != 1:
                    spec("raises(...) takes exactly one expression")
                inner = args[0][1]
                try:
                    eval(inner, h)
                except EvalErrorStudent:
                    return ValueBool(True)
                return ValueBool(False)
            # all(xs, pred) / any(xs, pred): `pred` names a one-argument built-in.
            match (name, args):
                case ("all" | "any", [(None, xs), (None, ExprName(p))]) if h.var(p) is None:
                    if p not in BUILTINS or p in ("raises", "then"):
                        spec(
                            f"{name}(xs, {p}): the second argument must name a built-in such as nonempty or is_sorted"
                        )
                    hits: list[Value] = []
                    for item in list_(eval(xs, h), name):
                        hits.append(ValueBool(builtin(p, [item]).truthy()))
                    return builtin(name, [ValueList(hits)])
            vals: Args = []
            for an, a in args:
                vals.append((an, eval(a, h)))
            r = h.call(name, list(vals))
            if r is not None:
                return r
            if any(an is not None for an, _ in vals):
                spec(f"`{name}` takes no named arguments")
            return builtin(name, [x for _, x in vals])
    raise AssertionError(e)


def index(v: Value, i: Value) -> Value:
    def pos(length: int) -> int:
        if not isinstance(i, ValueInt):
            spec("index must be an int")
        idx = length + i.i if i.i < 0 else i.i
        if idx < 0 or idx >= length:
            spec(f"index {i.i} out of range (len {length})")
        return idx

    match v:
        case ValueList(items):
            return items[pos(len(items))]
        case ValueStr(s):
            return ValueChar(s[pos(len(s))])
        case ValueMap():
            t = i.text()
            key = t if t is not None else str(i)
            return field(v, key)
        case other:
            spec(f"cannot index a {other.type_name()}")


def _repeat(s: str, n: int) -> str:
    """Rust `str::repeat`, which also takes counts too large for a Python index
    when the text is empty."""
    return "" if s == "" else s * n


def binop(op: BinOp, a: Value, b: Value) -> Value:
    match op:
        case BinOp.EQ:
            return ValueBool(a == b)
        case BinOp.NE:
            return ValueBool(a != b)
        case BinOp.LT:
            return ValueBool(a.cmp_values(b) < 0)
        case BinOp.LE:
            return ValueBool(a.cmp_values(b) <= 0)
        case BinOp.GT:
            return ValueBool(a.cmp_values(b) > 0)
        case BinOp.GE:
            return ValueBool(a.cmp_values(b) >= 0)
        case BinOp.ADD:
            match (a, b):
                case (ValueInt(x), ValueInt(y)):
                    r = _checked_i128(x + y)
                    if r is None:
                        raise EvalErrorSpec("overflow")
                    return ValueInt(r)
                case (ValueList(x), ValueList(y)):
                    return ValueList(x + y)
            ta, tb = a.text(), b.text()
            if ta is not None and tb is not None:
                return ValueStr(ta + tb)
            return num(op, a, b)
        case BinOp.MUL:
            match (a, b):
                case (ValueStr(s), ValueInt(n)) | (ValueInt(n), ValueStr(s)):
                    return ValueStr(_repeat(s, max(n, 0) & U64_MASK))
            return num(op, a, b)
        case BinOp.AND | BinOp.OR:
            raise AssertionError("short-circuited in eval")
    return num(op, a, b)


def _fdiv(x: float, y: float) -> float:
    """IEEE division, as Rust's `/` on f64 (Python raises on a zero divisor)."""
    if y == 0.0:
        if x == 0.0 or math.isnan(x):
            return math.nan
        return math.copysign(math.inf, x) * math.copysign(1.0, y)
    return x / y


def _frem(x: float, y: float) -> float:
    """Rust's `%` on f64: the C `fmod` (Python's `%` follows the divisor's sign)."""
    if math.isnan(x) or math.isnan(y) or math.isinf(x) or y == 0.0:
        return math.nan
    return math.fmod(x, y)


def num(op: BinOp, a: Value, b: Value) -> Value:
    if isinstance(a, ValueInt) and isinstance(b, ValueInt):
        x, y = a.i, b.i
        r: int | None
        match op:
            case BinOp.ADD:
                r = _checked_i128(x + y)
            case BinOp.SUB:
                r = _checked_i128(x - y)
            case BinOp.MUL:
                r = _checked_i128(x * y)
            case BinOp.DIV:
                r = None if y == 0 else _checked_i128(compat.div_trunc(x, y))
            case BinOp.REM:
                r = None if y == 0 or (x == I128_MIN and y == -1) else compat.rem_trunc(x, y)
            case _:
                r = None
        if r is None:
            raise EvalErrorSpec("integer overflow or division by zero")
        return ValueInt(r)
    fx, fy = a.as_f64(), b.as_f64()
    if fx is None or fy is None:
        spec(f"arithmetic on {a.type_name()} and {b.type_name()}")
    match op:
        case BinOp.ADD:
            return ValueFloat(fx + fy)
        case BinOp.SUB:
            return ValueFloat(fx - fy)
        case BinOp.MUL:
            return ValueFloat(fx * fy)
        case BinOp.DIV:
            return ValueFloat(_fdiv(fx, fy))
        case BinOp.REM:
            return ValueFloat(_frem(fx, fy))
    raise AssertionError(op)


BUILTINS: list[str] = [
    "len",
    "bytes",
    "sorted",
    "reversed",
    "sum",
    "min",
    "max",
    "abs",
    "count",
    "contains",
    "join",
    "split",
    "words",
    "lines",
    "lower",
    "upper",
    "trim",
    "chars",
    "unique",
    "keys",
    "values",
    "get",
    "all",
    "any",
    "int",
    "float",
    "str",
    "is_sorted",
    "permutation",
    "repeat",
    "range",
    "slice",
    "starts_with",
    "ends_with",
    "matches",
    "replace",
    "nonempty",
    "concat",
    "flatten",
    "approx",
    "raises",
    "then",
    "Ok",
    "Some",
]


def list_(v: Value, f: str) -> list[Value]:
    """Rust `list` (renamed: it would hide Python's `list`)."""
    match v:
        case ValueList(items):
            return list(items)
        case ValueStr(s):
            return [ValueChar(c) for c in s]
        case ValueNull():
            return []
        case other:
            spec(f"{f}: expected a list, got {other.type_name()}")


def text(v: Value, f: str) -> str:
    t = v.text()
    if t is None:
        raise EvalErrorSpec(f"{f}: expected a string, got {v.type_name()}")
    return t


def int_(v: Value, f: str) -> int:
    """Rust `int` (renamed: it would hide Python's `int`)."""
    match v:
        case ValueInt(i):
            return i
        case other:
            spec(f"{f}: expected an int, got {other.type_name()}")


def _f64_to_i128(f: float) -> int:
    """Rust `f as i128`: truncates, saturates, NaN is 0."""
    if math.isnan(f):
        return 0
    if f >= 2.0**127:
        return I128_MAX
    if f <= -(2.0**127):
        return I128_MIN
    return int(f)


def _fmax(a: float, b: float) -> float:
    """Rust `f64::max`: a NaN loses to the other number."""
    if math.isnan(a):
        return b
    if math.isnan(b):
        return a
    return a if a > b else b


def _cmp_values(x: Value, y: Value) -> int:
    return x.cmp_values(y)


# Sort key of `sort_by(|x, y| x.cmp_values(y))` (both sorts are stable).
_order = functools.cmp_to_key(_cmp_values)


def _texts(items: list[Value]) -> str:
    """`items.iter().filter_map(Value::text).collect::<String>()`."""
    return "".join(t for x in items if (t := x.text()) is not None)


def builtin(name: str, a: list[Value]) -> Value:
    n = len(a)

    def arity(k: int) -> None:
        if n != k:
            spec(f"{name} takes {k} argument(s), got {n}")

    def strs(lst: list[str]) -> Value:
        return ValueList([ValueStr(s) for s in lst])

    match name:
        case "len":
            arity(1)
            match a[0]:
                case ValueStr(s) | ValueOpaque(s):
                    size = len(s)
                case ValueList(items):
                    size = len(items)
                case ValueMap(pairs):
                    size = len(pairs)
                case ValueChar():
                    size = 1
                case ValueNull():
                    size = 0
                case v:
                    spec(f"len of a {v.type_name()}")
            return ValueInt(size)
        case "bytes":
            arity(1)
            return ValueInt(byte_len(text(a[0], name)))
        case "sorted" | "reversed" | "unique" | "nonempty" | "flatten":
            arity(1)
            lst = list_(a[0], name)
            match name:
                case "sorted":
                    lst.sort(key=_order)
                case "reversed":
                    lst.reverse()
                case "unique":
                    seen: list[Value] = []
                    kept: list[Value] = []
                    for x in lst:
                        if any(s == x for s in seen):
                            continue
                        seen.append(x)
                        kept.append(x)
                    lst = kept
                case "nonempty":
                    lst = [x for x in lst if x.truthy()]
                case _:
                    flat: list[Value] = []
                    for x in lst:
                        match x:
                            case ValueList(inner):
                                flat.extend(inner)
                            case other:
                                flat.append(other)
                    lst = flat
            if isinstance(a[0], ValueStr) and name != "flatten":
                return ValueStr(_texts(lst))
            return ValueList(lst)
        case "sum":
            arity(1)
            acc: Value = ValueInt(0)
            for x in list_(a[0], name):
                acc = num(BinOp.ADD, acc, x)
            return acc
        case "min" | "max":
            items = list_(a[0], name) if n == 1 else a
            if not items:
                return NULL
            pick = items[0]
            for y in items[1:]:
                less = pick.cmp_values(y) > 0
                pick = y if (name == "min") == less else pick
            return pick
        case "abs":
            arity(1)
            match a[0]:
                case ValueInt(i):
                    return ValueInt(_wrap_i128(abs(i)))
                case ValueFloat(f):
                    return ValueFloat(abs(f))
                case v:
                    spec(f"abs of a {v.type_name()}")
        case "count":
            arity(2)
            match a[0]:
                case ValueStr(s):
                    pat = text(a[1], name)
                    if pat == "":
                        spec("count: empty pattern")
                    return ValueInt(s.count(pat))
                case v:
                    return ValueInt(sum(1 for x in list_(v, name) if x == a[1]))
        case "contains":
            arity(2)
            match a[0]:
                case ValueStr(s):
                    return ValueBool(text(a[1], name) in s)
                case ValueMap(pairs):
                    return ValueBool(any(k == a[1] for k, _ in pairs))
                case v:
                    return ValueBool(any(x == a[1] for x in list_(v, name)))
        case "join":
            arity(2)
            # Python's `sep.join(parts)` arrives as join(sep, parts).
            parts: Value
            sep: Value
            match (a[0], a[1]):
                case (ValueStr(), ValueList()):
                    parts, sep = a[1], a[0]
                case _:
                    parts, sep = a[0], a[1]
            sep_text = text(sep, name)
            part_texts = [text(x, name) for x in list_(parts, name)]
            return ValueStr(sep_text.join(part_texts))
        case "split":
            arity(2)
            s = text(a[0], name)
            sep_text = text(a[1], name)
            if sep_text == "":
                spec("split: empty separator")
            return strs(s.split(sep_text))
        case "words":
            arity(1)
            return strs(compat.split_whitespace(text(a[0], name)))
        case "lines":
            arity(1)
            return strs(compat.lines(text(a[0], name)))
        case "lower":
            arity(1)
            return ValueStr(text(a[0], name).lower())
        case "upper":
            arity(1)
            return ValueStr(text(a[0], name).upper())
        case "trim":
            arity(1)
            return ValueStr(compat.trim(text(a[0], name)))
        case "chars":
            arity(1)
            return ValueList([ValueChar(c) for c in text(a[0], name)])
        case "keys" | "values":
            arity(1)
            match a[0]:
                case ValueMap(pairs):
                    return ValueList([k if name == "keys" else x for k, x in pairs])
                case v:
                    spec(f"{name} of a {v.type_name()}")
        case "get":
            if n not in (2, 3):
                spec("get(map, key[, default])")
            match a[0]:
                case ValueMap(pairs):
                    hit = next((x for k, x in pairs if k == a[1]), None)
                    if hit is not None:
                        return hit
                    return a[2] if len(a) > 2 else NULL
                case v:
                    return index(v, a[1])
        case "all":
            arity(1)
            return ValueBool(all(x.truthy() for x in list_(a[0], name)))
        case "any":
            arity(1)
            return ValueBool(any(x.truthy() for x in list_(a[0], name)))
        case "int":
            arity(1)
            match a[0]:
                case ValueInt(i):
                    return ValueInt(i)
                case ValueFloat(f):
                    return ValueInt(_f64_to_i128(f))
                case ValueBool(b):
                    return ValueInt(int(b))
                case ValueChar(c):
                    return ValueInt(ord(c))
                case v:
                    parsed = compat.parse_int(compat.trim(text(v, name)))
                    if parsed is None:
                        raise EvalErrorSpec(f"int: cannot parse {v}")
                    return ValueInt(parsed)
        case "float":
            arity(1)
            fv = a[0].as_f64()
            if fv is not None:
                return ValueFloat(fv)
            v = a[0]
            pf = compat.parse_float(compat.trim(text(v, name)))
            if pf is None:
                raise EvalErrorSpec(f"float: cannot parse {v}")
            return ValueFloat(pf)
        case "str":
            arity(1)
            t = a[0].text()
            return ValueStr(t if t is not None else str(a[0]))
        case "is_sorted":
            arity(1)
            lst = list_(a[0], name)
            return ValueBool(all(lst[k].cmp_values(lst[k + 1]) <= 0 for k in range(len(lst) - 1)))
        case "permutation":
            arity(2)
            xs = list_(a[0], name)
            ys = list_(a[1], name)
            xs.sort(key=_order)
            ys.sort(key=_order)
            return ValueBool(xs == ys)
        case "repeat":
            arity(2)
            k = min(max(int_(a[1], name), 0), 1_000_000)
            match a[0]:
                case ValueList(items):
                    return ValueList(list(items) * k)
                case v:
                    return ValueStr(text(v, name) * k)
        case "range":
            match n:
                case 1:
                    lo, hi = 0, int_(a[0], name)
                case 2:
                    lo, hi = int_(a[0], name), int_(a[1], name)
                case _:
                    spec("range(n) or range(lo, hi)")
            if _wrap_i128(hi - lo) > 1_000_000:
                spec("range too large")
            return ValueList([ValueInt(x) for x in range(lo, hi)])
        case "slice":
            if n not in (2, 3):
                spec("slice(x, start[, end])")
            is_str = isinstance(a[0], ValueStr)
            lst = list_(a[0], name)
            length = len(lst)

            def norm(i: int) -> int:
                return min(max(length + i if i < 0 else i, 0), length)

            s_idx = norm(int_(a[1], name))
            e_idx = norm(int_(a[2], name)) if n == 3 else len(lst)
            part = lst[s_idx:e_idx] if s_idx < e_idx else []
            if is_str:
                return ValueStr(_texts(part))
            return ValueList(part)
        case "starts_with" | "ends_with":
            arity(2)
            s = text(a[0], name)
            p = text(a[1], name)
            return ValueBool(s.startswith(p) if name == "starts_with" else s.endswith(p))
        case "matches":
            arity(2)
            source = text(a[1], name)
            try:
                pattern = compat.regex(source)
            except re.error as e:
                raise EvalErrorSpec(f"matches: bad regex: {e}") from None
            return ValueBool(pattern.search(text(a[0], name)) is not None)
        case "replace":
            arity(3)
            return ValueStr(text(a[0], name).replace(text(a[1], name), text(a[2], name)))
        case "concat":
            out: list[Value] = []
            for x in a:
                out.extend(list_(x, name))
            return ValueList(out)
        # Sequencing: evaluates every argument in order, returns the last.
        case "then":
            return a[-1] if a else NULL
        # Harnesses unwrap Ok(v) / Some(v) to v (None is null, Err raises), so
        # Rust-style expectations read as the plain value.
        case "Ok" | "Some":
            arity(1)
            return a[0]
        case "approx":
            if n not in (2, 3):
                spec("approx(a, b[, eps])")
            x0, y0 = a[0].as_f64(), a[1].as_f64()
            if x0 is None or y0 is None:
                spec("approx: expected numbers")
            eps = a[2].as_f64() if len(a) > 2 else None
            if eps is None:
                eps = 1e-6
            return ValueBool(abs(x0 - y0) <= eps * _fmax(_fmax(abs(x0), abs(y0)), 1.0))
        case _:
            spec(f"unknown function `{name}`")


def constant(src: str) -> Value:
    """Evaluates a constant expression (no variables, no external calls)."""

    class NoHost(Host):
        def var(self, name: str) -> Value | None:
            return None

        def call(self, name: str, args: Args) -> Value | None:
            return None

        def is_external(self, name: str) -> bool:
            return False

    e = parse(src)
    try:
        v = eval(e, NoHost())
    except EvalError as err:
        raise Error(err.msg) from None
    return v


def calls(e: Expr, out: list[str]) -> None:
    """Names of every call in `e` (to validate specs and to find external calls)."""
    match e:
        case ExprCall(n, args):
            out.append(n)
            for _, a in args:
                calls(a, out)
        case ExprList(items):
            for x in items:
                calls(x, out)
        case ExprMap(pairs):
            for k, v in pairs:
                calls(k, out)
                calls(v, out)
        case ExprNot(x) | ExprNeg(x) | ExprField(x, _):
            calls(x, out)
        case ExprBin(_, a, b) | ExprIndex(a, b):
            calls(a, out)
            calls(b, out)
        case ExprLit() | ExprName():
            pass


def distance(a: str, b: str) -> int:
    row = list(range(len(b) + 1))
    for i, ca in enumerate(a):
        prev = row[0]
        row[0] = i + 1
        for j, cb in enumerate(b):
            cur = row[j + 1]
            row[j + 1] = min(prev + (1 if ca != cb else 0), row[j] + 1, cur + 1)
            prev = cur
    return row[len(b)]


# An expected function: name, fewest and most arguments.
Arity = tuple[str, int, int]


def resolve_methods(e: Expr, functions: list[Arity]) -> Expr:
    """`s.len()` lexes as a call named `s.len`. Unless that is an expected
    function (Python's `Stack.push`), it is the method `len` on `s`."""

    def r(x: Expr) -> Expr:
        return resolve_methods(x, functions)

    match e:
        case ExprCall(name, args):
            resolved = [(n, r(a)) for n, a in args]
            recv, dot, m = name.rpartition(".")
            if dot and not any(f == name for f, _, _ in functions) and name not in BUILTINS:
                return method(ExprName(recv), m, [a for _, a in resolved])
            return ExprCall(name, resolved)
        case ExprList(items):
            return ExprList([r(x) for x in items])
        case ExprMap(pairs):
            out: list[tuple[Expr, Expr]] = []
            for k, v in pairs:
                rk = r(k)
                out.append((rk, r(v)))
            return ExprMap(out)
        case ExprNot(x):
            return ExprNot(r(x))
        case ExprNeg(x):
            return ExprNeg(r(x))
        case ExprField(x, f):
            return ExprField(r(x), f)
        case ExprBin(op, a, b):
            ra = r(a)
            return ExprBin(op, ra, r(b))
        case ExprIndex(a, b):
            ra = r(a)
            return ExprIndex(ra, r(b))
        case ExprLit() | ExprName():
            return e
    raise AssertionError(e)


def closest(name: str, known: Iterable[str]) -> str:
    """` (closest: `x`)` when a known name is a likely typo of `name`."""
    candidates: list[tuple[int, str]] = []
    for k in known:
        prefix = min(byte_len(k), byte_len(name)) >= 3 and (
            name.startswith(k) or k.startswith(name)
        )
        d = 1 if prefix else distance(name, k)
        if d <= max(2, byte_len(k) // 3):
            candidates.append((d, k))
    if not candidates:
        return ""
    _, best = min(candidates)
    return f" (closest: `{best}`)"


def static_check(e: Expr, functions: list[Arity], vars: list[str]) -> None:
    """Checks names before anything runs: every call is one of `functions`
    (name, parameter count), a built-in or `run`, and every bare name is one
    of `vars`. The message says what to write instead."""
    err: str | None = None
    predicates: list[Expr] = []

    def visit(x: Expr) -> None:
        nonlocal err
        if err is not None:
            return
        match x:
            case ExprCall(name, args):
                match (name, args):
                    case ("all" | "any", [_, (None, ExprName() as p)]):
                        predicates.append(p)
                found = next(((lo, hi) for f, lo, hi in functions if f == name), None)
                if found is not None:
                    lo, hi = found
                    if not lo <= len(args) <= hi:
                        want = str(lo) if lo == hi else f"{lo} to {hi}"
                        err = f"`{name}` takes {want} argument(s), this call passes {len(args)}"
                elif name not in BUILTINS and name != "run":
                    known = [f for f, _, _ in functions] + BUILTINS
                    err = f"unknown function `{name}`{closest(name, known)}: call an expected function or a built-in"
            case ExprName(n) if not any(p is x for p in predicates):
                base = n.split(".")[0]
                if base not in vars:
                    if not vars:
                        err = f"unknown name `{n}`: a case has no variables, write the value itself"
                    else:
                        err = f"unknown name `{n}`{closest(base, vars)}: the variables here are {', '.join(vars)}"
            case ExprName(n) if n not in BUILTINS:
                err = f"`{n}` is not a built-in{closest(n, BUILTINS)}: all(xs, p) / any(xs, p) need a built-in such as nonempty"

    walk(e, visit)
    if err is not None:
        raise Error(err)


class Outcome:
    """Base of `OutcomePass`, `OutcomeFail` and `OutcomeSpecError`."""

    __slots__ = ()


@dataclass(frozen=True)
class OutcomePass(Outcome):
    pass


@dataclass(frozen=True)
class OutcomeFail(Outcome):
    msg: str


@dataclass(frozen=True)
class OutcomeSpecError(Outcome):
    msg: str


def check(e: Expr, h: Host) -> Outcome:
    """Runs one boolean check. For a top-level `==`, the side that calls the
    student's code is reported as "got"."""

    def res() -> Outcome:
        if isinstance(e, ExprBin) and e.op in (BinOp.EQ, BinOp.NE):
            op, left, right = e.op, e.left, e.right
            lv = eval(left, h)
            rv = eval(right, h)
            same = lv == rv
            if same == (op == BinOp.EQ):
                return OutcomePass()
            names: list[str] = []
            calls(right, names)
            right_is_student = any(h.is_external(n) for n in names)
            lnames: list[str] = []
            calls(left, lnames)
            left_is_student = any(h.is_external(n) for n in lnames)
            if right_is_student and not left_is_student:
                got, want = rv, lv
            else:
                got, want = lv, rv
            if op == BinOp.EQ:
                return OutcomeFail(f"expected {want.short(120)}, got {got.short(120)}")
            return OutcomeFail(f"got {got.short(120)}, which it must not be")
        v = eval(e, h)
        return OutcomePass() if v.truthy() else OutcomeFail("check is false")

    try:
        o = res()
    except EvalErrorStudent as err:
        return OutcomeFail(err.msg)
    except EvalErrorSpec as err:
        return OutcomeSpecError(err.msg)
    return o
