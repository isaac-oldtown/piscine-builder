"""Seeded random input generators and shrinking.

A generator is written as a short string (`string "ab," 0..30`,
`list(int 0..9) 0..10`, `lines(log_line) 0..50`), or as a template map
`{ fmt: "{h}:{m}", vars: { h: "int 0..23 w2", m: "int 0..59 w2" } }`.
Names not built in are looked up in the course's `lib.yaml`."""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

from proctor import expr
from proctor.compat import (
    I128_MAX,
    I128_MIN,
    U64_MASK,
    debug_str,
    div_trunc,
    float_display,
    is_whitespace,
    lines,
    parse_float,
    parse_uint,
    trim,
)
from proctor.errors import Error
from proctor.value import (
    NULL,
    Value,
    ValueBool,
    ValueChar,
    ValueFloat,
    ValueInt,
    ValueList,
    ValueNull,
    ValueStr,
)

_U128_MASK = (1 << 128) - 1
_I64_MIN = -(1 << 63)
_I64_MAX = (1 << 63) - 1


def _wrap_i128(x: int) -> int:
    """Rust's wrapping i128 arithmetic (release builds do not check overflow)."""
    x &= _U128_MASK
    return x - (1 << 128) if x > I128_MAX else x


@dataclass
class Rng:
    f0: int

    @staticmethod
    def new(seed: int) -> Rng:
        return Rng(seed & U64_MASK)

    def next_u64(self) -> int:
        self.f0 = (self.f0 + 0x9E37_79B9_7F4A_7C15) & U64_MASK
        z = self.f0
        z = ((z ^ (z >> 30)) * 0xBF58_476D_1CE4_E5B9) & U64_MASK
        z = ((z ^ (z >> 27)) * 0x94D0_49BB_1331_11EB) & U64_MASK
        return z ^ (z >> 31)

    def below(self, n: int) -> int:
        if n == 0:
            return 0
        return self.next_u64() % n

    def range(self, lo: int, hi: int) -> int:
        if hi <= lo:
            return lo
        span = (((hi - lo) & _U128_MASK) + 1) & _U128_MASK
        high = self.next_u64()
        low = self.next_u64()
        return _wrap_i128(lo + ((high << 64) | low) % span)

    def chance(self, p: float) -> bool:
        return self.unit() < p

    def unit(self) -> float:
        return float(self.next_u64() >> 11) / float(1 << 53)


def sub_seed(seed: int, name: str) -> int:
    """Mixes a run seed with a test name so each test has its own stream."""
    h = 0xCBF2_9CE4_8422_2325
    for b in name.encode("utf-8"):
        h ^= b
        h = (h * 0x0100_0000_01B3) & U64_MASK
    return Rng.new(seed ^ h).next_u64()


