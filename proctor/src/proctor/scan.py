"""Source scanning: comment/string stripping, the Allowed (whitelist) check
and the prototype finder. Text-based by design: fast, no toolchain needed,
and good enough for students who play the game."""

from __future__ import annotations

import os
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

from proctor import defaults, sig
from proctor.compat import (
    from_utf8_lossy,
    is_whitespace,
    lines,
    read_text,
    regex,
    split_whitespace,
    trim,
    trim_end,
    trim_start,
)
from proctor.errors import Error
from proctor.unit import LangConfig


@dataclass(kw_only=True)
class Source:
    rel: str
    raw: str
    # Comments and literals blanked, newlines and offsets preserved (one
    # character of `code` per character of `raw`).
    code: str


@dataclass(kw_only=True)
class Violation:
    what: str
    at: str


IGNORED_DIRS: list[str] = [
    ".git",
    "target",
    "node_modules",
    "__pycache__",
    ".proctor",
    ".vscode",
    ".idea",
]


def walk(dir: Path) -> list[str]:
    """Every file under `dir`, as `/`-separated relative paths, sorted."""

    def go(base: Path, dir: Path, out: list[str]) -> None:
        try:
            rd = list(os.scandir(dir))
        except OSError:
            return
        for e in rd:
            p = Path(e.path)
            name = _lossy(e.name)
            if p.is_dir():
                if name not in IGNORED_DIRS:
                    go(base, p, out)
            else:
                try:
                    rel = p.relative_to(base)
                except ValueError:
                    continue
                out.append(_lossy(str(rel)).replace("\\", "/"))

    out: list[str] = []
    go(dir, dir, out)
    out.sort()
    return out


def sources(lang: str, ws: Path) -> list[Source]:
    return sources_ext(lang, ws, SrcOpts.from_config(defaults.preset(lang)))


@dataclass(kw_only=True)
class SrcOpts:
    """Source settings from the course's `languages:` block."""

    # Extra source extensions, without the dot.
    exts: list[str] = field(default_factory=list)
    # Line-comment marker for languages without a preset (`#`, `--`, `;`).
    comment: str | None = None

    @staticmethod
    def from_config(c: LangConfig) -> SrcOpts:
        return SrcOpts(
            exts=list(c.extensions) if c.extensions is not None else [],
            comment=c.comment,
        )


def has_stripper(lang: str) -> bool:
    """Languages with a dedicated comment/string stripper."""
    return defaults.canonical(lang) in (
        "c",
        "cpp",
        "python",
        "rust",
        "bash",
        "ruby",
        "javascript",
        "typescript",
        "go",
        "java",
    )


def sources_ext(lang: str, ws: Path, opts: SrcOpts) -> list[Source]:
    """Source files of lang; xtra adds extensions (from the course's
    languages: block), which matters for languages without a preset."""
    if not opts.exts:
        exts = defaults.preset(lang).extensions or []
    else:
        exts = [e.lstrip(".") for e in opts.exts]

    def wanted(rel: str) -> bool:
        if not exts:
            return True
        e = _extension(rel)
        return e is not None and any(x == e for x in exts)

    out: list[Source] = []
    for rel in walk(ws):
        if not wanted(rel):
            continue
        try:
            raw = read_text(Path(ws) / rel)
        except OSError:
            continue
        if opts.comment is not None and not has_stripper(lang):
            code = strip_line_comments(raw, opts.comment)
        else:
            code = strip(lang, raw)
        out.append(Source(rel=rel, raw=raw, code=code))
    return out


def line_of(text: str, idx: int) -> int:
    return text[: min(idx, len(text))].count("\n") + 1


# ------------------------------------------------------------------ stripping
#
# The strippers work on the UTF-8 bytes of the source as Rust does (see
# `_bytes`), so every decision (which bytes a literal spans, the byte windows
# of Rust char literals) is the Rust one; `_blanked` turns the result back
# into text with one character per character of the source.


def blank(b: list[str], from_: int, to: int) -> None:
    to = min(to, len(b))
    for x in range(min(from_, to), to):
        if b[x] != "\n":
            b[x] = " "


def is_ident_byte(c: str) -> bool:
    return c.isascii() and (c.isalnum() or c == "_")


def strip(lang: str, src: str) -> str:
    match lang:
        case "python":
            return strip_hash(src, True, False)
        case "bash" | "sh" | "shell":
            return strip_hash(src, False, True)
        case "ruby":
            return strip_hash(src, False, False)
        case "rust":
            return strip_c_like(src, True, False)
        case "javascript" | "js" | "typescript" | "ts" | "go":
            return strip_c_like(src, False, True)
        case _:
            return strip_c_like(src, False, False)


def strip_c_like(src: str, rust: bool, backtick: bool) -> str:
    """C, C++, Rust, Java, JS, Go: `//`, `/* */`, "strings", 'chars'.
    Rust: raw strings and lifetimes. JS/Go: backtick strings."""
    return strip_c_like_ex(src, rust, backtick, False)


