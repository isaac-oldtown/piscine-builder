"""Function signatures, per language: the abstract types used to pick
generators and harness conversions, and a normalized form used by the
prototype check (parameter names never matter)."""

from __future__ import annotations

import dataclasses
import re
from dataclasses import dataclass

from proctor import gens
from proctor.compat import is_whitespace, trim, trim_start
from proctor.errors import Error


class Ty:
    """Base of the type variants (`TyInt`, `TyStr`, ...)."""


@dataclass(frozen=True)
class TyInt(Ty):
    unsigned: bool


@dataclass(frozen=True)
class TyFloat(Ty):
    pass


@dataclass(frozen=True)
class TyBool(Ty):
    pass


@dataclass(frozen=True)
class TyChar(Ty):
    pass


@dataclass(frozen=True)
class TyStr(Ty):
    pass


@dataclass(frozen=True)
class TyStrList(Ty):
    """C `char **`: NULL-terminated array of strings."""


@dataclass(frozen=True)
class TyList(Ty):
    inner: Ty


@dataclass(frozen=True)
class TyOpt(Ty):
    inner: Ty


@dataclass(frozen=True)
class TyVoid(Ty):
    pass


@dataclass(frozen=True)
class TyOther(Ty):
    name: str


@dataclass(kw_only=True)
class Param:
    name: str
    ty: Ty
    # The parameter's type as written (C/Rust) or annotation (Python).
    raw: str
    has_default: bool


@dataclass(kw_only=True)
class Proto:
    # Full name as in the spec (may be dotted for Python).
    name: str
    params: list[Param]
    ret: Ty
    ret_raw: str
    # Canonical text compared by the prototype check.
    norm: str
    is_method: bool
    # The signature exactly as the spec wrote it.
    sig_text: str

    def short_name(self) -> str:
        return self.name.rsplit(".", 1)[-1].rsplit("::", 1)[-1]

    def owner(self) -> str | None:
        """The type of a Rust method or associated function (`Stack` for
        `Stack::push`); None for a free function."""
        return owner_of(self.name)


def owner_of(name: str) -> str | None:
    if "::" not in name:
        return None
    return name.rsplit("::", 1)[0].rsplit("::", 1)[-1]


def parse(lang: str, sig: str, name: str) -> Proto:
    short = name.rsplit(".", 1)[-1]
    text = sig
    owner = owner_of(short) if lang == "rust" else None
    if owner is not None:
        # `Self` and the type's name mean the same inside its `impl`.
        short = short.rsplit("::", 1)[-1]
        text = re.sub(r"\bSelf\b", owner, sig)
    match lang:
        case "c":
            p = parse_c(text)
        case "python":
            p = parse_py(text)
        case "rust":
            p = parse_rust(text)
        case _:
            p = parse_generic(text)
    if p.name != short:
        raise Error(f"signature `{sig}` does not declare `{short}`")
    return dataclasses.replace(p, name=name, sig_text=trim(sig))


def default_gen(t: Ty) -> gens.Gen | None:
    match t:
        case TyInt(unsigned=True):
            return gens.GenInt(lo=0, hi=1000, width=0)
        case TyInt(unsigned=False):
            return gens.GenInt(lo=-1000, hi=1000, width=0)
        case TyFloat():
            return gens.GenFloat(lo=-1000.0, hi=1000.0)
        case TyBool():
            return gens.GenBool()
        case TyChar():
            return gens.GenChar(alphabet=list(gens.DEFAULT_ALPHABET))
        case TyStr():
            return gens.GenStr(alphabet=list(gens.DEFAULT_ALPHABET), lo=0, hi=20)
        case TyStrList():
            return gens.GenList(
                inner=gens.GenStr(alphabet=list(gens.DEFAULT_ALPHABET), lo=0, hi=10),
                lo=0,
                hi=8,
            )
        case TyList(inner=inner):
            g = default_gen(inner)
            if g is None:
                return None
            return gens.GenList(inner=g, lo=0, hi=10)
        case TyOpt(inner=inner):
            g = default_gen(inner)
            if g is None:
                return None
            return gens.GenOpt(g)
    return None


