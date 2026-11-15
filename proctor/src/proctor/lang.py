"""Per-language presets: toolchain lookup, build commands, harness code
generation (the hidden `main` that calls the student's functions), and
mechanical stubs. Languages without a harness template still work with
`target: program`."""

from __future__ import annotations

import functools
import os
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path

from proctor import harness, report, run, scan, sig
from proctor.command import Command
from proctor.compat import (
    from_utf8_lossy,
    io_error,
    lines,
    read_text,
    split_whitespace,
    trim,
    trim_end,
    trim_start,
    write_text,
)
from proctor.errors import Error
from proctor.unit import LangConfig

BUILD_TIMEOUT = 600.0


def has_harness(lang: str) -> bool:
    return lang in ("c", "python", "rust")


def builds_in_place(lang: str) -> bool:
    """Rust builds happen in place (crates often inherit a cargo workspace);
    everything else is copied to a scratch directory first."""
    return lang == "rust"


def exe(name: str) -> str:
    if sys.platform == "win32":
        return f"{name}.exe"
    return name


def _split_paths(s: str) -> list[str]:
    """`std::env::split_paths` on Windows: `;`-separated, with "double quotes"
    around parts that contain a `;`."""
    out: list[str] = []
    cur: list[str] = []
    in_quote = False
    for c in s:
        if c == '"':
            in_quote = not in_quote
        elif c == ";" and not in_quote:
            out.append("".join(cur))
            cur = []
        else:
            cur.append(c)
    out.append("".join(cur))
    return out


@functools.cache
def _bash() -> str | None:
    path = os.environ.get("PATH", "")
    on_path = [
        os.path.join(d, "bash.exe")
        for d in _split_paths(path)
        if "system32" not in d.lower() and "windowsapps" not in d.lower()
    ]
    home = os.environ.get("USERPROFILE", "")
    known = [
        f"{home}\\scoop\\apps\\msys2\\current\\usr\\bin\\bash.exe",
        f"{home}\\scoop\\apps\\git\\current\\bin\\bash.exe",
        r"C:\msys64\usr\bin\bash.exe",
        r"C:\Program Files\Git\bin\bash.exe",
    ]
    return next((p for p in on_path + known if os.path.isfile(p)), None)


def resolve(program: str) -> str:
    """Resolves a program name the way a shell would. On Windows, `bash`/`sh`
    would otherwise hit `System32\\bash.exe` (WSL) before `PATH`, so look for
    an msys2 / Git bash on `PATH` first."""
    if sys.platform != "win32" or program not in ("bash", "sh"):
        return program
    found = _bash()
    return found if found is not None else program


def resolve_words(words: list[str]) -> list[str]:
    if words:
        words[0] = resolve(words[0])
    return words


@functools.cache
def cc() -> str | None:
    candidates: list[str] = []
    c = os.environ.get("CC")
    if c is not None:
        candidates.append(c)
    candidates.extend(["cc", "gcc", "clang"])
    return next((c for c in candidates if run.exists(c)), None)


@functools.cache
def python() -> str | None:
    for p in ["python3", "python"]:
        try:
            o = Command(p).arg("--version").output()
        except OSError:
            continue
        if (o.status.success() and "Python 3" in from_utf8_lossy(o.stdout)) or (
            "Python 3" in from_utf8_lossy(o.stderr)
        ):
            return p
    return None


class BuildError(Error):
    def __init__(self, msg: str, log: str = "") -> None:
        super().__init__(msg)
        self.log = log

    @staticmethod
    def new(msg: str) -> BuildError:
        return BuildError(msg=msg, log="")


@dataclass(kw_only=True)
class Ctx:
    lang: str
    # The tree to build (scratch copy, or the real workspace for Rust).
    ws: Path
    # Scratch space for outputs (objects, executables, generated harness).
    work: Path
    env: dict[str, str]
    # Persistent cache (cargo target dir).
    cache: Path
    # Merged language settings (preset + course).
    cfg: LangConfig
    # Course support files built in with the student's code.
    support: list[Path]
    # Prefix for every run of student code.
    wrap: str | None

    def compiler(self) -> str:
        c = self.cfg.compiler if self.cfg.compiler is not None else cc()
        if c is None:
            raise BuildError.new("no C compiler found (cc, gcc or clang)")
        return c

    def flags(self) -> list[str]:
        return list(self.cfg.flags) if self.cfg.flags is not None else list(C_FLAGS)

    def support_files(self, ext: str) -> list[str]:
        return [str(p) for p in self.support if p.suffix == "." + ext]

    def support_dirs(self) -> list[str]:
        out: list[str] = []
        for p in self.support:
            d = _parent(p)
            if d is not None and d not in out:
                out.append(d)
        return out

    def wrapped(self, cmd: list[str]) -> list[str]:
        """`cmd` behind the `wrap` prefix (valgrind and the like), if any."""
        if self.wrap is not None and trim(self.wrap) != "":
            out = resolve_words(run.split_words(self.wrap))
            out.extend(cmd)
            return out
        return cmd

    def command(self, words: list[str], cwd: Path) -> Command:
        c = Command(resolve(words[0]))
        c.args(words[1:]).current_dir(cwd).envs(self.env)
        if self.lang == "rust":
            c.env("CARGO_TARGET_DIR", self.cache / "target")
        return c

    def exec(self, words: list[str], cwd: Path) -> run.Out:
        c = self.command(words, cwd)
        try:
            return run.run(c, None, BUILD_TIMEOUT)
        except Error as e:
            raise BuildError.new(e.msg) from None

    def rel(self, s: str) -> str:
        out = s
        for base in (self.ws, self.work):
            for form in (str(base), str(base).replace("\\", "/")):
                for sep in ("\\", "/"):
                    out = out.replace(f"{form}{sep}", "")
        return out.replace("\\\\?\\", "")


def _parent(p: Path) -> str | None:
    """`Path::parent`: `None` for a root, `""` for a bare file name."""
    if p.parent == p:
        return None
    if not p.parent.parts:
        return ""
    return str(p.parent)