def strip_c_like_ex(src: str, rust: bool, backtick: bool, keep_strings: bool) -> str:
    b = _bytes(src)
    n = len(b)
    i = 0
    while i < n:
        c = b[i]
        if c == "/" and i + 1 < n and b[i + 1] == "/":
            s = i
            while i < n and b[i] != "\n":
                i += 1
            blank(b, s, i)
        elif c == "/" and i + 1 < n and b[i + 1] == "*":
            s = i
            depth = 0
            while i < n:
                if b[i] == "/" and i + 1 < n and b[i + 1] == "*":
                    depth += 1
                    i += 2
                elif b[i] == "*" and i + 1 < n and b[i + 1] == "/":
                    depth -= 1
                    i += 2
                    if depth == 0 or not rust:
                        break
                else:
                    i += 1
            blank(b, s, i)
        elif (c == '"' or c == "`") and (c == '"' or backtick):
            q = c
            s = i
            i += 1
            while i < n and b[i] != q:
                if b[i] == "\\":
                    i += 1
                i += 1
            i = min(i + 1, n)
            if not keep_strings:
                blank(b, s + 1, max(i - 1, 0))
        elif (
            c == "r"
            and rust
            and (
                i == 0
                or not is_ident_byte(b[i - 1])
                or (b[i - 1] == "b" and (i < 2 or not is_ident_byte(b[i - 2])))
            )
        ):
            j = i + 1
            hashes = 0
            while j < n and b[j] == "#":
                hashes += 1
                j += 1
            if j < n and b[j] == '"':
                s = j
                j += 1
                while j < n:
                    if b[j] == '"' and all(
                        j + 1 + k < n and b[j + 1 + k] == "#" for k in range(hashes)
                    ):
                        j += 1 + hashes
                        break
                    j += 1
                if not keep_strings:
                    blank(b, s + 1, max(j - (1 + hashes), 0))
                i = j
            else:
                i += 1
        elif c == "'":
            s = i
            if rust:
                # Char literal only if it closes within a few bytes; else lifetime.
                close: int | None
                if i + 1 < n and b[i + 1] == "\\":
                    # The escaped character may itself be a quote: `'\''`.
                    close = next((j for j in range(i + 3, min(i + 12, n)) if b[j] == "'"), None)
                else:
                    close = next((j for j in range(i + 2, min(i + 6, n)) if b[j] == "'"), None)
                    if close is not None and not _one_char(b, i + 1, close):
                        close = None
                if close is not None:
                    if not keep_strings:
                        blank(b, s + 1, close)
                    i = close + 1
                else:
                    i += 1
            else:
                i += 1
                while i < n and b[i] != "'" and b[i] != "\n":
                    if b[i] == "\\":
                        i += 1
                    i += 1
                if not keep_strings:
                    blank(b, s + 1, i)
                i = min(i + 1, n)
        else:
            i += 1
    return _blanked(src, b)


def strip_hash(src: str, triple: bool, shell: bool) -> str:
    """Python, Ruby, shell: `#` comments, quotes; Python triple quotes; shell
    comments only at word start, and heredoc bodies."""
    return strip_hash_ex(src, triple, shell, False)


def strip_hash_ex(src: str, triple: bool, shell: bool, keep_strings: bool) -> str:
    b = _bytes(src)
    n = len(b)
    i = 0
    heredoc: str | None = None
    while i < n:
        if heredoc is not None:
            delim = heredoc
            # Skip to the next line and blank lines until the delimiter.
            while i < n and b[i] != "\n":
                i += 1
            start = i + 1
            while start < n:
                end = next((j for j in range(start, n) if b[j] == "\n"), n)
                line = trim(_text(b, start, end))
                if line == delim:
                    blank(b, i, end)
                    i = end
                    break
                start = end + 1
                if start >= n:
                    blank(b, i, n)
                    i = n
            heredoc = None
            continue
        c = b[i]
        if c == "#" and (not shell or i == 0 or b[i - 1] in (" ", "\t", "\n", ";")):
            s = i
            while i < n and b[i] != "\n":
                i += 1
            blank(b, s, i)
        elif c == "\\" and shell:
            i += 2
        elif c == '"' or c == "'":
            q = c
            s = i
            is_triple = triple and i + 2 < n and b[i + 1] == q and b[i + 2] == q
            if is_triple:
                i += 3
                while i + 2 < n and not (b[i] == q and b[i + 1] == q and b[i + 2] == q):
                    if b[i] == "\\":
                        i += 1
                    i += 1
                i = min(i + 3, n)
                if not keep_strings:
                    blank(b, s + 3, max(i - 3, 0))
            elif shell and q == '"':
                # Keep $(...) inside double quotes: it runs commands.
                i += 1
                depth = 0
                while i < n and not (b[i] == '"' and depth == 0):
                    if b[i] == "\\":
                        if depth == 0 and not keep_strings:
                            blank(b, i, i + 2)
                        i += 2
                        continue
                    if b[i] == "$" and i + 1 < n and b[i + 1] == "(":
                        depth += 1
                        i += 2
                        continue
                    if depth > 0 and b[i] == ")":
                        depth -= 1
                    elif depth == 0 and not keep_strings and b[i] != "\n":
                        b[i] = " "
                    i += 1
                i = min(i + 1, n)
            else:
                i += 1
                while i < n and b[i] != q:
                    if b[i] == "\\" and not (shell and q == "'"):
                        i += 1
                    if not shell and i < n and b[i] == "\n":
                        break
                    i += 1
                i = min(i + 1, n)
                if not keep_strings:
                    blank(b, s + 1, max(i - 1, 0))
        elif (
            c == "<"
            and shell
            and i + 1 < n
            and b[i + 1] == "<"
            and not (i + 2 < n and b[i + 2] == "<")
        ):
            j = i + 2
            if j < n and b[j] == "-":
                j += 1
            while j < n and b[j] == " ":
                j += 1
            s = j
            while j < n and b[j] not in (" ", "\n", ";", "|", "&", ">"):
                j += 1
            word = _text(b, s, j).strip("'\"")
            i = j
            if word:
                heredoc = word
        else:
            i += 1
    return _blanked(src, b)


