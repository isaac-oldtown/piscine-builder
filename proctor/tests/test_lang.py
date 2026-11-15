from __future__ import annotations

import sys
from pathlib import Path

import pytest
from proctor.command import Command
from proctor.compat import write_text
from proctor.unit import LangConfig
from proctor.value import ValueInt, ValueList, ValueStr

from proctor import lang, run, sig


def test_presets() -> None:
    assert lang.has_harness("c") and lang.has_harness("rust") and not lang.has_harness("go")
    assert lang.builds_in_place("rust") and not lang.builds_in_place("c")
    assert lang.exe("prog") == ("prog.exe" if sys.platform == "win32" else "prog")
    assert lang.resolve("cargo") == "cargo"
    assert lang.BUILD_TIMEOUT == 600.0
    root = Path("cache")
    assert lang.work_paths(root, "k") == (root / "k" / "ws", root / "k" / "build", root / "k")
    e = lang.BuildError(msg="m", log="l")
    assert (e.msg, e.log, str(e)) == ("m", "l", "m")
    assert lang.BuildError.new("x").log == ""


def test_cargo_names(tmp_path: Path) -> None:
    with pytest.raises(lang.BuildError) as e:
        lang.cargo_package(tmp_path)
    assert e.value.msg == "Cargo.toml is missing"
    write_text(tmp_path / "Cargo.toml", '[package]\nname = "pkg"\n\n[[bin]]\nname = "tool"\n')
    assert lang.cargo_package(tmp_path) == "pkg"
    assert lang.cargo_binary(tmp_path) == "tool"
    write_text(tmp_path / "Cargo.toml", "[workspace]\n")
    with pytest.raises(lang.BuildError) as e:
        lang.cargo_package(tmp_path)
    assert e.value.msg == "Cargo.toml has no [package] name"


def test_build_logs() -> None:
    assert lang.count_warnings("a.c:1: warning: x\nwarning: 2 warnings generated\n") == 1
    assert (
        lang.first_error("error[E0425]: cannot find value `x`\n --> src\\lib.rs:2:5\n")
        == "error[E0425]: cannot find value `x` (src/lib.rs:2:5)"
    )
    assert lang.first_error("a.c:1:2: error: bad\n") == "a.c:1:2: error: bad"
    assert lang.first_error("  \n") is None


def test_stubs_and_headers() -> None:
    c = [sig.parse("c", "size_t str_len(const char *s);", "str_len")]
    assert (
        lang.stub_source("c", c)
        == "size_t str_len(const char *s)\n{\n    (void)s;\n    return 0;\n}\n"
    )
    assert lang.c_header("include/my-str.h", c) == (
        "#ifndef MY_STR_H\n#define MY_STR_H\n\n#include <stddef.h>\n\nsize_t str_len(const char *s);\n\n#endif\n"
    )
    r = [sig.parse("rust", "pub fn add(a: i32, b: i32) -> i32", "add")]
    assert lang.stub_source("rust", r) == (
        "/// TODO\npub fn add(a: i32, b: i32) -> i32 {\n    let _ = (a, b);\n    todo!()\n}\n"
    )
    assert lang.stub_source("go", r) is None


def ctx(language: str, tmp_path: Path, **kw: object) -> lang.Ctx:
    args: dict[str, object] = {
        "lang": language,
        "ws": tmp_path / "ws",
        "work": tmp_path / "work",
        "env": {},
        "cache": tmp_path / "cache",
        "cfg": LangConfig(),
        "support": [],
        "wrap": None,
    }
    args.update(kw)
    return lang.Ctx(**args)  # type: ignore[arg-type]


STR_C = """\
#include <stddef.h>
#include "str.h"

size_t str_len(const char *s)
{
    size_t n = 0;
    while (s[n])
        n++;
    return n;
}

void str_upper(char *s)
{
    for (; *s; s++)
        if (*s >= 'a' && *s <= 'z')
            *s = (char)(*s - 32);
}

char **pair(const char *a, const char *b)
{
    static char *v[3];
    v[0] = (char *)a;
    v[1] = (char *)b;
    v[2] = NULL;
    return v;
}

int main(void)
{
    return 3;
}
"""