def count_warnings(text: str) -> int:
    """Counts compiler/linter warnings in build output."""
    return len(
        [
            ln
            for ln in lines(text)
            if "warning:" in ln and "generated" not in ln and "emitted" not in ln
        ]
    )


def first_error(log: str) -> str | None:
    """The first useful error line of a build log, with its location."""
    ls = lines(log)

    def noise(ln: str) -> bool:
        return (
            "could not compile" in ln
            or "aborting due to" in ln
            or "build failed" in ln
            or "ld returned" in ln
            or ln.startswith("error: process didn't exit")
            or "compilation terminated" in ln
        )

    # gcc/clang: `file:line:col: error: msg`
    found = next((ln for ln in ls if ": error:" in ln or ": fatal error:" in ln), None)
    if found is not None:
        return report.truncate(trim(found), 200)
    # rustc / cargo: `error[...]: msg` then ` --> file:line:col`
    idx = next(
        (k for k, ln in enumerate(ls) if trim_start(ln).startswith("error") and not noise(ln)),
        None,
    )
    if idx is not None:
        msg = trim(ls[idx])
        loc = next(
            (
                trim(ln)[len("--> ") :]
                for ln in ls[idx + 1 : idx + 4]
                if trim(ln).startswith("--> ")
            ),
            None,
        )
        if loc is not None:
            loc = loc.replace("\\", "/")
            return f"{report.truncate(msg, 160)} ({loc})"
        return report.truncate(msg, 200)
    # Python tracebacks and linker messages.
    found = next(
        (ln for ln in reversed(ls) if "Error" in ln or "undefined reference" in ln),
        None,
    )
    if found is not None:
        return report.truncate(trim(found), 200)
    return next((report.truncate(t, 200) for t in map(trim, ls) if t != ""), None)


def c_sources(ws: Path) -> list[str]:
    return [f for f in scan.walk(ws) if f.endswith(".c")]


def c_include_flags(ws: Path) -> list[str]:
    dirs: set[str] = set()
    dirs.add(".")
    for f in scan.walk(ws):
        if f.endswith(".h") and "/" in f:
            d, _ = f.rsplit("/", 1)
            dirs.add(d)
    return [f"-I{d}" for d in sorted(dirs)]


C_FLAGS = ["-std=c11", "-Wall", "-Wextra", "-Werror"]


def substitute(words: list[str], map: list[tuple[str, str]]) -> list[str]:
    out: list[str] = []
    for w in words:
        if w == "{sources}":
            v = next((v for k, v in map if k == "{sources}"), None)
            if v is not None:
                out.extend(v.split("\x1f"))
            continue
        for k, v in map:
            w = w.replace(k, v)
        out.append(w)
    return out


def python_words(cmd: str) -> list[str]:
    words = run.split_words(cmd)
    if words and words[0] in ("python", "python3", "{python}"):
        py = python()
        if py is None:
            raise BuildError.new("Python 3 is not installed (python3 / python)")
        words[0] = py
    return words


def cargo_package(ws: Path) -> str:
    try:
        text = read_text(ws / "Cargo.toml")
    except OSError:
        raise BuildError.new("Cargo.toml is missing") from None
    in_pkg = False
    for line in lines(text):
        t = trim(line)
        if t.startswith("["):
            in_pkg = t == "[package]"
        elif in_pkg and t.startswith("name"):
            r = trim_start(t[len("name") :])
            if r.startswith("="):
                return trim(r[1:]).strip('"')
    raise BuildError.new("Cargo.toml has no [package] name")


def cargo_binary(ws: Path) -> str:
    """The executable cargo builds: the only `[[bin]]` name if there is one,
    else the package name."""
    try:
        text = read_text(ws / "Cargo.toml")
    except OSError:
        raise BuildError.new("Cargo.toml is missing") from None
    in_bin = False
    bins: list[str] = []
    for line in lines(text):
        t = trim(line)
        if t.startswith("["):
            in_bin = t == "[[bin]]"
        elif in_bin and t.startswith("name"):
            r = trim_start(t[len("name") :])
            if r.startswith("="):
                bins.append(trim(r[1:]).strip('"'))
    if len(bins) == 1:
        return bins[0]
    return cargo_package(ws)


def _or(x: str | None, default: str) -> str:
    """`Option::unwrap_or`."""
    return x if x is not None else default