def strip_line_comments(src: str, marker: str) -> str:
    """Generic stripper: blanks `marker` to end of line when it is outside a
    quoted string. String contents are kept: many languages import by string."""
    out: list[str] = []
    for line in _split_inclusive(src):
        quote: str | None = None
        prev = " "
        cut = False
        for i, c in enumerate(line):
            if cut:
                out.append("\n" if c == "\n" else " ")
                continue
            if quote is not None and c == quote and prev != "\\":
                quote = None
                out.append(c)
            elif quote is not None:
                out.append(c)
            elif c == '"' or c == "'":
                quote = c
                out.append(c)
            elif line.startswith(marker, i):
                cut = True
                out.append(" ")
            else:
                out.append(c)
            prev = c
    return "".join(out)


# ------------------------------------------------------------- allowed check


def allowed_by(path: str, allowed: set[str], seps: list[str]) -> bool:
    return any(path == e or any(path.startswith(f"{e}{s}") for s in seps) for e in allowed)


@dataclass(kw_only=True)
class Rules:
    """The whitelist and the forbidden items, both matched as prefixes: `std`
    covers `std::mem::take`, and forbidding `std::mem::swap` removes just that."""

    allowed: set[str]
    forbidden: set[str]

    def ok(self, name: str, seps: list[str]) -> bool:
        return allowed_by(name, self.allowed, seps) and not allowed_by(name, self.forbidden, seps)


@dataclass(kw_only=True)
class AllowedReport:
    checked: int
    violations: list[Violation]


def check_allowed(
    lang: str,
    ws: Path,
    allowed: list[str],
    extra: list[str],
    opts: SrcOpts,
) -> AllowedReport:
    return check_rules(lang, ws, allowed, [], extra, opts)


def is_test_source(rel: str) -> bool:
    """The student's own tests (a top-level `tests/` folder) may use anything:
    the rules are about the exercise's code."""
    return rel.startswith("tests/")


def check_rules(
    lang: str,
    ws: Path,
    allowed: list[str],
    forbidden: list[str],
    extra: list[str],
    opts: SrcOpts,
) -> AllowedReport:
    def set_(lst: list[str]) -> set[str]:
        return {trim(s) for s in lst}

    rules = Rules(allowed=set_(allowed), forbidden=set_(forbidden))
    srcs = [s for s in sources_ext(lang, ws, opts) if not is_test_source(s.rel)]
    r = AllowedReport(checked=0, violations=[])
    match lang:
        case "c" | "cpp" | "c++":
            allowed_c(ws, srcs, rules, r)
        case "python":
            allowed_python(ws, srcs, rules, r)
        case "rust":
            allowed_rust(ws, srcs, rules, r)
        case "bash" | "sh" | "shell":
            allowed_bash(ws, srcs, rules, r)
        case _:
            pass
    if extra:
        allowed_regex(lang, ws, srcs, rules, extra, r)
    # One report per name, the first place it appears.
    seen: set[str] = set()
    kept: list[Violation] = []
    for v in r.violations:
        if v.what not in seen:
            seen.add(v.what)
            kept.append(v)
    r.violations = kept
    return r


C_KEYWORDS: list[str] = [
    "if",
    "while",
    "for",
    "switch",
    "return",
    "sizeof",
    "_Alignof",
    "alignof",
    "_Generic",
    "_Static_assert",
    "static_assert",
    "case",
    "do",
    "else",
    "typeof",
    "__typeof__",
    "__attribute__",
    "__asm__",
    "asm",
    "defined",
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
    "struct",
    "union",
    "enum",
    "goto",
    "__declspec",
]


@dataclass(kw_only=True)
class CTok:
    text: str
    line: int


def c_toks(code: str) -> list[CTok]:
    out: list[CTok] = []
    line = 1
    cs = code
    i = 0
    while i < len(cs):
        c = cs[i]
        if c == "\n":
            line += 1
            i += 1
        elif is_whitespace(c):
            i += 1
        elif c.isalnum() or c == "_":
            s = i
            while i < len(cs) and (cs[i].isalnum() or cs[i] == "_"):
                i += 1
            out.append(CTok(text=cs[s:i], line=line))
        else:
            two = cs[i : min(i + 2, len(cs))]
            if two == "->" or two == "::":
                out.append(CTok(text=two, line=line))
                i += 2
            else:
                out.append(CTok(text=c, line=line))
                i += 1
    return out


def without_preprocessor(code: str) -> str:
    """Blanks preprocessor lines (keeps line count)."""
    return "\n".join("" if trim_start(ln).startswith("#") else ln for ln in lines(code))


@dataclass(kw_only=True)
class CDef:
    name: str
    norm: str
    line: int


def c_definitions(code: str) -> list[CDef]:
    toks = c_toks(without_preprocessor(code))
    texts = [t.text for t in toks]
    defs: list[CDef] = []
    depth = 0
    i = 0
    while i < len(toks):
        t = texts[i]
        if t == "{":
            depth += 1
        elif t == "}":
            depth -= 1
        elif (
            t == "("
            and depth == 0
            and i > 0
            and is_ident(texts[i - 1])
            and texts[i - 1] not in C_KEYWORDS
        ):
            close = sig.matching(texts, i)
            if close is not None and close + 1 < len(texts) and texts[close + 1] == "{":
                name = texts[i - 1]
                start = i - 1
                while start > 0 and texts[start - 1] not in (";", "}", "{", ")"):
                    start -= 1
                p = sig.c_from_tokens(texts[start : i - 1], name, texts[i + 1 : close])
                defs.append(CDef(name=name, norm=p.norm, line=toks[i].line))
                i = close
        i += 1
    return defs


