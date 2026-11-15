from __future__ import annotations

import sys
from pathlib import Path

import pytest
from proctor.errors import Error

from coursecrafter import cli


# Computed by the Rust `text_hash`: courses approved by the Rust tools keep
# their approval.
@pytest.mark.parametrize(
    ("text", "hash"),
    [
        ("", "cbf29ce484222325"),
        ("a", "af63dc4c8601ec8c"),
        ("hello\r\nworld\n", "2925835410388969"),
        ("é\u00e9 ünïcode 😀", "8fbb5bf6dd847ce2"),
        ("---\ntitle: x\n---\n## d01-a: A\n", "75c6b5e7b70d64bd"),
        ("\r\n\r", "08547707b5083970"),
        ("x\r\r\n", "be6a22197ed77d9c"),
    ],
)
def test_text_hash_matches_rust(text: str, hash: str) -> None:
    assert cli.text_hash(text) == hash


def test_approve_writes_the_hash(tmp_path: Path) -> None:
    cli.approve(tmp_path, "a")
    assert (
        tmp_path / ".proctor" / "coursecrafter" / "approved"
    ).read_bytes() == b"af63dc4c8601ec8c"


def test_args() -> None:
    a = cli.Args.parse(
        ["x", "--course", "c1", "--yes", "y", "--only", "a", "--only", "b", "--course", "c2"],
        ["course", "only"],
        ["yes"],
    )
    assert a.positional == ["x", "y"]
    assert a.get("course") == "c2"
    assert a.all("only") == ["a", "b"]
    assert a.has("yes") and not a.has("debug")
    with pytest.raises(Error, match="^--course needs a value$"):
        cli.Args.parse(["--course"], ["course"], [])
    with pytest.raises(Error, match="^unknown option --x$"):
        cli.Args.parse(["--x"], ["course"], [])


def test_unwrap_curriculum() -> None:
    assert cli.unwrap_curriculum("hi <curriculum>\n---\nt\n</curriculum>") == "---\nt"
    assert cli.unwrap_curriculum("Here:\n```markdown\n---\ntitle: x\n```\nbye") == "---\ntitle: x\n"
    assert cli.unwrap_curriculum("  ---\ntitle: x\n```c\n```\n ") == "---\ntitle: x\n```c\n```"


@pytest.mark.skipif(sys.platform != "win32", reason="Windows separators")
def test_course_paths_display_as_rust_joins_them() -> None:
    p = cli.RustPath(".")
    assert f"{p / 'curriculum.md'}" == ".\\curriculum.md"
    assert f"{p / '.proctor' / 'coursecrafter'}" == ".\\.proctor\\coursecrafter"
    assert f"{cli.RustPath('C:/x/') / 'a'}" == "C:/x/a"
    assert f"{cli.RustPath('') / 'a'}" == "a"
    assert str(p / "curriculum.md") == "curriculum.md"
    assert p / "a" == Path("a")


def test_help_and_unknown_command(capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.run_main([]) == 0
    assert capsys.readouterr().out == cli.USAGE + "\n"
    assert cli.run_main(["nope"]) == 2
    assert capsys.readouterr().err == f"error: unknown command `nope`\n\n{cli.USAGE}\n"