def build_program(ctx: Ctx, run_cmd: str | None, build_cmd: str | None) -> tuple[list[str], str]:
    """Builds the student's program; returns the command that runs it."""
    try:
        ctx.work.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        raise BuildError.new(io_error(e)) from None
    log = ""
    vars: list[tuple[str, str]] = []
    match ctx.lang:
        case "c":
            cc_ = ctx.compiler()
            out = ctx.work / exe("prog")
            srcs = c_sources(ctx.ws)
            srcs.extend(ctx.support_files("c"))
            if not srcs and build_cmd is None:
                raise BuildError.new("no .c file to compile")
            vars.append(("{cc}", cc_))
            vars.append(("{out}", str(out)))
            vars.append(("{exe}", str(out)))
            vars.append(("{sources}", "\x1f".join(srcs)))
            if build_cmd is not None:
                words = substitute(run.split_words(build_cmd), vars)
            else:
                words = [cc_]
                words.extend(ctx.flags())
                words.extend(c_include_flags(ctx.ws))
                words.extend(f"-I{d}" for d in ctx.support_dirs())
                words.extend(srcs)
                words.extend(["-o", str(out)])
            o = ctx.exec(words, ctx.ws)
            log += o.stderr
            if not o.success:
                raise BuildError(
                    msg=_or(first_error(ctx.rel(o.stderr)), "compilation failed"),
                    log=o.stderr,
                )
        case "python":
            py = python()
            if py is None:
                raise BuildError.new("Python 3 is not installed (python3 / python)")
            o = ctx.exec([py, "-m", "compileall", "-q", "."], ctx.ws)
            if not o.success:
                all_ = f"{o.stdout}{o.stderr}"
                raise BuildError(msg=_or(first_error(ctx.rel(all_)), "syntax error"), log=all_)
            vars.append(("{python}", py))
        case "rust":
            o = ctx.exec(["cargo", "build", "--quiet"], ctx.ws)
            log += o.stderr
            if not o.success:
                raise BuildError(
                    msg=_or(first_error(ctx.rel(o.stderr)), "cargo build failed"),
                    log=o.stderr,
                )
            pkg = cargo_binary(ctx.ws)
            bin_ = ctx.cache / "target" / "debug" / exe(pkg)
            vars.append(("{cargo_bin}", str(bin_)))
            if run_cmd is None:
                return ctx.wrapped([str(bin_)]), log
        case "bash" | "sh" | "shell":
            for f in [f for f in scan.walk(ctx.ws) if f.endswith(".sh")]:
                o = ctx.exec(["bash", "-n", f], ctx.ws)
                if not o.success:
                    raise BuildError(
                        msg=_or(first_error(ctx.rel(o.stderr)), f"{f}: syntax error"),
                        log=o.stderr,
                    )
        case _:
            if build_cmd is not None:
                o = ctx.exec(run.split_words(build_cmd), ctx.ws)
                if not o.success:
                    all_ = f"{o.stdout}{o.stderr}"
                    raise BuildError(msg=_or(first_error(ctx.rel(all_)), "build failed"), log=all_)
                log += o.stderr
    if run_cmd is not None:
        cmd = resolve_words(substitute(python_words(run_cmd), vars))
    elif ctx.lang == "c":
        cmd = [str(ctx.work / exe("prog"))]
    else:
        raise BuildError.new("this unit needs a `run:` command for its program")
    return ctx.wrapped(cmd), log


def build_harness(
    ctx: Ctx, protos: list[sig.Proto], module: str | None, timeout: float
) -> tuple[harness.Harness, str]:
    """Builds a harness exposing `protos`; returns it plus the build log."""
    try:
        ctx.work.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        raise BuildError.new(io_error(e)) from None
    callable_: list[sig.Proto] = []
    for p in protos:
        try:
            harness_supports(ctx.lang, p)
        except Error:
            continue
        callable_.append(p)
    names = {p.name for p in callable_}
    match ctx.lang:
        case "c":
            return build_c_harness(ctx, callable_, names, timeout)
        case "python":
            py = python()
            if py is None:
                raise BuildError.new("Python 3 is not installed (python3 / python)")
            o = ctx.exec([py, "-m", "compileall", "-q", "."], ctx.ws)
            if not o.success:
                all_ = f"{o.stdout}{o.stderr}"
                raise BuildError(msg=_or(first_error(ctx.rel(all_)), "syntax error"), log=all_)
            script = ctx.work / "proctor_harness.py"
            try:
                write_text(script, PY_HARNESS)
            except OSError as e:
                raise BuildError.new(io_error(e)) from None
            cmd = ctx.wrapped(
                [py, "-u", str(script), str(ctx.ws), module if module is not None else ""]
            )
            env = dict(ctx.env)
            dirs = ctx.support_dirs()
            if dirs:
                sep = ";" if sys.platform == "win32" else ":"
                env["PYTHONPATH"] = sep.join(dirs)
            env["PYTHONIOENCODING"] = "utf-8"
            env["PYTHONDONTWRITEBYTECODE"] = "1"
            return harness.Harness.new(cmd, ctx.ws, env, timeout, names), ""
        case "rust":
            return build_rust_harness(ctx, callable_, names, timeout)
        case other:
            raise BuildError.new(
                f"`target: function` has no harness for {other}; use `target: program`"
            )


def harness_supports(lang: str, p: sig.Proto) -> None:
    """Whether the harness can pass this function's parameter and return types
    (raises `Error` when it cannot)."""
    if p.is_method:
        raise Error(f"`{p.name}` is a method; only free functions can be called")

    def ok(t: sig.Ty, lang: str) -> bool:
        match t:
            case sig.TyOther(name=o):
                return lang == "python" and (o == "untyped" or o != "")
            case sig.TyList(inner=i) | sig.TyOpt(inner=i):
                return lang != "c" and ok(i, lang)
            case _:
                return True

    for prm in p.params:
        if lang == "python":
            continue
        if not ok(prm.ty, lang):
            raise Error(
                f"parameter `{prm.name}` of `{p.name}` has type `{prm.raw}`, which the {lang} harness cannot build"
            )
    if lang == "c" and isinstance(p.ret, sig.TyOther):
        raise Error(f"`{p.name}` returns `{p.ret_raw}`, which the C harness cannot read")


# ------------------------------------------------------------------- C harness

