"""Rust behaviour where Python's built-ins differ: whitespace and line
splitting, `{:?}` and float formatting, strict number parsing, integer
division, UTF-8 files without newline translation, and io error messages.

Courses, journals and caches written by the Rust tools must read the same,
and what students see must not change, so the port uses these helpers
instead of `str.split()`, `str.splitlines()`, `str.strip()`, `repr()`,
`int()`, `float()`, `Path.read_text()` and `Path.write_text()`."""

from __future__ import annotations

import decimal
import math
import os
import re
import sys
import unicodedata
from pathlib import Path

# Unicode White_Space, as Rust's `char::is_whitespace` and `str::trim` use it.
# Python's `str.isspace()` also counts U+001C..U+001F, which Rust does not.
WHITESPACE = (
    "\t\n\x0b\x0c\r \x85\xa0\u1680\u2000\u2001\u2002\u2003\u2004\u2005\u2006"
    "\u2007\u2008\u2009\u200a\u2028\u2029\u202f\u205f\u3000"
)
ASCII_WHITESPACE = " \t\n\x0c\r"

_WS_RUN = re.compile("[" + re.escape(WHITESPACE) + "]+")
_ASCII_WS_RUN = re.compile("[" + re.escape(ASCII_WHITESPACE) + "]+")

I128_MIN = -(1 << 127)
I128_MAX = (1 << 127) - 1
U64_MASK = (1 << 64) - 1


def is_whitespace(c: str) -> bool:
    """Rust `char::is_whitespace`."""
    return c in WHITESPACE


def trim(s: str) -> str:
    """Rust `str::trim`."""
    return s.strip(WHITESPACE)


def trim_start(s: str) -> str:
    """Rust `str::trim_start`."""
    return s.lstrip(WHITESPACE)


def trim_end(s: str) -> str:
    """Rust `str::trim_end`."""
    return s.rstrip(WHITESPACE)


def split_whitespace(s: str) -> list[str]:
    """Rust `str::split_whitespace`."""
    return [p for p in _WS_RUN.split(s) if p]


def split_ascii_whitespace(s: str) -> list[str]:
    """Rust `str::split_ascii_whitespace` (space, tab, LF, FF, CR; not VT)."""
    return [p for p in _ASCII_WS_RUN.split(s) if p]


def lines(s: str) -> list[str]:
    """Rust `str::lines`: split at `\\n`, drop one `\\r` before it, no final
    empty line. Unlike `str.splitlines()`, no other character ends a line."""
    if not s:
        return []
    parts = s.split("\n")
    if parts[-1] == "":
        parts.pop()
        return [p[:-1] if p.endswith("\r") else p for p in parts]
    last = parts.pop()
    return [p[:-1] if p.endswith("\r") else p for p in parts] + [last]


def byte_len(s: str) -> int:
    """Rust `str::len`: the length in UTF-8 bytes."""
    return len(s.encode("utf-8"))


def from_utf8_lossy(b: bytes) -> str:
    """Rust `String::from_utf8_lossy`."""
    return b.decode("utf-8", "replace")


# ------------------------------------------------------------------ formatting


def _escape_char(c: str, quote: str) -> str:
    """One char as Rust's `escape_debug` writes it inside `quote` quotes."""
    special = {"\0": "\\0", "\t": "\\t", "\r": "\\r", "\n": "\\n", "\\": "\\\\"}
    if c in special:
        return special[c]
    if c == quote:
        return "\\" + c
    if not c.isprintable() or unicodedata.category(c) in ("Mn", "Me"):
        return f"\\u{{{ord(c):x}}}"
    return c


def debug_str(s: str) -> str:
    """Rust `format!("{s:?}")` for a string."""
    return '"' + "".join(_escape_char(c, '"') for c in s) + '"'


def debug_char(c: str) -> str:
    """Rust `format!("{c:?}")` for a char."""
    return "'" + _escape_char(c, "'") + "'"


def float_display(x: float) -> str:
    """Rust `format!("{x}")` for an f64: the shortest digits that read back
    as `x`, never in exponent form (`1e20` prints all 21 digits, `1.0` is
    `1`)."""
    if math.isnan(x):
        return "NaN"
    if math.isinf(x):
        return "inf" if x > 0 else "-inf"
    if x == 0:
        return "-0" if math.copysign(1.0, x) < 0 else "0"
    s = format(decimal.Decimal(repr(x)), "f")
    if "." in s:
        s = s.rstrip("0").rstrip(".")
    return s