# ------------------------------------------------------------------------- C


def c_tokens(s: str) -> list[str]:
    """Tokens of C declarations: identifiers, numbers, and single punctuation."""
    out: list[str] = []
    cs = s
    i = 0
    while i < len(cs):
        c = cs[i]
        if is_whitespace(c):
            i += 1
        elif c.isalnum() or c == "_":
            start = i
            while i < len(cs) and (cs[i].isalnum() or cs[i] == "_"):
                i += 1
            out.append(cs[start:i])
        elif c == "." and cs[i + 1 : i + 2] == "." and cs[i + 2 : i + 3] == ".":
            out.append("...")
            i += 3
        else:
            out.append(c)
            i += 1
    return out


C_TYPE_WORDS = [
    "void",
    "char",
    "short",
    "int",
    "long",
    "float",
    "double",
    "signed",
    "unsigned",
    "_Bool",
    "bool",
    "const",
    "volatile",
    "restrict",
    "struct",
    "union",
    "enum",
    "size_t",
    "ssize_t",
]


def is_ident(t: str) -> bool:
    return t != "" and (t[0].isalpha() or t[0] == "_")


def c_param_type(tokens: list[str]) -> tuple[list[str], str]:
    """Drops the parameter name from a C parameter's tokens; `[]` becomes `*`."""
    t = list(tokens)
    arrays = 0
    while t and t[-1] == "]":
        opens = [k for k, x in enumerate(t) if x == "["]
        if not opens:
            break
        del t[opens[-1] :]
        arrays += 1
    name = ""
    if len(t) >= 2:
        last = t[-1]
        before = t[-2]
        if (
            is_ident(last)
            and last not in C_TYPE_WORDS
            and before not in ("struct", "union", "enum")
        ):
            name = last
            t.pop()
    for _ in range(arrays):
        t.append("*")
    return (t, name)


def c_storage_free(tokens: list[str]) -> list[str]:
    return [t for t in tokens if t not in ("static", "inline", "extern", "__inline", "__inline__")]


_C_INTS = [
    "int",
    "long",
    "short",
    "unsigned",
    "signed",
    "char",
    "size_t",
    "ssize_t",
    "ptrdiff_t",
    "intptr_t",
    "uintptr_t",
    "int8_t",
    "int16_t",
    "int32_t",
    "int64_t",
    "uint8_t",
    "uint16_t",
    "uint32_t",
    "uint64_t",
]


def c_ty(tokens: list[str]) -> Ty:
    t = [x for x in tokens if x not in ("const", "volatile", "restrict")]
    joined = " ".join(t)
    stars = sum(1 for x in t if x == "*")
    base = [x for x in t if x != "*"]
    is_char = base == ["char"]
    if stars == 1 and is_char:
        return TyStr()
    if stars == 2 and is_char:
        return TyStrList()
    if stars == 0:
        if joined == "char":
            return TyChar()
        if joined == "void":
            return TyVoid()
        if joined in ("double", "float", "long double"):
            return TyFloat()
        if joined in ("bool", "_Bool"):
            return TyBool()
        if base and all(w in _C_INTS for w in base):
            unsigned = "unsigned" in base or any(
                w.startswith("uint") or w == "size_t" for w in base
            )
            return TyInt(unsigned)
        return TyOther(joined)
    return TyOther(joined)


def c_norm(ret: list[str], name: str, params: list[list[str]]) -> str:
    """Canonical C prototype text: `size_t str_len(const char *)`."""

    def join(t: list[str]) -> str:
        s = ""
        for i, x in enumerate(t):
            if i > 0 and not (x == "*" and t[i - 1] == "*"):
                s += " "
            s += x
        return s.replace("* ", "*")

    if not params or (len(params) == 1 and params[0] == ["void"]):
        ps = ["void"]
    else:
        ps = [join(p) for p in params]
    r = join(ret)
    sep = "" if r.endswith("*") else " "
    return f"{r}{sep}{name}({', '.join(ps)})"