# `-std=c11` hides POSIX's `fdopen` unless asked for: GCC 14 rejects the
# implicit declaration, and older ones truncate the returned pointer.
C_HARNESS_HEAD = r"""#ifndef _WIN32
#define _POSIX_C_SOURCE 200809L
#endif
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <stddef.h>
#include <stdint.h>
#include <stdbool.h>
#ifdef _WIN32
#include <io.h>
#define SC_DUP _dup
#define SC_DUP2 _dup2
#define SC_FDOPEN _fdopen
#else
#include <unistd.h>
#define SC_DUP dup
#define SC_DUP2 dup2
#define SC_FDOPEN fdopen
#endif

static FILE *sc_out;
static char *sc_cur;

static char *sc_tok(void) {
    while (*sc_cur == ' ') sc_cur++;
    if (!*sc_cur) return "n";
    char *t = sc_cur;
    while (*sc_cur && *sc_cur != ' ') sc_cur++;
    if (*sc_cur) *sc_cur++ = 0;
    return t;
}
static int sc_hexval(char c) { return c <= '9' ? c - '0' : (c | 32) - 'a' + 10; }
static char *sc_str(const char *t) {
    if (t[0] != 's') return NULL;
    size_t n = strlen(t + 1) / 2;
    char *s = malloc(n + 1);
    for (size_t i = 0; i < n; i++) s[i] = (char)(sc_hexval(t[1 + 2 * i]) * 16 + sc_hexval(t[2 + 2 * i]));
    s[n] = 0;
    return s;
}
static long long sc_int(const char *t) {
    if (t[0] == 'i' || t[0] == 'c') return strtoll(t + 1, NULL, 10);
    if (t[0] == 'b') return t[1] == '1';
    if (t[0] == 'f') return (long long)strtod(t + 1, NULL);
    return 0;
}
static unsigned long long sc_uint(const char *t) {
    if (t[0] == 'i' && t[1] != '-') return strtoull(t + 1, NULL, 10);
    return (unsigned long long)sc_int(t);
}
static double sc_float(const char *t) { return t[0] == 'f' ? strtod(t + 1, NULL) : (double)sc_int(t); }
static char sc_char(const char *t) {
    if (t[0] == 's') { char *s = sc_str(t); char c = s[0]; free(s); return c; }
    return (char)sc_int(t);
}
static char **sc_strlist(const char *t) {
    if (t[0] != 'l') return NULL;
    long n = strtol(t + 1, NULL, 10);
    char **v = malloc((size_t)(n + 1) * sizeof *v);
    for (long i = 0; i < n; i++) v[i] = sc_str(sc_tok());
    v[n] = NULL;
    return v;
}
static void sc_put_str(const char *s) {
    if (!s) { fputs("n", sc_out); return; }
    fputc('s', sc_out);
    for (const unsigned char *p = (const unsigned char *)s; *p; p++) fprintf(sc_out, "%02x", *p);
}
static void sc_put_int(long long v) { fprintf(sc_out, "i%lld", v); }
static void sc_put_uint(unsigned long long v) { fprintf(sc_out, "i%llu", v); }
static void sc_put_float(double v) { fprintf(sc_out, "f%.17g", v); }
static void sc_put_char(char c) { fprintf(sc_out, "c%d", (int)(unsigned char)c); }
static void sc_put_bool(int b) { fputs(b ? "b1" : "b0", sc_out); }
static void sc_put_strlist(char **v) {
    if (!v) { fputs("n", sc_out); return; }
    unsigned long long n = 0;
    while (v[n]) n++;
    fprintf(sc_out, "l%llu", n);
    for (unsigned long long i = 0; i < n; i++) { fputc(' ', sc_out); sc_put_str(v[i]); }
}
static char *sc_line(void) {
    static char *buf = NULL;
    static size_t cap = 0;
    size_t len = 0;
    int c;
    while ((c = getchar()) != EOF && c != '\n') {
        if (len + 2 > cap) { cap = cap ? cap * 2 : 4096; buf = realloc(buf, cap); }
        if (c != '\r') buf[len++] = (char)c;
    }
    if (c == EOF && len == 0) return NULL;
    if (!buf) { cap = 16; buf = malloc(cap); }
    buf[len] = 0;
    return buf;
}

"""


def c_decode(t: sig.Ty, raw: str, tok: str) -> str:
    match t:
        case sig.TyInt(unsigned=True):
            return f"({raw})sc_uint({tok})"
        case sig.TyInt(unsigned=False):
            return f"({raw})sc_int({tok})"
        case sig.TyFloat():
            return f"({raw})sc_float({tok})"
        case sig.TyBool():
            return f"({raw})(sc_int({tok}) != 0)"
        case sig.TyChar():
            return f"sc_char({tok})"
        case sig.TyStr():
            return f"({raw})sc_str({tok})"
        case sig.TyStrList():
            return f"({raw})sc_strlist({tok})"
        case _:
            return f"({raw})0"


def c_encode(t: sig.Ty, expr: str) -> str:
    match t:
        case sig.TyInt(unsigned=True):
            return f"sc_put_uint((unsigned long long)({expr}));"
        case sig.TyInt(unsigned=False):
            return f"sc_put_int((long long)({expr}));"
        case sig.TyFloat():
            return f"sc_put_float((double)({expr}));"
        case sig.TyBool():
            return f"sc_put_bool(({expr}) != 0);"
        case sig.TyChar():
            return f"sc_put_char({expr});"
        case sig.TyStr():
            return f"sc_put_str({expr});"
        case sig.TyStrList():
            return f"sc_put_strlist((char **)({expr}));"
        case _:
            return 'fputs("n", sc_out);'


def mutable_c_buffer(p: sig.Proto) -> int | None:
    """Index of the first writable string parameter (char *, not const)."""
    return next(
        (
            i
            for i, x in enumerate(p.params)
            if x.ty == sig.TyStr() and "const" not in split_whitespace(x.raw)
        ),
        None,
    )


def c_harness_source(protos: list[sig.Proto], sigs: dict[str, str]) -> str:
    s = C_HARNESS_HEAD
    for p in protos:
        text = sigs.get(p.name)
        if text is not None:
            s += f"{trim(text).rstrip(';')};\n"
    s += '\nint main(void) {\n    fflush(stdout);\n    sc_out = SC_FDOPEN(SC_DUP(1), "w");\n    SC_DUP2(2, 1);\n    char *line;\n    while ((line = sc_line()) != NULL) {\n        sc_cur = line;\n        if (strcmp(sc_tok(), "call") != 0) continue;\n        const char *fn = sc_tok();\n        sc_tok();\n        fputs("ok ", sc_out);\n        if (0) {\n        }'
    for p in protos:
        s += f' else if (strcmp(fn, "{p.name}") == 0) {{\n'
        args: list[str] = []
        for i, prm in enumerate(p.params):
            decl_ty = "char" if isinstance(prm.ty, sig.TyChar) else prm.raw
            s += f"            {decl_ty} a{i} = {c_decode(prm.ty, prm.raw, 'sc_tok()')};\n"
            args.append(f"a{i}")
        call = f"{p.name}({', '.join(args)})"
        if p.ret == sig.TyVoid():
            # In-place functions: the value is the mutated buffer.
            buf = mutable_c_buffer(p)
            out = f"sc_put_str(a{buf});" if buf is not None else 'fputs("n", sc_out);'
            s += f"            {call};\n            {out}\n"
        else:
            s += f"            {p.ret_raw} r = {call};\n            {c_encode(p.ret, 'r')}\n"
        s += "        }"
    s += " else {\n            fputs(\"n\", sc_out);\n        }\n        fputc('\\n', sc_out);\n        fflush(sc_out);\n    }\n    return 0;\n}\n"
    return s