def is_ident(t: str) -> bool:
    return t != "" and (t[0].isalpha() or t[0] == "_")


def allowed_c(ws: Path, srcs: list[Source], allowed: Rules, r: AllowedReport) -> None:
    own_files = {p.rsplit("/", 1)[-1] for p in walk(ws)}
    defined: set[str] = set()
    fnptrs: set[str] = set()
    define_re = re.compile(r"^\s*#\s*define\s+([A-Za-z_]\w*)")
    for s in srcs:
        defined.update(d.name for d in c_definitions(s.code))
        for line in lines(s.raw):
            c = define_re.search(line)
            if c is not None:
                defined.add(c[1])
        toks = c_toks(s.code)
        for w in zip(toks, toks[1:], toks[2:], toks[3:], strict=False):
            if w[0].text == "(" and w[1].text == "*" and is_ident(w[2].text) and w[3].text == ")":
                fnptrs.add(w[2].text)
    include_re = re.compile(r'^\s*#\s*include\s*([<"])([^>"]+)[>"]')
    for s in srcs:
        for n, (raw, code) in enumerate(zip(lines(s.raw), lines(s.code), strict=False)):
            if not trim_start(code).startswith("#"):
                continue
            c = include_re.search(raw)
            if c is None:
                continue
            header = trim(c[2])
            base = header.rsplit("/", 1)[-1]
            r.checked += 1
            # Quoted includes are the student's own headers by convention.
            own = c[1] == '"' or base in own_files
            if not own and not allowed.ok(header, []) and not allowed.ok(base, []):
                r.violations.append(Violation(what=f"#include <{header}>", at=f"{s.rel}:{n + 1}"))
        toks = c_toks(without_preprocessor(s.code))
        for i in range(max(len(toks) - 1, 0)):
            t = toks[i].text
            if not is_ident(t) or toks[i + 1].text != "(" or t in C_KEYWORDS:
                continue
            if i > 0 and toks[i - 1].text in (".", "->"):
                continue
            if t in defined or t in fnptrs:
                continue
            # A declaration like `char *foo(int);` that has no body is still a call target.
            r.checked += 1
            if not allowed.ok(t, []):
                r.violations.append(Violation(what=f"{t}()", at=f"{s.rel}:{toks[i].line}"))


def own_python_modules(ws: Path) -> set[str]:
    own: set[str] = set()
    for rel in walk(ws):
        parts = rel.split("/")
        if rel.endswith(".py"):
            stem = rel[: -len(".py")]
            own.add(stem.rsplit("/", 1)[-1])
        if len(parts) > 1:
            own.add(parts[0])
    return own


def allowed_python(ws: Path, srcs: list[Source], allowed: Rules, r: AllowedReport) -> None:
    own = own_python_modules(ws)
    import_re = re.compile(r"(?m)^[ \t]*import[ \t]+([^\n]+)")
    from_re = re.compile(r"(?m)^[ \t]*from[ \t]+([\w.]+)[ \t]+import[ \t]+(\([^)]*\)|[^\n]+)")

    def check(path: str, s: Source, idx: int, r: AllowedReport) -> None:
        root = path.split(".")[0]
        r.checked += 1
        if path.startswith(".") or root in own or root == "__future__":
            return
        if not allowed.ok(path, ["."]):
            r.violations.append(
                Violation(what=f"import {path}", at=f"{s.rel}:{line_of(s.code, idx)}")
            )

    for s in srcs:
        for c in import_re.finditer(s.code):
            at = c.start()
            for item in c[1].split(","):
                name = _first_word(item)
                if name:
                    check(name, s, at, r)
        for c in from_re.finditer(s.code):
            at = c.start()
            module = c[1]
            if module.startswith("."):
                r.checked += 1
                continue
            names = c[2].strip("()")
            for item in names.split(","):
                name = _first_word(item)
                if not name or name == "\\":
                    continue
                path = module if name == "*" else f"{module}.{name}"
                check(path, s, at, r)


def expand_use(prefix: str, tree: str, out: list[str]) -> None:
    """Expands a `use` tree (`std::{fmt, io::Read as R}`) into full paths."""
    t = _trim_start_matches(trim(tree), "::")

    def join(a: str, b: str) -> str:
        return b if a == "" else f"{a}::{b}"

    open_ = t.find("{")
    if open_ >= 0:
        base = trim(_trim_end_matches(t[:open_], "::"))
        inner = trim_end(t[open_ + 1 :]).rstrip("}")
        full = join(prefix, base)
        depth = 0
        cur = ""
        for c in inner:
            if c == "{":
                depth += 1
                cur += c
            elif c == "}":
                depth -= 1
                cur += c
            elif c == "," and depth == 0:
                expand_use(full, cur, out)
                cur = ""
            else:
                cur += c
        if trim(cur):
            expand_use(full, cur, out)
        return
    item = trim(t.split(" as ")[0])
    if not item:
        return
    if item == "self" or item == "*":
        out.append(prefix)
    else:
        out.append(join(prefix, item))