def split_top(tokens: list[str], sep: str) -> list[list[str]]:
    out: list[list[str]] = [[]]
    depth = 0
    for t in tokens:
        if t in ("(", "[", "{", "<"):
            depth += 1
        elif t in (")", "]", "}", ">"):
            depth -= 1
        if depth == 0 and t == sep:
            out.append([])
        elif out:
            out[-1].append(t)
    if len(out) == 1 and not out[0]:
        out.clear()
    return out


def c_from_tokens(ret: list[str], name: str, inside: list[str]) -> Proto:
    """Parses C tokens `ret name ( params )` starting from a known name position."""
    ret = c_storage_free(ret)
    params: list[Param] = []
    types: list[list[str]] = []
    for i, p in enumerate(split_top(inside, ",")):
        if len(p) == 1 and p[0] == "void":
            continue
        ty_tokens, pname = c_param_type(p)
        params.append(
            Param(
                name=f"a{i}" if pname == "" else pname,
                ty=c_ty(ty_tokens),
                raw=" ".join(ty_tokens),
                has_default=False,
            )
        )
        types.append(ty_tokens)
    return Proto(
        name=name,
        norm=c_norm(ret, name, types),
        ret=c_ty(ret),
        ret_raw=" ".join(ret),
        params=params,
        is_method=False,
        sig_text="",
    )


def parse_c(sig: str) -> Proto:
    toks = c_tokens(trim(sig).rstrip(";"))
    open = next(
        (i for i, t in enumerate(toks) if t == "(" and i > 0 and is_ident(toks[i - 1])),
        None,
    )
    if open is None:
        raise Error(f"`{sig}` is not a C function prototype")
    close = matching(toks, open)
    if close is None:
        raise Error(f"unbalanced `(` in `{sig}`")
    return c_from_tokens(toks[: open - 1], toks[open - 1], toks[open + 1 : close])


def matching(toks: list[str], open: int) -> int | None:
    match toks[open]:
        case "(":
            o, c = "(", ")"
        case "[":
            o, c = "[", "]"
        case "{":
            o, c = "{", "}"
        case "<":
            o, c = "<", ">"
        case _:
            return None
    depth = 0
    for i in range(open, len(toks)):
        t = toks[i]
        if t == o:
            depth += 1
        elif t == c:
            depth -= 1
            if depth == 0:
                return i
    return None


# -------------------------------------------------------------------- Python


def py_ty(ann: str) -> Ty:
    a = "".join(c for c in ann if not is_whitespace(c))
    if a.endswith("|None"):
        return TyOpt(py_ty(a[: -len("|None")]))
    if a.startswith("None|"):
        return TyOpt(py_ty(a[len("None|") :]))

    def generic(prefixes: list[str]) -> str | None:
        for p in prefixes:
            if a.startswith(f"{p}["):
                r = a[len(p) + 1 :]
                if r.endswith("]"):
                    return r[:-1]
        return None

    inner = generic(["Optional", "typing.Optional"])
    if inner is not None:
        return TyOpt(py_ty(inner))
    inner = generic(
        [
            "list",
            "List",
            "Sequence",
            "Iterable",
            "typing.List",
            "Iterator",
            "Collection",
        ]
    )
    if inner is not None:
        return TyList(py_ty(inner))
    match a:
        case "int":
            return TyInt(unsigned=False)
        case "float":
            return TyFloat()
        case "bool":
            return TyBool()
        case "str":
            return TyStr()
        case "None":
            return TyVoid()
        case "":
            return TyOther("untyped")
        case other:
            return TyOther(other)