def float_exp(x: float) -> str:
    """Rust `format!("{x:e}")` for an f64 (`1e2`, `1.5e0`, `-2.5e-3`)."""
    if math.isnan(x):
        return "NaN"
    if math.isinf(x):
        return "inf" if x > 0 else "-inf"
    sign = "-" if math.copysign(1.0, x) < 0 else ""
    if x == 0:
        return sign + "0e0"
    # repr gives the shortest digits; a Decimal of it never has leading zeros.
    t = decimal.Decimal(repr(abs(x))).as_tuple()
    digits = "".join(str(d) for d in t.digits)
    assert isinstance(t.exponent, int)
    exp = t.exponent + len(digits) - 1
    digits = digits.rstrip("0") or "0"
    mantissa = digits[0] + ("." + digits[1:] if len(digits) > 1 else "")
    return f"{sign}{mantissa}e{exp}"


# ---------------------------------------------------------------------- regex

_regex_cache: dict[str, re.Pattern[str]] = {}


def regex(pattern: str) -> re.Pattern[str]:
    """A pattern in the Rust `regex` crate's syntax, compiled for `re`.

    Course specs and models write Rust syntax, where `$` outside multi-line
    mode matches only at the very end of the text (Python's also matches
    before a final newline: `out ~ "^ok$"` would accept `"ok\\n"`), `\\z` is
    the end of text and `(?<name>...)` names a group. Raises `re.error`."""
    hit = _regex_cache.get(pattern)
    if hit is not None:
        return hit
    multiline = bool(re.search(r"\(\?[a-zA-Z]*m[a-zA-Z]*[):]", pattern))
    out: list[str] = []
    i, in_class = 0, False
    while i < len(pattern):
        c = pattern[i]
        if c == "\\" and i + 1 < len(pattern):
            nxt = pattern[i + 1]
            out.append("\\Z" if nxt == "z" and not in_class else c + nxt)
            i += 2
            continue
        if in_class:
            if c == "]":
                in_class = False
            out.append(c)
        elif c == "[":
            in_class = True
            out.append(c)
            # A `]` right after `[` or `[^` is a literal.
            for lit in ("^]", "]"):
                if pattern.startswith(lit, i + 1):
                    out.append(lit)
                    i += len(lit)
                    break
        elif c == "$" and not multiline:
            out.append("\\Z")
        elif pattern.startswith("(?<", i) and pattern[i + 3 : i + 4] not in ("=", "!"):
            out.append("(?P<")
            i += 3
            continue
        else:
            out.append(c)
        i += 1
    compiled = re.compile("".join(out))
    _regex_cache[pattern] = compiled
    return compiled


# --------------------------------------------------------------------- numbers

_INT = re.compile(r"[+-]?[0-9]+")
_UINT = re.compile(r"\+?[0-9]+")
_FLOAT = re.compile(
    r"[+-]?(?:inf|infinity|nan|(?:[0-9]+\.?[0-9]*|\.[0-9]+)(?:[eE][+-]?[0-9]+)?)",
    re.IGNORECASE,
)


def parse_int(s: str, lo: int = I128_MIN, hi: int = I128_MAX) -> int | None:
    """Rust `s.parse::<iN>()`: optional sign, ASCII digits, in range; else None."""
    if not _INT.fullmatch(s):
        return None
    v = int(s)
    return v if lo <= v <= hi else None


def parse_uint(s: str, hi: int = U64_MASK) -> int | None:
    """Rust `s.parse::<uN>()` (`usize` is u64 here)."""
    if not _UINT.fullmatch(s):
        return None
    v = int(s)
    return v if v <= hi else None


def parse_float(s: str) -> float | None:
    """Rust `s.parse::<f64>()`: no surrounding spaces, no underscores."""
    if not _FLOAT.fullmatch(s):
        return None
    return float(s)


def as_millis(seconds: float) -> int:
    """Rust `Duration::as_millis` of a duration held as float seconds: rounded
    to the nanosecond first, so `1.001` gives 1001 (not `int(1000.9999)`)."""
    return round(seconds * 1_000_000_000) // 1_000_000


def trim_start_matches(s: str, pat: str) -> str:
    """Rust `str::trim_start_matches` with a string pattern: every repetition."""
    if pat:
        while s.startswith(pat):
            s = s[len(pat) :]
    return s


def trim_end_matches(s: str, pat: str) -> str:
    """Rust `str::trim_end_matches` with a string pattern: every repetition."""
    if pat:
        while s.endswith(pat):
            s = s[: -len(pat)]
    return s


def div_trunc(a: int, b: int) -> int:
    """Rust integer `/`: rounds toward zero (Python's `//` floors)."""
    q = abs(a) // abs(b)
    return q if (a >= 0) == (b >= 0) else -q


def rem_trunc(a: int, b: int) -> int:
    """Rust integer `%`: the sign of the dividend (Python's `%` follows the divisor)."""
    return a - b * div_trunc(a, b)


# ------------------------------------------------------------------- files, io


class InvalidData(OSError):
    """A file that is not UTF-8, as Rust's `fs::read_to_string` reports it."""

    def __init__(self) -> None:
        super().__init__("stream did not contain valid UTF-8")

    def __str__(self) -> str:
        return "stream did not contain valid UTF-8"


