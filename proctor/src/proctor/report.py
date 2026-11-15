"""The live terminal report."""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass

from proctor import engine
from proctor.compat import is_terminal, trim, trim_end
from proctor.unit import Unit


def truncate(s: str, max: int) -> str:
    one_line = s.replace("\n", " ").replace("\r", " ")
    if len(one_line) <= max:
        return one_line
    cut = one_line[:max]
    return f"{cut}..."


@dataclass(kw_only=True)
class Style:
    color: bool
    ascii: bool

    @staticmethod
    def detect() -> Style:
        return Style(
            color=is_terminal(sys.stdout) and os.environ.get("NO_COLOR") is None,
            ascii=os.environ.get("PROCTOR_ASCII") is not None,
        )

    def paint(self, code: str, s: str) -> str:
        if self.color:
            return f"\x1b[{code}m{s}\x1b[0m"
        return s

    def green(self, s: str) -> str:
        return self.paint("32", s)

    def red(self, s: str) -> str:
        return self.paint("31", s)

    def yellow(self, s: str) -> str:
        return self.paint("33", s)

    def dim(self, s: str) -> str:
        return self.paint("2", s)

    def bold(self, s: str) -> str:
        return self.paint("1", s)

    def ok_mark(self) -> str:
        return self.green("[ok]" if self.ascii else "✔")

    def ko_mark(self) -> str:
        return self.red("[KO]" if self.ascii else "✘")

    def skip_mark(self) -> str:
        return self.dim("[--]" if self.ascii else "·")

    def run_mark(self) -> str:
        return ">" if self.ascii else "▸"


NAME_W = 11


@dataclass(kw_only=True)
class Term(engine.Reporter):
    style: Style
    verbose: bool
    quiet: bool

    def out(self, s: str) -> None:
        if not self.quiet:
            print(s, flush=True)

    def start(self, unit: Unit, seed: int, against: str) -> None:
        s = self.style
        self.out(
            f"{s.bold('proctor')} {s.bold(unit.id)}  {s.dim(f'seed {seed:016x}  against {against}')}"
        )

    def phase_begin(self, name: str, streamed: bool) -> None:
        if streamed:
            line = f" {self.style.run_mark()} {name}"
            self.out(line)

    def item(self, item: engine.Item) -> None:
        s = self.style
        mark = s.ok_mark() if item.ok else s.ko_mark()
        if not item.detail:
            detail = ""
        elif item.ok:
            detail = f"  {s.dim(item.detail)}"
        elif item.course_error:
            detail = f"  {s.yellow(truncate(item.detail, 220))}"
        else:
            detail = f"  {s.red(truncate(item.detail, 220))}"
        self.out(f"   {mark} {truncate(item.name, 72)}{detail}")

    def phase_end(self, p: engine.Phase) -> None:
        s = self.style
        match p.status:
            case engine.Status.PASS:
                mark = s.ok_mark()
            case engine.Status.FAIL:
                mark = s.ko_mark()
            case engine.Status.SKIP:
                mark = s.skip_mark()
        if p.status == engine.Status.FAIL and not p.streamed:
            summary = s.red(truncate(p.summary, 200))
        elif p.status == engine.Status.SKIP:
            summary = s.dim(p.summary)
        else:
            summary = p.summary
        self.out(f" {mark} {p.name:<{NAME_W}} {summary}")
        if p.status == engine.Status.FAIL and not p.streamed:
            failing = [i for i in p.items if not i.ok]
            if len(failing) > 1:
                for i in failing[:8]:
                    self.out(f"   {s.ko_mark()} {i.name}  {s.red(truncate(i.detail, 200))}")
                if len(failing) > 8:
                    self.out(f"   {s.dim(f'... and {len(failing) - 8} more')}")

    def log(self, text: str) -> None:
        if self.verbose and trim(text):
            self.out(self.style.dim(trim_end(text)))


def verdict_line(v: engine.Verdict, seed: int, style: Style) -> str:
    """`OK 17/17` or `KO <item>: <reason>`."""
    match v.first_fail:
        case None if v.ok:
            return style.green(f"OK {v.passed}/{v.total}")
        case None:
            return style.red("KO")
        case (cat, item):
            if cat in ("tests", "regression"):
                line = f"KO {truncate(item.name, 60)}: {truncate(item.detail, 240)}"
            elif cat == "build" and item.name == "compile":
                line = f"KO build: {truncate(item.detail, 240)}"
            elif cat == "build":
                line = f"KO {item.name}: {truncate(item.detail, 240)}"
            else:
                line = f"KO {cat}: {truncate(item.name, 60)} {truncate(item.detail, 240)}"
            if item.random:
                line += f"   (replay: proctor check --seed {seed:016x})"
            return style.red(line)
    raise AssertionError(v.first_fail)