def cargo_manifests(ws: Path) -> list[str]:
    """Every `Cargo.toml` of the workspace, the root's first."""
    found = [f for f in walk(ws) if f == "Cargo.toml" or f.endswith("/Cargo.toml")]
    return sorted(found, key=lambda f: (f != "Cargo.toml", f))


def cargo_package_name(ws: Path, manifest: str = "Cargo.toml") -> str | None:
    try:
        text = read_text(Path(ws) / manifest)
    except OSError:
        return None
    in_pkg = False
    for line in lines(text):
        t = trim(line)
        if t.startswith("["):
            in_pkg = t == "[package]"
        elif in_pkg and t.startswith("name"):
            rest = trim_start(t[len("name") :])
            if rest.startswith("="):
                return trim(rest[1:]).strip('"')
    return None


@dataclass(kw_only=True)
class CargoDep:
    name: str
    line: int
    # A `path` dependency: a crate of the student's own workspace.
    local: bool


def cargo_dependencies(ws: Path, manifest: str = "Cargo.toml") -> list[CargoDep]:
    try:
        text = read_text(Path(ws) / manifest)
    except OSError:
        return []
    out: list[CargoDep] = []
    in_deps = False
    table: CargoDep | None = None
    path_key = re.compile(r"(?:^|[{,\s])path\s*=")
    for i, line in enumerate(lines(text)):
        t = trim(line)
        if t.startswith("["):
            h = trim(t.strip("[]"))
            table = None
            if "dependencies." in h:
                name = h.split("dependencies.", 1)[1]
                table = CargoDep(name=name, line=i + 1, local=False)
                out.append(table)
                in_deps = False
            else:
                in_deps = h.endswith("dependencies")
        elif table is not None and path_key.match(t):
            table.local = True
        elif in_deps and t and not t.startswith("#") and "=" in t:
            k, v = t.split("=", 1)
            k = trim(k)
            local = bool(path_key.search(v))
            # Dotted keys: `name.path = "../x"`, `name.workspace = true`.
            if not k.startswith('"') and "." in k:
                k, sub = k.split(".", 1)
                local = local or trim(sub) == "path"
            out.append(CargoDep(name=trim(k).strip('"'), line=i + 1, local=local))
    return out


def allowed_rust(ws: Path, srcs: list[Source], allowed: Rules, r: AllowedReport) -> None:
    # The workspace's own crates (members, path dependencies) are the student's code.
    crates: set[str] = set()
    deps: list[tuple[str, CargoDep]] = []
    for manifest in cargo_manifests(ws):
        p = cargo_package_name(ws, manifest)
        if p is not None:
            crates.add(p.replace("-", "_"))
        deps.extend((manifest, d) for d in cargo_dependencies(ws, manifest))
    # `name.workspace = true` points at a `[workspace.dependencies]` entry.
    local = {d.name for _, d in deps if d.local}
    crates.update(n.replace("-", "_") for n in local)
    own: set[str] = {"crate", "self", "super", "Self", *crates}
    mod_re = re.compile(r"\bmod\s+([A-Za-z_]\w*)")
    for s in srcs:
        for c in mod_re.finditer(s.code):
            own.add(c[1])
    for manifest, d in deps:
        r.checked += 1
        if d.name in local:
            continue
        if not allowed.ok(d.name, []) and not allowed.ok(d.name.replace("-", "_"), []):
            r.violations.append(Violation(what=f"crate `{d.name}`", at=f"{manifest}:{d.line}"))
    use_re = re.compile(r"\buse\s+([^;]+);")
    path_re = re.compile(r"\b(?:std|core|alloc)(?:::[A-Za-z_]\w*)+")
    extern_re = re.compile(r"\bextern\s+crate\s+([A-Za-z_]\w*)")
    short_re = re.compile(r"\b([A-Za-z_]\w*)((?:::[A-Za-z_]\w*)+)")
    for s in srcs:
        found: list[tuple[str, int]] = []
        for c in use_re.finditer(s.code):
            at = c.start()
            paths: list[str] = []
            expand_use("", c[1], paths)
            found.extend((q, at) for q in paths)
        use_spans = [(m.start(), m.end()) for m in use_re.finditer(s.code)]
        for m in path_re.finditer(s.code):
            if not any(a <= m.start() < b for a, b in use_spans):
                found.append((m[0], m.start()))
        for c in extern_re.finditer(s.code):
            found.append((c[1], c.start()))
        # `use std::mem;` then `mem::swap(..)`: the short path is std::mem::swap,
        # which matters for forbidden items (the module itself was checked above).
        aliases: list[tuple[str, str]] = []
        for q, _ in found:
            if " as " in q:
                path, alias = q.split(" as ", 1)
                path, name = trim(path), trim(alias)
            else:
                path, name = q, q.rsplit("::", 1)[-1]
            if "::" in path and name != "self" and name != "*":
                aliases.append((name, path))
        for c in short_re.finditer(s.code):
            start = c.start()
            if any(a <= start < b for a, b in use_spans):
                continue
            full = next((f for n, f in aliases if n == c[1]), None)
            if full is not None:
                path = f"{full}{c[2]}"
                if allowed_by(path, allowed.forbidden, ["::"]):
                    r.violations.append(
                        Violation(what=path, at=f"{s.rel}:{line_of(s.code, start)}")
                    )
        for path, at in found:
            root = path.split("::")[0]
            r.checked += 1
            if root in own:
                continue
            if not allowed.ok(path, ["::"]):
                r.violations.append(Violation(what=path, at=f"{s.rel}:{line_of(s.code, at)}"))