def build_c_harness(
    ctx: Ctx, protos: list[sig.Proto], names: set[str], timeout: float
) -> tuple[harness.Harness, str]:
    cc_ = ctx.compiler()
    flags = ctx.flags()
    # Flags that must also reach the harness and the link (sanitizers, -pthread...).
    link_flags = [
        f
        for f in flags
        if f.startswith("-fsanitize")
        or f.startswith("-fno-omit")
        or f == "-pthread"
        or f.startswith("-l")
        or f.startswith("-L")
    ]
    obj = ctx.work / "obj"
    shutil.rmtree(obj, ignore_errors=True)
    try:
        obj.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        raise BuildError.new(io_error(e)) from None
    log = ""
    objects: list[str] = []
    includes = c_include_flags(ctx.ws)
    includes.extend(f"-I{d}" for d in ctx.support_dirs())
    for i, src in enumerate(c_sources(ctx.ws)):
        o = obj / f"s{i}.o"
        w = [cc_]
        w.extend(flags)
        w.extend(includes)
        w.extend(["-Dmain=proctor_student_main", "-c", src, "-o", str(o)])
        out = ctx.exec(w, ctx.ws)
        log += out.stderr
        if not out.success:
            raise BuildError(
                msg=_or(first_error(ctx.rel(out.stderr)), "compilation failed"),
                log=out.stderr,
            )
        objects.append(str(o))
    # Course support code: trusted, so no -Werror, but the same sanitizers.
    for i, src in enumerate(ctx.support_files("c")):
        o = obj / f"support{i}.o"
        w = [cc_, "-std=c11", "-w"]
        w.extend(link_flags)
        w.extend(includes)
        w.extend(["-c", src, "-o", str(o)])
        out = ctx.exec(w, ctx.ws)
        if not out.success:
            raise BuildError(
                msg=f"course error: support file does not compile: {_or(first_error(out.stderr), '')}",
                log=out.stderr,
            )
        objects.append(str(o))
    sigs = {p.name: p.sig_text for p in protos}
    src_path = ctx.work / "proctor_harness.c"
    try:
        write_text(src_path, c_harness_source(protos, sigs))
    except OSError as e:
        raise BuildError.new(io_error(e)) from None
    hobj = ctx.work / "proctor_harness.o"
    out = ctx.exec(
        [cc_, "-std=c11", "-w", "-c", str(src_path), "-o", str(hobj)],
        ctx.work,
    )
    if not out.success:
        raise BuildError(
            msg=f"internal: harness does not compile: {_or(first_error(out.stderr), '')}",
            log=out.stderr,
        )
    bin_ = ctx.work / exe("proctor_harness")
    w = [cc_]
    w.extend(link_flags)
    w.extend(objects)
    w.extend([str(hobj), "-o", str(bin_)])
    out = ctx.exec(w, ctx.ws)
    if not out.success:
        undefined: str | None = None
        for ln in lines(out.stderr):
            parts = ln.split("undefined reference to `")
            if len(parts) > 1:
                n = parts[1].split("'")[0]
                undefined = f"function `{n}` is not defined (or is static)"
                break
        msg = undefined if undefined is not None else first_error(ctx.rel(out.stderr))
        raise BuildError(msg=_or(msg, "link failed"), log=out.stderr)
    return (
        harness.Harness.new(
            ctx.wrapped([str(bin_)]),
            ctx.ws,
            dict(ctx.env),
            timeout,
            names,
        ),
        log,
    )


# -------------------------------------------------------------- Python harness