def split_depth(s: str, quotes: str = "\"'") -> list[str]:
    """Splits `s` at its top-level commas. Rust passes `quotes='"'`: there a
    `'` starts a lifetime, and parameters never hold a character literal."""
    out: list[str] = []
    cur = ""
    depth = 0
    quote: str | None = None
    for c in s:
        if quote is not None:
            if c == quote:
                quote = None
            cur += c
        elif c in quotes:
            quote = c
            cur += c
        elif c in ("(", "[", "{", "<"):
            depth += 1
            cur += c
        elif c == ">" and cur.endswith("-"):
            cur += c
        elif c in (")", "]", "}", ">"):
            depth -= 1
            cur += c
        elif c == "," and depth == 0:
            out.append(cur)
            cur = ""
        else:
            cur += c
    if trim(cur) != "":
        out.append(cur)
    return [trim(x) for x in out if trim(x) != ""]


def name_params_ret(s: str) -> tuple[str, str, str]:
    """Splits `name(params) -> ret` after the keyword; `params` must be balanced."""
    open = s.find("(")
    if open < 0:
        raise Error("missing `(`")
    name = trim(s[:open])
    depth = 0
    close = None
    for i in range(open, len(s)):
        c = s[i]
        if c == "(":
            depth += 1
        elif c == ")":
            depth -= 1
            if depth == 0:
                close = i
                break
    if close is None:
        raise Error("unbalanced `(`")
    return (name, s[open + 1 : close], trim(s[close + 1 :]))


def _trim_start_matches(s: str, pat: str) -> str:
    """Rust `trim_start_matches` with a string pattern: every repetition."""
    while pat and s.startswith(pat):
        s = s[len(pat) :]
    return s


def py_from_parts(name: str, params: str, ret: str) -> Proto:
    ps: list[Param] = []
    for raw in split_depth(params):
        if raw == "/" or raw == "*" or raw.startswith("*"):
            continue
        if "=" in raw:
            head, default = trim(raw.split("=", 1)[0]), True
        else:
            head, default = raw, False
        if ":" in head:
            n, a = head.split(":", 1)
            pname, ann = trim(n), trim(a)
        else:
            pname, ann = trim(head), ""
        if pname == "self" or pname == "cls":
            continue
        ps.append(Param(name=pname, ty=py_ty(ann), raw=ann, has_default=default))
    ret = trim(trim(_trim_start_matches(ret, "->")).rstrip(":"))
    required = sum(1 for p in ps if not p.has_default)
    return Proto(
        name=name,
        norm=f"{name}: {len(ps)} parameter(s), {required} required",
        ret=py_ty(ret),
        ret_raw=ret,
        params=ps,
        is_method=False,
        sig_text="",
    )


def parse_py(sig: str) -> Proto:
    s = trim(sig)
    s = trim(s.removeprefix("async "))
    if not s.startswith("def "):
        raise Error(f"`{sig}` must start with `def`")
    s = s[len("def ") :]
    name, params, ret = name_params_ret(s)
    return py_from_parts(name, params, ret)


# ---------------------------------------------------------------------- Rust


def rust_ty(t: str) -> Ty:
    t = strip_lifetimes(t)
    t = trim(t)
    if t.startswith("&mut"):
        t = t[len("&mut") :]
    elif t.startswith("&"):
        t = t[1:]
    t = trim(t)

    def inner(p: str) -> str | None:
        if not t.startswith(p):
            return None
        r = t[len(p) :]
        if not r.startswith("<"):
            return None
        r = r[1:]
        if not r.endswith(">"):
            return None
        return r[:-1]

    i = inner("Option")
    if i is not None:
        return TyOpt(rust_ty(i))
    i = inner("Vec")
    if i is not None:
        return TyList(rust_ty(i))
    if t.startswith("["):
        r = t[1:]
        if r.endswith("]") and ";" not in r[:-1]:
            return TyList(rust_ty(r[:-1]))
    match t:
        case "i8" | "i16" | "i32" | "i64" | "i128" | "isize":
            return TyInt(unsigned=False)
        case "u8" | "u16" | "u32" | "u64" | "u128" | "usize":
            return TyInt(unsigned=True)
        case "f32" | "f64":
            return TyFloat()
        case "bool":
            return TyBool()
        case "char":
            return TyChar()
        case "str" | "String":
            return TyStr()
        case "()" | "":
            return TyVoid()
        case other:
            return TyOther(other)