SHELL_BUILTINS: list[str] = [
    "echo",
    "printf",
    "read",
    "cd",
    "pwd",
    "test",
    "[",
    "[[",
    "]]",
    "export",
    "local",
    "declare",
    "typeset",
    "readonly",
    "set",
    "unset",
    "shift",
    "return",
    "exit",
    "source",
    ".",
    "eval",
    "exec",
    "trap",
    "wait",
    "true",
    "false",
    ":",
    "let",
    "getopts",
    "mapfile",
    "readarray",
    "alias",
    "unalias",
    "type",
    "command",
    "builtin",
    "hash",
    "pushd",
    "popd",
    "dirs",
    "umask",
    "ulimit",
    "times",
    "jobs",
    "fg",
    "bg",
    "disown",
    "shopt",
    "enable",
    "help",
    "history",
    "caller",
    "break",
    "continue",
    "compgen",
    "complete",
    "kill",
    "if",
    "then",
    "else",
    "elif",
    "fi",
    "for",
    "in",
    "do",
    "done",
    "while",
    "until",
    "case",
    "esac",
    "function",
    "select",
    "time",
    "coproc",
    "!",
    "{",
    "}",
    "((",
    "))",
]


def allowed_bash(ws: Path, srcs: list[Source], allowed: Rules, r: AllowedReport) -> None:
    own_files = set(walk(ws))
    func_re = re.compile(r"(?m)^\s*(?:function\s+)?([A-Za-z_][\w-]*)\s*\(\s*\)")
    func_kw = re.compile(r"(?m)\bfunction\s+([A-Za-z_][\w-]*)")
    funcs: set[str] = set()
    for s in srcs:
        for m in [*func_re.finditer(s.code), *func_kw.finditer(s.code)]:
            funcs.add(m[1])
    for s in srcs:
        # Tokenize into words and separators, tracking lines.
        words: list[tuple[str, int, bool]] = []  # (text, line, is_separator)
        line = 1
        cur = ""
        bytes_ = s.code
        i = 0

        def flush(cur: str, words: list[tuple[str, int, bool]], line: int) -> str:
            if cur:
                words.append((cur, line, False))
            return ""

        while i < len(bytes_):
            c = bytes_[i]
            nxt = bytes_[i + 1 : i + 2]
            if c == "\n":
                cur = flush(cur, words, line)
                words.append(("\n", line, True))
                line += 1
            elif c == " " or c == "\t":
                cur = flush(cur, words, line)
            # Arithmetic `$(( ))` / `(( ))`: no commands inside.
            elif (
                (c == "$" or c == "(") and nxt == "(" and (c == "(" or bytes_[i + 2 : i + 3] == "(")
            ):
                depth = 0
                while i < len(bytes_):
                    match bytes_[i]:
                        case "(":
                            depth += 1
                        case ")":
                            depth -= 1
                            if depth == 0:
                                break
                        case "\n":
                            line += 1
                        case _:
                            pass
                    i += 1
                cur += "$arith"
            elif c in (";", "|", "&", "(", ")", "`"):
                cur = flush(cur, words, line)
                words.append((c, line, True))
            elif c == "$" and nxt == "(":
                cur = flush(cur, words, line)
                words.append(("$(", line, True))
                i += 1
            else:
                cur += c
            i += 1
        cur = flush(cur, words, line)
        at_cmd = True
        in_case = 0
        after_in = False
        idx = 0
        while idx < len(words):
            w, ln, sep = words[idx]
            if sep:
                if w != ")" or in_case == 0:
                    at_cmd = True
                if w == ")" and in_case > 0:
                    at_cmd = True
                idx += 1
                continue
            if w == "case":
                in_case += 1
                after_in = False
            elif w == "esac":
                in_case -= 1
            elif w == "in" and in_case > 0:
                after_in = True
            if at_cmd:
                next_is_paren = idx + 1 < len(words) and (
                    words[idx + 1][2] and words[idx + 1][0] == ")"
                )
                is_pattern = in_case > 0 and after_in and next_is_paren
                assignment = "=" in w and not w.startswith("=")
                if is_pattern or assignment:
                    idx += 1
                    continue
                keyword_continues = w in (
                    "if",
                    "then",
                    "else",
                    "elif",
                    "do",
                    "while",
                    "until",
                    "!",
                    "time",
                    "{",
                    "exec",
                    "command",
                )
                name = w.strip("\"'")
                skip = (
                    not name
                    or name in SHELL_BUILTINS
                    or name in funcs
                    or name.startswith("$")
                    or name.startswith("-")
                    or name.startswith("[")
                    or name.startswith("<")
                    or name.startswith(">")
                    or "0" <= name[0] <= "9"
                    or name.startswith("((")
                    or name == "fi"
                    or name == "done"
                    or name == "esac"
                    or name == ";;"
                )
                local = "/" in name and _trim_start_matches(name, "./") in own_files
                if not skip and not local:
                    r.checked += 1
                    base = name.rsplit("/", 1)[-1]
                    if not allowed.ok(name, []) and not allowed.ok(base, []):
                        r.violations.append(Violation(what=f"command `{name}`", at=f"{s.rel}:{ln}"))
                at_cmd = keyword_continues
            idx += 1