C_SIGS = [
    ("size_t str_len(const char *s)", "str_len"),
    ("void str_upper(char *s)", "str_upper"),
    ("char **pair(const char *a, const char *b)", "pair"),
]


@pytest.mark.skipif(lang.cc() is None, reason="no C compiler")
def test_c_harness_and_program(tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    (ws / "inc").mkdir(parents=True)
    protos = [sig.parse("c", s, n) for s, n in C_SIGS]
    write_text(ws / "str.c", STR_C)
    write_text(ws / "inc" / "str.h", lang.c_header("inc/str.h", protos))
    c = ctx("c", tmp_path)
    h, _ = lang.build_harness(c, protos, None, 5.0)
    try:
        assert h.funcs == {"str_len", "str_upper", "pair"}
        assert h.call("str_len", [ValueStr("hello")]) == ValueInt(5)
        assert h.call("str_upper", [ValueStr("abc")]) == ValueStr("ABC")
        assert h.call("pair", [ValueStr("x"), ValueStr("y")]) == ValueList(
            [ValueStr("x"), ValueStr("y")]
        )
    finally:
        h.close()
    cmd, _ = lang.build_program(c, None, None)
    assert cmd == [str(tmp_path / "work" / lang.exe("prog"))]
    out = run.run(Command(cmd[0]), None, 10.0)
    assert out.code == 3


@pytest.mark.skipif(lang.cc() is None, reason="no C compiler")
def test_c_build_errors(tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    ws.mkdir()
    write_text(ws / "bad.c", "int f(void)\n{\n    int x;\n    return 0;\n}\n")
    with pytest.raises(lang.BuildError) as e:
        lang.build_program(ctx("c", tmp_path), None, None)
    assert e.value.msg.startswith("bad.c:3:")
    assert "unused variable" in e.value.msg
    assert "unused variable" in e.value.log
    write_text(ws / "bad.c", "int f(void)\n{\n    return 0;\n}\n")
    protos = [sig.parse("c", "size_t str_len(const char *s)", "str_len")]
    with pytest.raises(lang.BuildError) as e:
        lang.build_harness(ctx("c", tmp_path), protos, None, 5.0)
    assert e.value.msg == "function `str_len` is not defined (or is static)"
    with pytest.raises(lang.BuildError) as e:
        lang.build_program(ctx("c", tmp_path, cfg=LangConfig(compiler="no-such-cc")), None, None)
    assert e.value.msg.startswith("cannot run `no-such-cc`: ")


@pytest.mark.skipif(lang.python() is None, reason="no Python 3")
def test_python_harness_and_program(tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    ws.mkdir()
    write_text(ws / "m.py", "def add(a: int, b: int) -> int:\n    return a + b\n")
    write_text(ws / "main.py", "import sys\nprint(sys.argv[1:])\n")
    c = ctx("python", tmp_path)
    protos = [sig.parse("python", "def add(a: int, b: int) -> int", "m.add")]
    h, log = lang.build_harness(c, protos, None, 5.0)
    try:
        assert log == ""
        assert h.call("m.add", [ValueInt(1), ValueInt(2)]) == ValueInt(3)
    finally:
        h.close()
    cmd, _ = lang.build_program(c, "python main.py x", None)
    assert cmd == [lang.python(), "main.py", "x"]
    write_text(ws / "broken.py", "def f(:\n")
    with pytest.raises(lang.BuildError) as e:
        lang.build_program(c, "python main.py", None)
    assert "SyntaxError" in e.value.msg


@pytest.mark.skipif(not run.exists(lang.resolve("bash")), reason="no bash")
def test_bash_syntax_check(tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    ws.mkdir()
    write_text(ws / "ok.sh", "echo hi\n")
    c = ctx("bash", tmp_path, wrap="  ")
    cmd, _ = lang.build_program(c, "bash ok.sh", None)
    assert cmd == [lang.resolve("bash"), "ok.sh"]
    with pytest.raises(lang.BuildError) as e:
        lang.build_program(c, None, None)
    assert e.value.msg == "this unit needs a `run:` command for its program"
    write_text(ws / "bad.sh", "if then\n")
    with pytest.raises(lang.BuildError) as e:
        lang.build_program(c, "bash ok.sh", None)
    assert "bad.sh" in e.value.msg