def read_text(path: str | os.PathLike[str]) -> str:
    """Rust `fs::read_to_string`: strict UTF-8, line endings kept as they are."""
    data = Path(path).read_bytes()
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        raise InvalidData() from None


def write_text(path: str | os.PathLike[str], text: str) -> None:
    """Rust `fs::write` of a string: UTF-8, no `\\r` added on Windows."""
    Path(path).write_bytes(text.encode("utf-8"))


# C runtime errno -> the Windows error and message Rust reports for it.
_WINDOWS_ERRNO = {
    13: (5, "Access is denied."),
    17: (183, "Cannot create a file when that file already exists."),
    41: (145, "The directory is not empty."),
}


def _windows_error(e: OSError) -> tuple[int, str] | None:
    """The Windows error code and message of `e`, with the final period that
    Rust keeps and Python drops. Python's `open()` only sets a C errno
    (`No such file or directory`): it is mapped back to the Windows error."""
    w = getattr(e, "winerror", None)
    if w is not None:
        msg = (e.strerror or "").rstrip()
        return w, msg if msg.endswith(".") or not msg else msg + "."
    if e.errno == 2:
        name = e.filename
        if name is not None and not Path(os.fsdecode(name)).parent.is_dir():
            return 3, "The system cannot find the path specified."
        return 2, "The system cannot find the file specified."
    return _WINDOWS_ERRNO.get(e.errno or 0)


def io_error(e: OSError) -> str:
    """An io error as Rust displays it: `message (os error N)`."""
    if isinstance(e, InvalidData):
        return str(e)
    if type(e).__name__ == "ProgramNotFound" and sys.platform == "win32":
        return "program not found"
    if sys.platform == "win32":
        win = _windows_error(e)
        if win is not None:
            return f"{win[1]} (os error {win[0]})"
    code = getattr(e, "winerror", None) or e.errno
    msg = e.strerror or str(e)
    return f"{msg} (os error {code})" if code is not None else msg


def is_terminal(stream: object) -> bool:
    """Rust's `IsTerminal` for a standard stream. On Windows, Python's
    `isatty()` also says yes for `NUL` and no for an msys or Cygwin terminal
    (Git Bash, mintty); Rust asks the console, then looks for an msys pty."""
    try:
        fd = stream.fileno()  # type: ignore[attr-defined]
    except (AttributeError, OSError, ValueError):
        return False
    if sys.platform != "win32":
        return os.isatty(fd)
    import ctypes
    import msvcrt
    from ctypes import wintypes

    try:
        handle = msvcrt.get_osfhandle(fd)
    except OSError:
        return False
    if not handle or handle == -1:
        return False
    k32 = ctypes.WinDLL("kernel32")
    h = wintypes.HANDLE(handle)
    mode = wintypes.DWORD()
    if k32.GetConsoleMode(h, ctypes.byref(mode)):
        return True
    if k32.GetFileType(h) != 3:  # FILE_TYPE_PIPE
        return False

    class NameInfo(ctypes.Structure):
        _fields_ = [("FileNameLength", wintypes.DWORD), ("FileName", ctypes.c_uint16 * 260)]

    info = NameInfo()
    # 2: FileNameInfo
    if not k32.GetFileInformationByHandleEx(h, 2, ctypes.byref(info), ctypes.sizeof(info)):
        return False
    n = info.FileNameLength // 2
    if n > 260:
        return False
    name = bytes(info.FileName)[: 2 * n].decode("utf-16-le", "replace")
    name = name.rsplit("\\", 1)[-1]
    return name.startswith(("msys-", "cygwin-")) and "-pty" in name


def leave_own_venv() -> None:
    """Drops the tools' own virtual environment from `PATH` (`uv run`, an
    activated venv), so that the programs found, the student's `python`
    first, and the environment coursecrafter detects are the machine's."""
    if sys.prefix == sys.base_prefix:
        return
    scripts = Path(sys.prefix) / ("Scripts" if sys.platform == "win32" else "bin")

    def same(a: str) -> bool:
        return os.path.normcase(os.path.normpath(a)) == os.path.normcase(str(scripts))

    path = os.environ.get("PATH")
    if path is not None:
        os.environ["PATH"] = os.pathsep.join(p for p in path.split(os.pathsep) if not same(p))
    venv = os.environ.get("VIRTUAL_ENV")
    if venv is not None and same(str(Path(venv) / scripts.name)):
        del os.environ["VIRTUAL_ENV"]


def setup_stdio() -> None:
    """UTF-8 and `\\n` on standard output and error, as Rust writes them,
    whatever the console code page or a pipe would choose."""
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            reconfigure(encoding="utf-8", errors="replace", newline="\n")