def allowed_regex(
    lang: str,
    _ws: Path,
    srcs: list[Source],
    allowed: Rules,
    patterns: list[str],
    r: AllowedReport,
) -> None:
    for p in patterns:
        try:
            pat = regex(p)
        except re.error as e:
            raise Error(f"bad import pattern `{p}`: {e}") from None
        for s in srcs:
            # Imports live in strings for JS/Go, so match on comment-free raw text.
            if lang in ("javascript", "js", "typescript", "ts", "go", "ruby"):
                text = strip_comments_only(lang, s.raw)
            else:
                text = s.code
            for c in pat.finditer(text):
                m = c.group(1) if pat.groups >= 1 else None
                if m is None:
                    continue
                name = _trim_start_matches(trim(m), "node:")
                r.checked += 1
                if name.startswith(".") or name.startswith("/"):
                    continue
                if not allowed.ok(name, [".", "/", "::"]):
                    r.violations.append(
                        Violation(
                            what=f"import {name}",
                            at=f"{s.rel}:{line_of(text, c.start(1))}",
                        )
                    )


def strip_comments_only(lang: str, raw: str) -> str:
    match lang:
        case "ruby":
            return strip_hash_ex(raw, False, False, True)
        case _:
            return strip_c_like_ex(raw, False, True, True)


# ----------------------------------------------------------- prototype finder


class ProtoFinding:
    """Base of `ProtoFindingOk`, `ProtoFindingMissing`, `ProtoFindingMismatch`."""


@dataclass(frozen=True)
class ProtoFindingOk(ProtoFinding):
    pass


@dataclass(frozen=True)
class ProtoFindingMissing(ProtoFinding):
    pass


@dataclass(frozen=True)
class ProtoFindingMismatch(ProtoFinding):
    msg: str


def find_prototype(lang: str, srcs: list[Source], expected: sig.Proto) -> ProtoFinding:
    name = expected.short_name()
    match lang:
        case "c":
            found: list[tuple[str, str]] = []
            for s in (s for s in srcs if s.rel.endswith(".c")):
                for d in c_definitions(s.code):
                    if d.name == name:
                        found.append((d.norm, f"{s.rel}:{d.line}"))
            return judge(found, expected.norm)
        case "python":
            pat = re.compile(rf"(?m)^[ \t]*(?:async[ \t]+)?def[ \t]+{re.escape(name)}[ \t]*\(")
            found = []
            for s in srcs:
                for m in pat.finditer(s.code):
                    open_ = m.end() - 1
                    params = balanced(s.code[open_:])
                    if params is None:
                        continue
                    p = sig.py_from_parts(name, params, "")
                    compatible = len(p.params) == len(expected.params)
                    norm = expected.norm if compatible else p.norm
                    found.append((norm, f"{s.rel}:{line_of(s.code, m.start())}"))
            return judge(found, expected.norm)
        case "rust":
            pat = re.compile(rf"\bfn\s+{re.escape(name)}\b")
            # A method counts only inside an `impl` block of its type.
            owner = expected.owner()
            found = []
            for s in srcs:
                blocks = impl_blocks(s.code, owner) if owner is not None else None
                for m in pat.finditer(s.code):
                    if blocks is not None and not any(a < m.start() < b for a, b in blocks):
                        continue
                    tail = s.code[m.start() :]
                    depth = 0
                    end = len(tail)
                    for i, c in enumerate(tail):
                        if c in ("(", "<", "["):
                            depth += 1
                        elif c in (")", ">", "]"):
                            depth -= 1
                        elif c in ("{", ";") and depth <= 0:
                            end = i
                            break
                    # `->` contains '>' which unbalances depth; recompute ignoring arrows.
                    text = tail[:end]
                    text = cut_body(tail) if "->" in text else text
                    if owner is not None:
                        text = re.sub(r"\bSelf\b", owner, text)
                    try:
                        p = sig.parse("rust", text, name)
                    except Error:
                        continue
                    found.append((p.norm, f"{s.rel}:{line_of(s.code, m.start())}"))
            return judge(found, expected.norm)
        case _:
            pat = re.compile(rf"\b{re.escape(name)}\b")
            if any(pat.search(s.code) for s in srcs):
                return ProtoFindingOk()
            return ProtoFindingMissing()


def cut_body(tail: str) -> str:
    """Text of a Rust item header up to its body `{` or `;`, `->` aware."""
    cleaned = tail.replace("->", "~~")
    depth = 0
    for i, c in enumerate(cleaned):
        if c in ("(", "<", "["):
            depth += 1
        elif c in (")", ">", "]"):
            depth -= 1
        elif c in ("{", ";") and depth <= 0:
            return tail[:i]
    return tail


def body_open(code: str, i: int) -> int | None:
    """Index of the `{` opening the body of the Rust item whose header starts
    at `i` (comments and literals blanked); None for an item ending in `;`."""
    depth = 0
    while i < len(code):
        c = code[i]
        if c in "([":
            depth += 1
        elif c in ")]":
            depth -= 1
        elif c == "{" and depth <= 0:
            return i
        elif c == ";" and depth <= 0:
            return None
        i += 1
    return None


def matching(code: str, open_: int) -> int | None:
    """Index of the `}` closing the `{` at `open_`."""
    depth = 0
    for i in range(open_, len(code)):
        if code[i] == "{":
            depth += 1
        elif code[i] == "}":
            depth -= 1
            if depth == 0:
                return i
    return None