PY_HARNESS = r"""import sys, os, importlib, dataclasses, enum
sys.path.insert(0, sys.argv[1])
os.chdir(sys.argv[1])
DEFAULT = sys.argv[2] if len(sys.argv) > 2 else ""
_proto = os.fdopen(os.dup(1), "w", encoding="utf-8", newline="\n")
os.dup2(2, 1)
sys.stdout = sys.stderr
sys.setrecursionlimit(20000)

def hx(s):
    return s.encode("utf-8", "replace").hex()

def dec(toks):
    t = next(toks)
    tag, rest = t[0], t[1:]
    if tag == "n": return None
    if tag == "b": return rest == "1"
    if tag == "i": return int(rest)
    if tag == "f": return float(rest)
    if tag == "s": return bytes.fromhex(rest).decode("utf-8", "replace")
    if tag == "c": return chr(int(rest))
    if tag == "o": return bytes.fromhex(rest).decode("utf-8", "replace")
    if tag == "l": return [dec(toks) for _ in range(int(rest))]
    if tag == "m":
        d = {}
        for _ in range(int(rest)):
            k = dec(toks)
            if isinstance(k, list): k = tuple(k)
            d[k] = dec(toks)
        return d
    raise ValueError("bad value tag " + t)

def enc(v, out, depth=0):
    if depth > 200:
        out.append("o" + hx("<too deep>")); return
    if v is None: out.append("n")
    elif v is True: out.append("b1")
    elif v is False: out.append("b0")
    elif isinstance(v, enum.Enum): out.append("s" + hx(v.name))
    elif isinstance(v, int): out.append("i%d" % v)
    elif isinstance(v, float): out.append("f" + repr(v))
    elif isinstance(v, str): out.append("s" + hx(v))
    elif hasattr(v, "_asdict"): enc(dict(v._asdict()), out, depth + 1)
    elif dataclasses.is_dataclass(v) and not isinstance(v, type):
        enc({f.name: getattr(v, f.name) for f in dataclasses.fields(v)}, out, depth + 1)
    elif isinstance(v, (list, tuple)):
        out.append("l%d" % len(v))
        for x in v: enc(x, out, depth + 1)
    elif isinstance(v, (set, frozenset)):
        items = sorted(v, key=repr)
        out.append("l%d" % len(items))
        for x in items: enc(x, out, depth + 1)
    elif isinstance(v, dict):
        out.append("m%d" % len(v))
        for k, x in v.items():
            enc(k, out, depth + 1); enc(x, out, depth + 1)
    elif isinstance(v, (bytes, bytearray)): out.append("o" + hx(repr(v)))
    elif hasattr(v, "__next__"):
        items = []
        for x in v:
            items.append(x)
            if len(items) > 1000000: break
        enc(items, out, depth + 1)
    else: out.append("o" + hx(str(v)))

mods = {}
def resolve(name):
    mod, attr = name.rsplit(".", 1) if "." in name else (DEFAULT, name)
    if not mod:
        raise ImportError("no module given for " + name)
    if mod not in mods:
        mods[mod] = importlib.import_module(mod)
    return getattr(mods[mod], attr)

while True:
    line = sys.stdin.readline()
    if not line: break
    parts = line.split()
    if len(parts) < 3 or parts[0] != "call": continue
    try:
        toks = iter(parts[3:])
        args = [dec(toks) for _ in range(int(parts[2]))]
        r = resolve(parts[1])(*args)
        out = ["ok"]
        enc(r, out)
        resp = " ".join(out)
    except RecursionError:
        resp = "exc " + hx("RecursionError") + " " + hx("maximum recursion depth exceeded")
    except BaseException as e:
        resp = "exc " + hx(type(e).__name__) + " " + hx(str(e))
    _proto.write(resp + "\n")
    _proto.flush()
"""

# ---------------------------------------------------------------- Rust harness