class Gen:
    """Base of the generator variants (`GenInt`, `GenStr`, ...)."""

    def generate(self, rng: Rng) -> Value:
        match self:
            case GenInt(lo=lo, hi=hi):
                if rng.chance(0.25):
                    edges = [lo, hi, min(_wrap_i128(lo + 1), hi), max(_wrap_i128(hi - 1), lo)]
                    if lo <= 0 <= hi:
                        edges.append(0)
                    return ValueInt(edges[rng.below(len(edges))])
                return ValueInt(rng.range(lo, hi))
            case GenFloat(lo=flo, hi=fhi):
                if rng.chance(0.15):
                    fedges = [flo, fhi]
                    if flo <= 0.0 <= fhi:
                        fedges.append(0.0)
                    return ValueFloat(fedges[rng.below(len(fedges))])
                return ValueFloat(flo + (fhi - flo) * rng.unit())
            case GenBool():
                return ValueBool(rng.chance(0.5))
            case GenChar(alphabet=alphabet):
                return ValueChar(alphabet[rng.below(len(alphabet))])
            case GenStr(alphabet=alphabet, lo=lo, hi=hi):
                n = sized(rng, lo, hi)
                # Sometimes draw from two letters only, so separators and
                # repeats (",," / "aa") show up often.
                if len(alphabet) > 2 and rng.chance(0.2):
                    pool = [alphabet[rng.below(len(alphabet))] for _ in range(2)]
                else:
                    pool = alphabet
                return ValueStr("".join(pool[rng.below(len(pool))] for _ in range(n)))
            case GenList(inner=inner, lo=lo, hi=hi):
                n = sized(rng, lo, hi)
                return ValueList([inner.generate(rng) for _ in range(n)])
            case GenLines(inner=inner, lo=lo, hi=hi):
                n = sized(rng, lo, hi)
                s: list[str] = []
                for _ in range(n):
                    s.append(render_text(inner.generate(rng), inner))
                    s.append("\n")
                return ValueStr("".join(s))
            case GenOpt(inner=inner):
                if rng.chance(0.15):
                    return NULL
                return inner.generate(rng)
            case GenChoice(values=items):
                return items[rng.below(len(items))]
            case GenConst(value=v):
                return v
            case GenFmt(parts=parts):
                out: list[str] = []
                for p in parts:
                    match p:
                        case FmtPartLit(text=lit):
                            out.append(lit)
                        case FmtPartVar(gen=g):
                            out.append(render_text(g.generate(rng), g))
                return ValueStr("".join(out))
        raise AssertionError(type(self))

    def admits(self, v: Value) -> bool | None:
        """Whether this generator could produce `v`; `None` when it cannot tell."""
        match (self, v):
            case (GenInt(lo=lo, hi=hi), ValueInt(i=i)):
                return lo <= i <= hi
            case (GenFloat(lo=flo, hi=fhi), ValueFloat(f=f)):
                return flo <= f <= fhi
            case (GenBool(), ValueBool()):
                return True
            case (GenChar(alphabet=alphabet), ValueChar(c=c)):
                return c in alphabet
            case (GenStr(alphabet=alphabet, lo=lo, hi=hi), ValueStr(s=s)):
                return lo <= len(s) <= hi and all(ch in alphabet for ch in s)
            case (GenList(inner=inner, lo=lo, hi=hi), ValueList(items=items)):
                if not lo <= len(items) <= hi:
                    return False
                each = [inner.admits(x) for x in items]
                return False if False in each else None if None in each else True
            case (GenLines(inner=inner, lo=lo, hi=hi), ValueStr(s=s)):
                if s and not s.endswith("\n"):
                    return False
                ls = lines(s)
                if not lo <= len(ls) <= hi:
                    return False
                if not isinstance(inner, GenStr | GenChoice | GenConst):
                    return None
                each = [inner.admits(ValueStr(x)) for x in ls]
                return False if False in each else None if None in each else True
            case (GenOpt(), ValueNull()):
                return True
            case (GenOpt(inner=inner), _):
                return inner.admits(v)
            case (GenChoice(values=items), _):
                return v in items
            case (GenConst(value=c), _):
                return v == c
            case (GenFmt(), _):
                return None
        return False

    def shrink(self, v: Value) -> list[Value]:
        """Smaller variants of `v` that this generator could also have produced."""
        out: list[Value] = []
        match (self, v):
            case (GenInt(lo=lo, hi=hi), ValueInt(i=i)):
                target = 0 if lo <= 0 <= hi else lo
                d = i - target
                signum = (d > 0) - (d < 0)
                for c in [target, target + div_trunc(d, 2), i - signum]:
                    if c != i and lo <= c <= hi and ValueInt(c) not in out:
                        out.append(ValueInt(c))
            case (GenStr(alphabet=alphabet, lo=lo), ValueStr(s=s)):
                cs = list(s)
                for part in shrink_seq(cs, lo):
                    out.append(ValueStr("".join(part)))
                if alphabet:
                    first = alphabet[0]
                    for i in range(len(cs))[:12]:
                        if cs[i] != first:
                            t = list(cs)
                            t[i] = first
                            out.append(ValueStr("".join(t)))
            case (GenList(inner=inner, lo=lo), ValueList(items=items)):
                for vpart in shrink_seq(items, lo):
                    out.append(ValueList(vpart))
                for i in range(len(items))[:8]:
                    for smaller in inner.shrink(items[i])[:3]:
                        vt = list(items)
                        vt[i] = smaller
                        out.append(ValueList(vt))
            case (GenLines(lo=lo), ValueStr(s=s)):
                ls = lines(s)
                for lpart in shrink_seq(ls, lo):
                    text = "".join(f"{line}\n" for line in lpart)
                    if not lpart:
                        text = ""
                    out.append(ValueStr(text))
            case (GenOpt(inner=inner), _) if not isinstance(v, ValueNull):
                out.append(NULL)
                out.extend(inner.shrink(v))
        return out


@dataclass(frozen=True)
class GenInt(Gen):
    lo: int
    hi: int
    width: int


@dataclass(frozen=True)
class GenFloat(Gen):
    lo: float
    hi: float