def impl_blocks(code: str, owner: str | None) -> list[tuple[int, int]]:
    """Spans (`{` to `}`) of the Rust `impl` blocks for type `owner`, trait
    impls included; every `impl` block when `owner` is None."""
    out: list[tuple[int, int]] = []
    for m in re.finditer(r"\bimpl\b", code):
        before = code[: m.start()].rstrip()
        # An item, not `-> impl Trait` in a signature.
        if before and before[-1] not in "{};]" and not re.search(r"\bunsafe$", before):
            continue
        open_ = body_open(code, m.end())
        if open_ is None:
            continue
        ty = re.split(r"\bfor\b", code[m.end() : open_])[-1]
        if owner is not None and not re.search(rf"\b{re.escape(owner)}\b", ty):
            continue
        close = matching(code, open_)
        if close is not None:
            out.append((open_, close))
    return out


def balanced(s: str) -> str | None:
    depth = 0
    for i, c in enumerate(s):
        if c == "(":
            depth += 1
        elif c == ")":
            depth -= 1
            if depth == 0:
                return s[1:i]
    return None


def judge(found: list[tuple[str, str]], expected: str) -> ProtoFinding:
    if not found:
        return ProtoFindingMissing()
    if any(n == expected or sig.unqualified(n) == sig.unqualified(expected) for n, _ in found):
        return ProtoFindingOk()
    n, at = found[0]
    return ProtoFindingMismatch(f"found `{n}` at {at}")


def c_defined_names(srcs: list[Source]) -> dict[str, str]:
    """Names of functions defined in C sources (for stubs and the harness)."""
    m: dict[str, str] = {}
    for s in srcs:
        for d in c_definitions(s.code):
            m.setdefault(d.name, s.rel)
    return dict(sorted(m.items()))


def files_with_main(lang: str, srcs: list[Source]) -> list[Path]:
    def has_main(s: Source) -> bool:
        match lang:
            case "c":
                return any(d.name == "main" for d in c_definitions(s.code))
            case _:
                return False

    return [Path(s.rel) for s in srcs if has_main(s)]


# ------------------------------------------------------------- port helpers


def _lossy(s: str) -> str:
    """`OsStr::to_string_lossy`: what a file name cannot hold as UTF-8 (POSIX
    bytes, Windows lone surrogates) becomes U+FFFD."""
    if sys.platform == "win32":
        return s.encode("utf-16-le", "surrogatepass").decode("utf-16-le", "replace")
    return os.fsencode(s).decode("utf-8", "replace")


def _extension(rel: str) -> str | None:
    """Rust `Path::extension` of a `/`-separated relative path: `None` without
    a dot, or when the only dot starts the name (`.bashrc`); `""` for `a.`."""
    name = rel.rsplit("/", 1)[-1]
    if name == "..":
        return None
    i = name.rfind(".")
    if i <= 0:
        return None
    return name[i + 1 :]


def _split_inclusive(s: str) -> list[str]:
    """Rust `str::split_inclusive('\\n')`: lines with their `\\n`, no empty last part."""
    out: list[str] = []
    start = 0
    while True:
        j = s.find("\n", start)
        if j < 0:
            break
        out.append(s[start : j + 1])
        start = j + 1
    if start < len(s):
        out.append(s[start:])
    return out


def _trim_start_matches(s: str, pat: str) -> str:
    """Rust `trim_start_matches` with a string: every leading repetition."""
    while pat and s.startswith(pat):
        s = s[len(pat) :]
    return s


def _trim_end_matches(s: str, pat: str) -> str:
    """Rust `trim_end_matches` with a string: every trailing repetition."""
    while pat and s.endswith(pat):
        s = s[: -len(pat)]
    return s


def _first_word(s: str) -> str:
    """`s.split_whitespace().next().unwrap_or("")`."""
    words = split_whitespace(s)
    return words[0] if words else ""


def _bytes(src: str) -> list[str]:
    """The UTF-8 bytes of `src`, one per item as a one-character string
    (`\\x00`..`\\xff`), so a stripper can walk bytes as the Rust one does and
    still compare with `"/"` or `"\\n"` (bytes of multi-byte characters are
    never ASCII)."""
    return list(src.encode("utf-8").decode("latin-1"))


def _text(b: list[str], lo: int, hi: int) -> str:
    """`String::from_utf8_lossy(&b[lo..hi])`."""
    return from_utf8_lossy("".join(b[lo:hi]).encode("latin-1"))


def _one_char(b: list[str], lo: int, hi: int) -> bool:
    """`std::str::from_utf8(&b[lo..hi]).is_ok_and(|t| t.chars().count() == 1)`."""
    try:
        t = "".join(b[lo:hi]).encode("latin-1").decode("utf-8")
    except UnicodeDecodeError:
        return False
    return len(t) == 1


def _utf8_len(c: str) -> int:
    o = ord(c)
    return 1 if o < 0x80 else 2 if o < 0x800 else 3 if o < 0x10000 else 4


def _blanked(src: str, b: list[str]) -> str:
    """The stripped bytes `b` of `src` back as text, one character per
    character of `src`, so offsets and newlines in it are those of `src`: a
    character whose bytes were all blanked becomes one space, one blanked only
    in part (a literal left open at the end of the file) U+FFFD, as
    `from_utf8_lossy` shows its leftover bytes."""
    if len(b) == len(src):
        return "".join(b)
    out: list[str] = []
    o = 0
    for c in src:
        k = _utf8_len(c)
        if k == 1:
            out.append(b[o])
        else:
            part = b[o : o + k]
            if " " not in part:
                out.append(c)
            elif all(x == " " for x in part):
                out.append(" ")
            else:
                out.append("\ufffd")
        o += k
    return "".join(out)