RUST_HARNESS_HEAD = r"""#![allow(clippy::all, unused)]
use std::io::{BufRead, Write};

enum W { N, B(bool), I(i128), F(f64), S(String), C(char), L(Vec<W>), M(Vec<(W, W)>), O(String) }
struct Exc(String, String);

fn hx(s: &str) -> String { s.bytes().map(|b| format!("{b:02x}")).collect() }
fn unhx(s: &str) -> String {
    let b: Vec<u8> = (0..s.len() / 2).filter_map(|i| u8::from_str_radix(&s[2 * i..2 * i + 2], 16).ok()).collect();
    String::from_utf8_lossy(&b).into_owned()
}
fn parse<'a>(it: &mut impl Iterator<Item = &'a str>) -> Result<W, Exc> {
    let t = it.next().ok_or(Exc("Protocol".into(), "truncated".into()))?;
    let (tag, rest) = t.split_at(1);
    Ok(match tag {
        "n" => W::N,
        "b" => W::B(rest == "1"),
        "i" => W::I(rest.parse().unwrap_or(0)),
        "f" => W::F(rest.parse().unwrap_or(f64::NAN)),
        "s" => W::S(unhx(rest)),
        "o" => W::O(unhx(rest)),
        "c" => W::C(rest.parse().ok().and_then(char::from_u32).unwrap_or('?')),
        "l" => { let n: usize = rest.parse().unwrap_or(0); let mut v = Vec::new(); for _ in 0..n { v.push(parse(it)?); } W::L(v) }
        "m" => { let n: usize = rest.parse().unwrap_or(0); let mut v = Vec::new(); for _ in 0..n { let k = parse(it)?; v.push((k, parse(it)?)); } W::M(v) }
        _ => return Err(Exc("Protocol".into(), format!("bad tag {t}"))),
    })
}
fn emit(w: &W, o: &mut String) {
    match w {
        W::N => o.push('n'),
        W::B(b) => o.push_str(if *b { "b1" } else { "b0" }),
        W::I(i) => o.push_str(&format!("i{i}")),
        W::F(f) => o.push_str(&format!("f{f:e}")),
        W::S(s) => { o.push('s'); o.push_str(&hx(s)); }
        W::O(s) => { o.push('o'); o.push_str(&hx(s)); }
        W::C(c) => o.push_str(&format!("c{}", *c as u32)),
        W::L(l) => { o.push_str(&format!("l{}", l.len())); for x in l { o.push(' '); emit(x, o); } }
        W::M(m) => { o.push_str(&format!("m{}", m.len())); for (k, v) in m { o.push(' '); emit(k, o); o.push(' '); emit(v, o); } }
    }
}
fn bad(what: &str) -> Exc { Exc("TypeError".into(), format!("cannot convert argument to {what}")) }

trait FromW: Sized { fn from_w(w: W) -> Result<Self, Exc>; }
macro_rules! ints { ($($t:ty),*) => { $(
    impl FromW for $t { fn from_w(w: W) -> Result<Self, Exc> { match w { W::I(i) => <$t>::try_from(i).map_err(|_| bad(stringify!($t))), W::C(c) => <$t>::try_from(c as u32 as i128).map_err(|_| bad(stringify!($t))), _ => Err(bad(stringify!($t))) } } }
    impl ToW for $t { fn to_w(&self) -> W { W::I(*self as i128) } }
)* } }
ints!(i8, i16, i32, i64, i128, isize, u8, u16, u32, u64, u128, usize);
impl FromW for f64 { fn from_w(w: W) -> Result<Self, Exc> { match w { W::F(f) => Ok(f), W::I(i) => Ok(i as f64), _ => Err(bad("f64")) } } }
impl FromW for f32 { fn from_w(w: W) -> Result<Self, Exc> { f64::from_w(w).map(|f| f as f32) } }
impl FromW for bool { fn from_w(w: W) -> Result<Self, Exc> { match w { W::B(b) => Ok(b), _ => Err(bad("bool")) } } }
impl FromW for char { fn from_w(w: W) -> Result<Self, Exc> { match w { W::C(c) => Ok(c), W::S(s) if s.chars().count() == 1 => Ok(s.chars().next().unwrap_or('?')), _ => Err(bad("char")) } } }
impl FromW for String { fn from_w(w: W) -> Result<Self, Exc> { match w { W::S(s) | W::O(s) => Ok(s), W::C(c) => Ok(c.to_string()), _ => Err(bad("String")) } } }
impl<T: FromW> FromW for Vec<T> { fn from_w(w: W) -> Result<Self, Exc> { match w { W::L(l) => l.into_iter().map(T::from_w).collect(), W::S(s) => s.chars().map(|c| T::from_w(W::C(c))).collect(), _ => Err(bad("Vec")) } } }
impl<T: FromW> FromW for Option<T> { fn from_w(w: W) -> Result<Self, Exc> { match w { W::N => Ok(None), w => T::from_w(w).map(Some) } } }

trait ToW { fn to_w(&self) -> W; }
impl ToW for f64 { fn to_w(&self) -> W { W::F(*self) } }
impl ToW for f32 { fn to_w(&self) -> W { W::F(*self as f64) } }
impl ToW for bool { fn to_w(&self) -> W { W::B(*self) } }
impl ToW for char { fn to_w(&self) -> W { W::C(*self) } }
impl ToW for str { fn to_w(&self) -> W { W::S(self.to_string()) } }
impl ToW for String { fn to_w(&self) -> W { W::S(self.clone()) } }
impl ToW for () { fn to_w(&self) -> W { W::N } }
impl<T: ToW> ToW for [T] { fn to_w(&self) -> W { W::L(self.iter().map(ToW::to_w).collect()) } }
impl<T: ToW> ToW for Vec<T> { fn to_w(&self) -> W { W::L(self.iter().map(ToW::to_w).collect()) } }
impl<T: ToW, const N: usize> ToW for [T; N] { fn to_w(&self) -> W { W::L(self.iter().map(ToW::to_w).collect()) } }
impl<T: ToW> ToW for Option<T> { fn to_w(&self) -> W { match self { Some(v) => v.to_w(), None => W::N } } }
impl<T: ToW + ?Sized> ToW for &T { fn to_w(&self) -> W { (**self).to_w() } }
impl<T: ToW + ?Sized> ToW for &mut T { fn to_w(&self) -> W { (**self).to_w() } }
impl<T: ToW + ?Sized> ToW for Box<T> { fn to_w(&self) -> W { (**self).to_w() } }
impl<A: ToW, B: ToW> ToW for (A, B) { fn to_w(&self) -> W { W::L(vec![self.0.to_w(), self.1.to_w()]) } }
impl<A: ToW, B: ToW, C: ToW> ToW for (A, B, C) { fn to_w(&self) -> W { W::L(vec![self.0.to_w(), self.1.to_w(), self.2.to_w()]) } }
impl<K: ToW, V: ToW> ToW for std::collections::HashMap<K, V> { fn to_w(&self) -> W { W::M(self.iter().map(|(k, v)| (k.to_w(), v.to_w())).collect()) } }
impl<K: ToW, V: ToW> ToW for std::collections::BTreeMap<K, V> { fn to_w(&self) -> W { W::M(self.iter().map(|(k, v)| (k.to_w(), v.to_w())).collect()) } }
impl<T: ToW> ToW for std::collections::BTreeSet<T> { fn to_w(&self) -> W { W::L(self.iter().map(ToW::to_w).collect()) } }

trait Out { fn out(self) -> Result<W, Exc>; }
impl<T: ToW, E: std::fmt::Debug> Out for Result<T, E> { fn out(self) -> Result<W, Exc> { match self { Ok(v) => Ok(v.to_w()), Err(e) => Err(Exc("Err".into(), format!("{e:?}"))) } } }
struct Plain<T>(T);
impl<T: ToW> Out for Plain<T> { fn out(self) -> Result<W, Exc> { Ok(self.0.to_w()) } }

fn arg<'a, T: FromW>(it: &mut impl Iterator<Item = &'a str>) -> Result<T, Exc> { T::from_w(parse(it)?) }

fn panic_text(p: Box<dyn std::any::Any + Send>) -> String {
    if let Some(s) = p.downcast_ref::<&str>() { s.to_string() } else if let Some(s) = p.downcast_ref::<String>() { s.clone() } else { "panic".into() }
}

fn main() {
    std::panic::set_hook(Box::new(|_| {}));
    let stdin = std::io::stdin();
    for line in stdin.lock().lines() {
        let Ok(line) = line else { break };
        let mut it = line.split_ascii_whitespace();
        if it.next() != Some("call") { continue; }
        let name = it.next().unwrap_or("").to_string();
        let _ = it.next();
        let res = std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| dispatch(&name, &mut it)));
        let resp = match res {
            Ok(Ok(w)) => { let mut s = String::from("ok "); emit(&w, &mut s); s }
            Ok(Err(Exc(t, m))) => format!("exc {} {}", hx(&t), hx(&m)),
            Err(p) => format!("exc {} {}", hx("panic"), hx(&panic_text(p))),
        };
        let mut out = std::io::stdout().lock();
        let _ = write!(out, "\n\u{1e}SC {resp}\n");
        let _ = out.flush();
    }
}
"""


def rust_arg(raw: str) -> tuple[str, str]:
    """Owned Rust type to decode into, and how to pass it."""
    t = trim(sig.strip_lifetimes(raw))
    if t.startswith("&mut"):
        owned, pass_ = trim(t[len("&mut") :]), "&mut "
    elif t.startswith("&"):
        owned, pass_ = trim(t[1:]), "&"
    else:
        owned, pass_ = t, ""
    if owned == "str":
        owned = "String"
    elif owned.startswith("[") and owned[1:].endswith("]"):
        owned = f"Vec<{owned[1:-1]}>"
    return owned, pass_