def strip_lifetimes(t: str) -> str:
    out = ""
    cs = t
    i = 0
    while i < len(cs):
        if cs[i] == "'":
            i += 1
            while i < len(cs) and (cs[i].isalnum() or cs[i] == "_"):
                i += 1
            while i < len(cs) and (cs[i] == " " or cs[i] == ","):
                i += 1
            continue
        out += cs[i]
        i += 1
    return out.replace("<>", "")


def squash(s: str) -> str:
    """Whitespace dropped, but one space kept between two words (`&mut W`,
    `impl Read`), lifetimes removed."""
    out = ""
    gap = False
    for c in strip_lifetimes(s):
        if is_whitespace(c):
            gap = True
            continue
        if gap and out and _word(out[-1]) and _word(c):
            out += " "
        gap = False
        out += c
    return out


def _word(c: str) -> bool:
    return c.isalnum() or c == "_"


_QUALIFIER = re.compile(r"\b(?:[A-Za-z_]\w*::)+(?=[A-Za-z_])")


def unqualified(norm: str) -> str:
    """A normalized signature with every type path cut to its last segment,
    so `std::io::Result<()>`, `io::Result<()>` and an imported `Result<()>`
    compare equal."""
    return _QUALIFIER.sub("", norm)


def rust_from_parts(name: str, params: str, ret: str) -> Proto:
    ps: list[Param] = []
    types: list[str] = []
    is_method = False
    for raw in split_depth(params, quotes='"'):
        if ":" not in raw:
            is_method = True
            types.append(squash(raw))
            continue
        p, t = raw.split(":", 1)
        pat, ty = trim(p), trim(t)
        pname = trim(_trim_start_matches(pat, "mut "))
        types.append(squash(ty))
        ps.append(Param(name=pname, ty=rust_ty(ty), raw=ty, has_default=False))
    ret = trim(ret)
    ret = ret.removeprefix("->")
    ret = trim(re.split(r"\bwhere\b", ret, maxsplit=1)[0])
    ret_n = squash(ret)
    arrow = "" if ret_n == "" or ret_n == "()" else f" -> {ret_n}"
    return Proto(
        name=name,
        norm=f"fn {name}({', '.join(types)}){arrow}",
        ret=rust_ty(ret),
        ret_raw=ret,
        params=ps,
        is_method=is_method,
        sig_text="",
    )


def parse_rust(sig: str) -> Proto:
    s = trim(trim(sig).rstrip(";").rstrip("{"))
    at = s.find("fn ")
    if at < 0:
        raise Error(f"`{sig}` must contain `fn name(...)`")
    rest = s[at + 3 :]
    name_end = next(
        (k for k, c in enumerate(rest) if not (c.isalnum() or c == "_")),
        len(rest),
    )
    name = rest[:name_end]
    tail = trim_start(rest[name_end:])
    if tail.startswith("<"):
        depth = 0
        end = 0
        for i, c in enumerate(tail):
            if c == "<":
                depth += 1
            elif c == ">":
                depth -= 1
                if depth == 0:
                    end = i + 1
                    break
        tail = tail[end:]
    _, params, ret = name_params_ret(f"{name}{tail}")
    return rust_from_parts(name, params, ret)


# ------------------------------------------------------------------- generic

_GENERIC_NAME = re.compile(r"([A-Za-z_][A-Za-z0-9_]*)\s*\(")


def parse_generic(sig: str) -> Proto:
    m = _GENERIC_NAME.search(sig)
    if m is None:
        raise Error(f"cannot find a function name in `{sig}`")
    name = m.group(1)
    return Proto(
        norm=name,
        name=name,
        params=[],
        ret=TyOther("unknown"),
        ret_raw="",
        is_method=False,
        sig_text="",
    )