@dataclass(frozen=True)
class GenBool(Gen):
    pass


@dataclass(frozen=True)
class GenChar(Gen):
    alphabet: list[str]


@dataclass(frozen=True)
class GenStr(Gen):
    alphabet: list[str]
    lo: int
    hi: int


@dataclass(frozen=True)
class GenList(Gen):
    inner: Gen
    lo: int
    hi: int


@dataclass(frozen=True)
class GenLines(Gen):
    inner: Gen
    lo: int
    hi: int


@dataclass(frozen=True)
class GenOpt(Gen):
    inner: Gen


@dataclass(frozen=True)
class GenChoice(Gen):
    values: list[Value]


@dataclass(frozen=True)
class GenConst(Gen):
    value: Value


@dataclass(frozen=True)
class GenFmt(Gen):
    parts: list[FmtPart]


class FmtPart:
    """Base of `FmtPartLit` and `FmtPartVar`."""


@dataclass(frozen=True)
class FmtPartLit(FmtPart):
    text: str


@dataclass(frozen=True)
class FmtPartVar(FmtPart):
    gen: Gen


DEFAULT_ALPHABET = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789 ,.;:!?-_"


def printable() -> list[str]:
    return [chr(b) for b in range(0x20, 0x7F)]


Lib = dict[str, Any]


def parse(v: Any, lib: Lib) -> Gen:
    return parse_depth(v, lib, 0)


def _number_value(n: int | float) -> Value:
    """`Value::Int` when the YAML number is an i64 (`as_i64`), else `Value::Float`."""
    if isinstance(n, int) and _I64_MIN <= n <= _I64_MAX:
        return ValueInt(n)
    try:
        return ValueFloat(float(n))
    except OverflowError:
        return ValueFloat(math.inf if n > 0 else -math.inf)


def parse_depth(v: Any, lib: Lib, depth: int) -> Gen:
    if depth > 16:
        raise Error("generator definitions are nested too deeply (cycle in lib.yaml?)")
    if isinstance(v, str):
        return parse_dsl(v, lib, depth)
    if isinstance(v, bool):
        return GenConst(ValueBool(v))
    if isinstance(v, (int, float)):
        return GenConst(_number_value(v))
    if isinstance(v, list):
        return GenChoice([yaml_value(x) for x in v])
    if isinstance(v, dict):
        fmt = v.get("fmt")
        if not isinstance(fmt, str):
            raise Error('a generator map needs `fmt: "...{var}..."` and `vars:`')
        vars: dict[str, Gen] = {}
        vm = v.get("vars")
        if isinstance(vm, dict):
            for k, g in vm.items():
                if not isinstance(k, str):
                    raise Error("`vars` keys must be names")
                vars[k] = parse_depth(g, lib, depth + 1)
        return GenFmt(parse_fmt(fmt, vars))
    raise Error("unsupported generator value")


def yaml_value(v: Any) -> Value:
    if v is None:
        return NULL
    if isinstance(v, bool):
        return ValueBool(v)
    if isinstance(v, (int, float)):
        return _number_value(v)
    if isinstance(v, str):
        return ValueStr(v)
    if isinstance(v, list):
        return ValueList([yaml_value(x) for x in v])
    raise Error("unsupported literal in generator")


def parse_fmt(fmt: str, vars: dict[str, Gen]) -> list[FmtPart]:
    parts: list[FmtPart] = []
    lit = ""
    i = 0
    while i < len(fmt):
        c = fmt[i]
        i += 1
        peek = fmt[i] if i < len(fmt) else None
        if c == "{" and peek == "{":
            i += 1
            lit += "{"
        elif c == "}" and peek == "}":
            i += 1
            lit += "}"
        elif c == "{":
            # `take_while` also consumes the closing `}`.
            end = fmt.find("}", i)
            if end < 0:
                name, i = fmt[i:], len(fmt)
            else:
                name, i = fmt[i:end], end + 1
            g = vars.get(trim(name))
            if g is None:
                raise Error(f"fmt uses `{{{name}}}` but `vars` does not define it")
            if lit:
                parts.append(FmtPartLit(lit))
                lit = ""
            parts.append(FmtPartVar(g))
        else:
            lit += c
    if lit:
        parts.append(FmtPartLit(lit))
    return parts