def rust_harness_source(protos: list[sig.Proto], krate: str) -> str:
    s = RUST_HARNESS_HEAD
    s += "\nfn dispatch<'a>(name: &str, it: &mut impl Iterator<Item = &'a str>) -> Result<W, Exc> {\n    match name {\n"
    for p in protos:
        s += f'        "{p.name}" => {{\n'
        args: list[str] = []
        for i, prm in enumerate(p.params):
            owned, pass_ = rust_arg(prm.raw)
            s += f"            let mut a{i}: {owned} = arg(it)?;\n"
            args.append(f"{pass_}a{i}")
        call = f"{krate}::{p.name}({', '.join(args)})"
        unit_ret = trim(p.ret_raw) in ("", "()")
        mutated = next(
            (i for i, x in enumerate(p.params) if trim_start(x.raw).startswith("&mut")), None
        )
        if unit_ret and mutated is not None:
            # In-place functions: the value is the mutated argument.
            s += f"            {call};\n            Plain(&a{mutated}).out()\n"
        elif trim_start(p.ret_raw).startswith("Result"):
            s += f"            {call}.out()\n"
        else:
            s += f"            Plain({call}).out()\n"
        s += "        }\n"
    s += '        _ => Err(Exc("Protocol".into(), format!("unknown function {name}"))),\n    }\n}\n'
    return s


def build_rust_harness(
    ctx: Ctx, protos: list[sig.Proto], names: set[str], timeout: float
) -> tuple[harness.Harness, str]:
    pkg = cargo_package(ctx.ws)
    dir_ = ctx.cache / "harness"
    try:
        (dir_ / "src").mkdir(parents=True, exist_ok=True)
    except OSError as e:
        raise BuildError.new(io_error(e)) from None
    ws = str(ctx.ws).replace("\\", "/")
    try:
        write_text(
            dir_ / "Cargo.toml",
            f'[package]\nname = "proctor_harness"\nversion = "0.0.0"\nedition = "2024"\npublish = false\n\n[dependencies]\nstudent = {{ package = "{pkg}", path = "{ws}" }}\n\n[workspace]\n',
        )
    except OSError as e:
        raise BuildError.new(io_error(e)) from None
    try:
        write_text(dir_ / "src" / "main.rs", rust_harness_source(protos, "student"))
    except OSError as e:
        raise BuildError.new(io_error(e)) from None
    c = Command("cargo")
    c.args(["build", "--quiet"]).current_dir(dir_).envs(ctx.env).env(
        "CARGO_TARGET_DIR", ctx.cache / "target"
    )
    try:
        out = run.run(c, None, BUILD_TIMEOUT)
    except Error as e:
        raise BuildError.new(e.msg) from None
    if not out.success:
        rel = ctx.rel(out.stderr)
        if "no library targets found" in rel or "lib target" in rel:
            msg = "the crate has no library target (src/lib.rs)"
        else:
            missing: str | None = None
            for ln in lines(rel):
                parts = ln.split("cannot find function `")
                if len(parts) > 1:
                    missing = parts[1].split("`")[0]
                    break
            if missing is not None:
                msg = f"function `{missing}` is missing or not `pub` at the crate root"
            else:
                msg = _or(first_error(rel), "harness build failed")
        raise BuildError(msg=msg, log=out.stderr)
    bin_ = ctx.cache / "target" / "debug" / exe("proctor_harness")
    return (
        harness.Harness.new(
            ctx.wrapped([str(bin_)]),
            ctx.ws,
            dict(ctx.env),
            timeout,
            names,
        ),
        "",
    )


# ----------------------------------------------------------------------- stubs


def stub_source(lang: str, protos: list[sig.Proto]) -> str | None:
    """Empty bodies for the mandatory functions, so a fresh workspace compiles."""
    s = ""
    match lang:
        case "c":
            for p in protos:
                text = trim(p.sig_text).rstrip(";")
                s += text
                s += "\n{\n"
                for prm in p.params:
                    s += f"    (void){prm.name};\n"
                match p.ret:
                    case sig.TyVoid():
                        pass
                    case sig.TyFloat():
                        s += "    return 0.0;\n"
                    case _ if "*" in p.ret_raw:
                        s += "    return 0;\n"
                    case sig.TyOther():
                        s += f"    return ({p.ret_raw}){{0}};\n"
                    case _:
                        s += "    return 0;\n"
                s += "}\n\n"
        case "python":
            for p in protos:
                params = [f"{x.name}=None" if x.has_default else x.name for x in p.params]
                s += f"def {p.short_name()}({', '.join(params)}):\n    raise NotImplementedError\n\n\n"
        case "rust":
            for p in protos:
                text = trim(trim(p.sig_text).rstrip(";").rstrip("{"))
                if text.startswith("pub "):
                    text = text[len("pub ") :]
                names = [x.name for x in p.params]
                match len(names):
                    case 0:
                        unused = ""
                    case 1:
                        unused = f"    let _ = {names[0]};\n"
                    case _:
                        unused = f"    let _ = ({', '.join(names)});\n"
                s += f"/// TODO\npub {text} {{\n{unused}    todo!()\n}}\n\n"
        case _:
            return None
    return trim_end(s) + "\n"


def c_header(path: str, protos: list[sig.Proto]) -> str:
    """A C header declaring `protos`, with an include guard."""
    guard = "".join(
        c.upper() if c.isascii() and c.isalnum() else "_" for c in path.rsplit("/", 1)[-1]
    )
    s = f"#ifndef {guard}\n#define {guard}\n\n#include <stddef.h>\n\n"
    for p in protos:
        s += f"{trim(p.sig_text).rstrip(';')};\n"
    s += "\n#endif\n"
    return s


def work_paths(cache_root: Path, key: str) -> tuple[Path, Path, Path]:
    base = cache_root / key
    return base / "ws", base / "build", base
