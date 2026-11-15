from __future__ import annotations

from proctor.structure import glob


def test_globbing() -> None:
    assert glob("src/*.c", "src/a.c")
    assert not glob("src/*.c", "src/x/a.c")
    assert glob("src/**/*.c", "src/a.c")
    assert glob("src/**/*.c", "src/x/y/a.c")
    assert glob("**", "anything/at/all.txt")
    assert glob("lib/**", "lib/a/b.rs")
    assert glob("main.?s", "main.rs")
    assert not glob("main.rs", "src/main.rs")