def head(s: str) -> tuple[str, str | None, str]:
    """Splits `name(inner) rest` / `name rest`."""
    s = trim(s)
    n = 0
    while n < len(s) and (s[n].isalnum() or s[n] == "_"):
        n += 1
    name = s[:n]
    if not name:
        raise Error(f"generator `{s}` must start with a name")
    rest = s[len(name) :]
    if rest.startswith("("):
        r = rest[1:]
        depth = 1
        in_str = False
        prev = " "
        for i, c in enumerate(r):
            if c == '"' and prev != "\\":
                in_str = not in_str
            elif c == "(" and not in_str:
                depth += 1
            elif c == ")" and not in_str:
                depth -= 1
                if depth == 0:
                    return (name, r[:i], r[i + 1 :])
            prev = c
        raise Error(f"unbalanced parentheses in `{s}`")
    return (name, None, rest)


@dataclass(kw_only=True)
class Opts:
    text: str | None = None
    range: tuple[float, float, bool] | None = None
    width: int = 0
    list: Value | None = None


def _is_ascii_digit(c: str) -> bool:
    return "0" <= c <= "9"


def options(rest: str) -> Opts:
    cs = rest
    o = Opts()
    i = 0
    while i < len(cs):
        c = cs[i]
        if is_whitespace(c):
            i += 1
        elif c == '"':
            o.text, i = expr.read_string(cs, i)
        elif c == "[":
            start = i
            depth = 0
            in_str = False
            while i < len(cs):
                ch = cs[i]
                if ch == '"' and (i == 0 or cs[i - 1] != "\\"):
                    in_str = not in_str
                elif ch == "[" and not in_str:
                    depth += 1
                elif ch == "]" and not in_str:
                    depth -= 1
                    if depth == 0:
                        i += 1
                        break
                i += 1
            src = cs[start:i]
            o.list = expr.constant(src)
        elif c == "w" and i + 1 < len(cs) and _is_ascii_digit(cs[i + 1]):
            j = i + 1
            while j < len(cs) and _is_ascii_digit(cs[j]):
                j += 1
            digits = cs[i + 1 : j]
            width = parse_uint(digits)
            o.width = width if width is not None else 0
            i += 1 + len(digits)
        else:
            j = i
            while j < len(cs) and not is_whitespace(cs[j]):
                j += 1
            word = cs[i:j]
            i += len(word)
            if ".." not in word:
                raise Error(f'unexpected `{word}` (expected "alphabet", lo..hi, wN or [list])')
            a, b = word.split("..", 1)
            is_float = "." in a or "." in b or "e" in a or "e" in b
            lo = _range_num(a, word)
            o.range = (lo, _range_num(b, word), is_float)
    return o


def _range_num(t: str, word: str) -> float:
    x = parse_float(t)
    if x is None:
        raise Error(f"bad range `{word}`")
    return x


def _fmax(a: float, b: float) -> float:
    """Rust `f64::max`: a NaN operand gives the other one."""
    if math.isnan(a):
        return b
    if math.isnan(b):
        return a
    return a if a > b else b


def _as_i128(x: float) -> int:
    """Rust `x as i128`: truncates toward zero, saturates, NaN is 0."""
    if math.isnan(x):
        return 0
    if x >= 2.0**127:
        return I128_MAX
    if x <= -(2.0**127):
        return I128_MIN
    return int(x)


def _as_usize(x: float) -> int:
    """Rust `x as usize`: truncates toward zero, saturates, NaN is 0."""
    if math.isnan(x) or x <= 0:
        return 0
    if x >= 2.0**64:
        return U64_MASK
    return int(x)


