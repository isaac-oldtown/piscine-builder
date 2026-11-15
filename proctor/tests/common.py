"""Helpers shared by the integration tests: scratch courses in the temp
folder, and whether this machine has a unit's toolchain."""

from __future__ import annotations

import os
import shutil
import tempfile
from pathlib import Path
from types import TracebackType

from proctor.compat import write_text
from proctor.unit import Unit

from proctor import course, lang, run

DEMO = Path(__file__).resolve().parents[2] / "examples" / "demo-course"


class Scratch:
    """A scratch folder in the temp folder, removed when the `with` block ends."""

    def __init__(self, name: str) -> None:
        self.path = Path(tempfile.gettempdir()) / f"proctor-{name}-{os.getpid()}"
        shutil.rmtree(self.path, ignore_errors=True)
        self.path.mkdir(parents=True)

    def write(self, rel: str, body: str) -> None:
        """Writes `body` at `rel` inside the scratch folder."""
        p = self.path / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        write_text(p, body)

    def __enter__(self) -> Scratch:
        return self

    def __exit__(
        self,
        kind: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        shutil.rmtree(self.path, ignore_errors=True)


def demo_copy(name: str) -> Scratch:
    """A fresh copy of the demo course."""
    s = Scratch(name)
    copy_course(DEMO, s.path)
    return s


def copy_course(src: Path, dst: Path) -> None:
    """Copies a course, without a cache left by running it in place."""
    dst.mkdir(parents=True, exist_ok=True)
    for e in src.iterdir():
        if e.is_dir():
            if e.name != "cache":
                copy_course(e, dst / e.name)
        else:
            shutil.copy(e, dst / e.name)


def missing_tool(c: course.CourseDir, u: Unit) -> str | None:
    """Why `u` cannot be checked on this machine, if it cannot."""
    language = c.language(u)
    if language == "c":
        return "no C compiler" if lang.cc() is None else None
    if language == "python":
        return "no Python 3" if lang.python() is None else None
    if language == "rust":
        program = "cargo"
    elif language in ("bash", "sh", "shell"):
        program = "bash"
    else:
        cmd = c.lang_config(u).run
        words = run.split_words(cmd) if cmd is not None else []
        program = words[0] if words else language
    return None if run.exists(lang.resolve(program)) else f"no `{program}`"