def parse_dsl(s: str, lib: Lib, depth: int) -> Gen:
    name, inner, rest = head(s)

    def inner_gen(what: str) -> Gen:
        if inner is None:
            raise Error(f"`{what}` needs an inner generator: {what}(...)")
        return parse_depth(inner, lib, depth + 1)

    if name == "lit":
        return GenConst(expr.constant(trim(rest)))
    o = options(rest)

    def urange(dlo: int, dhi: int) -> tuple[int, int]:
        if o.range is not None:
            a, b, _ = o.range
            return (_as_usize(_fmax(a, 0.0)), _as_usize(_fmax(_fmax(b, a), 0.0)))
        return (dlo, dhi)

    def alphabet(default: list[str]) -> list[str]:
        if o.text is not None and o.text != "":
            return list(o.text)
        return default

    match name:
        case "int" | "uint":
            if o.range is not None:
                a, b, _ = o.range
                lo, hi = _as_i128(a), _as_i128(b)
            elif name == "uint":
                lo, hi = 0, 1000
            else:
                lo, hi = -1000, 1000
            return GenInt(lo=lo, hi=max(hi, lo), width=o.width)
        case "float":
            if o.range is not None:
                flo, fhi, _ = o.range
            else:
                flo, fhi = -1000.0, 1000.0
            return GenFloat(lo=flo, hi=_fmax(fhi, flo))
        case "bool":
            return GenBool()
        case "char":
            return GenChar(alphabet=alphabet(printable()))
        case "string" | "str":
            lo, hi = urange(0, 20)
            return GenStr(alphabet=alphabet(list(DEFAULT_ALPHABET)), lo=lo, hi=hi)
        case "word":
            lo, hi = urange(1, 8)
            return GenStr(
                alphabet=alphabet(list("abcdefghijklmnopqrstuvwxyz")),
                lo=max(lo, 1),
                hi=max(hi, 1),
            )
        case "ascii":
            lo, hi = urange(0, 20)
            return GenStr(alphabet=printable(), lo=lo, hi=hi)
        case "list":
            lo, hi = urange(0, 10)
            return GenList(inner=inner_gen("list"), lo=lo, hi=hi)
        case "lines":
            lo, hi = urange(0, 20)
            return GenLines(inner=inner_gen("lines"), lo=lo, hi=hi)
        case "opt":
            return GenOpt(inner_gen("opt"))
        case "choice":
            if isinstance(o.list, ValueList) and o.list.items:
                return GenChoice(o.list.items)
            raise Error('choice needs a non-empty list: choice ["a", "b"]')
        case other:
            if other in lib:
                return parse_depth(lib[other], lib, depth + 1)
            raise Error(f"unknown generator `{other}`")


def sized(rng: Rng, lo: int, hi: int) -> int:
    if hi <= lo:
        return lo
    r = rng.below(10)
    if r == 0:
        return lo
    if r == 1:
        return hi
    if 2 <= r <= 6:
        # Bias towards short inputs: easier to read when they fail.
        return rng.range(lo, max(lo + (hi - lo) // 3, lo))
    return rng.range(lo, hi)


def shrink_seq[T](items: list[T], min: int) -> list[list[T]]:
    n = len(items)
    out: list[list[T]] = []
    if n <= min:
        return out
    if min == 0:
        out.append([])
    half = n // 2
    if half >= min and half > 0:
        out.append(items[:half])
        out.append(items[n - half :])
    for i in range(n)[:16]:
        t = list(items)
        del t[i]
        out.append(t)
    return out


def render_text(v: Value, g: Gen) -> str:
    if isinstance(v, ValueInt) and isinstance(g, GenInt) and g.width > 0:
        neg = v.i < 0
        digits = f"{abs(v.i):0{g.width}}"
        return f"-{digits}" if neg else digits
    t = v.text()
    return t if t is not None else str(v)


def describe(g: Gen) -> str:
    """Human description used by `proctor explain`."""
    match g:
        case GenInt(lo=lo, hi=hi, width=width):
            if width > 0:
                return f"int {lo}..{hi} (width {width})"
            return f"int {lo}..{hi}"
        case GenFloat(lo=flo, hi=fhi):
            return f"float {float_display(flo)}..{float_display(fhi)}"
        case GenBool():
            return "bool"
        case GenChar(alphabet=alphabet):
            return f"char from {debug_str(''.join(alphabet))}"
        case GenStr(alphabet=alphabet, lo=lo, hi=hi):
            a = "".join(alphabet)
            if len(a) > 40:
                a = a[:40] + "..."
            return f"string {lo}..{hi} over {debug_str(a)}"
        case GenList(inner=inner, lo=lo, hi=hi):
            return f"list {lo}..{hi} of ({describe(inner)})"
        case GenLines(inner=inner, lo=lo, hi=hi):
            return f"{lo}..{hi} lines of ({describe(inner)})"
        case GenOpt(inner=inner):
            return f"({describe(inner)}) or null"
        case GenChoice(values=items):
            return f"one of {ValueList(list(items)).short(60)}"
        case GenConst(value=v):
            return f"always {v.short(40)}"
        case GenFmt(parts=parts):
            s = 'text "'
            for p in parts:
                match p:
                    case FmtPartLit(text=lit):
                        s += lit
                    case FmtPartVar(gen=pg):
                        s += f"{{{describe(pg)}}}"
            s += '"'
            return s
    raise AssertionError(type(g))
