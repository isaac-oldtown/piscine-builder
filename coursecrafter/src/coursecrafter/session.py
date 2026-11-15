"""The log of one coursecrafter run (`new`, `revise`, `convert`, `build`):
every LLM call with its purpose, time and tokens, cache hits, rejections,
autofixes, and a closing summary. It goes to
`.proctor/coursecrafter/logs/<command>-<date>-<time>.log`; `--debug` also
prints it to the terminal as it is written."""

from __future__ import annotations

import sys
import time
from collections.abc import Callable
from pathlib import Path
from typing import TypeVar

from proctor.compat import byte_len, lines, trim_end

from coursecrafter import llm

T = TypeVar("T")


def civil(t: int) -> tuple[str, str]:
    """`YYYY-MM-DD` and `HH:MM:SS` (UTC) of a Unix time."""
    days, secs = t // 86_400, t % 86_400
    # Howard Hinnant's days-to-civil algorithm.
    z = days + 719_468
    era = z // 146_097
    doe = z - era * 146_097
    yoe = (doe - doe // 1460 + doe // 36_524 - doe // 146_096) // 365
    doy = doe - (365 * yoe + yoe // 4 - yoe // 100)
    mp = (5 * doy + 2) // 153
    d = doy - (153 * mp + 2) // 5 + 1
    m = mp + 3 if mp < 10 else mp - 9
    y = yoe + era * 400 + (1 if m <= 2 else 0)
    return (
        f"{y:04}-{m:02}-{d:02}",
        f"{secs // 3600:02}:{secs // 60 % 60:02}:{secs % 60:02}",
    )


def secs(d: float) -> str:
    s = d
    if s >= 60.0:
        return f"{int(s / 60.0)}m{int(s) % 60:02}s"
    return f"{s:.1f}s"


def tokens(u: llm.Usage) -> str:
    return f"in {u.input} (fresh {u.fresh()}, cache read {u.cache_read}, cache write {u.cache_write}), out {u.output}"


def add(a: llm.Usage, b: llm.Usage) -> None:
    a.input += b.input
    a.output += b.output
    a.cache_read += b.cache_read
    a.cache_write += b.cache_write


class Session:
    path: Path
    debug: bool
    # Rust's `start: Instant`, renamed: `start` is the constructor.
    started: float
    llm_time: float
    user_time: float
    # Calls per purpose: (count, tokens).
    purposes: dict[str, tuple[int, llm.Usage]]
    calls: list[tuple[str, llm.CallRecord]]
    # Replies read from the on-disk cache instead of asking the model.
    cache_hits: int
    # Extra summary lines (the command's own counts).
    notes: list[str]
    questions: int
    drafts: int
    rejections: int
    autofixes: int
    revisions: int

    def __init__(self, path: Path, debug: bool) -> None:
        self.path = path
        self.debug = debug
        self.started = time.monotonic()
        self.llm_time = 0.0
        self.user_time = 0.0
        self.purposes = {}
        self.calls = []
        self.cache_hits = 0
        self.notes = []
        self.questions = 0
        self.drafts = 0
        self.rejections = 0
        self.autofixes = 0
        self.revisions = 0

    @staticmethod
    def start(course: Path, command: str, debug: bool) -> Session:
        from proctor import course as proctor_course
        from proctor import journal

        date, time_ = civil(journal.now())
        dir = course / proctor_course.HIDDEN / "coursecrafter" / "logs"
        try:
            dir.mkdir(parents=True, exist_ok=True)
        except OSError:
            pass
        path = dir / f"{command}-{date}-{time_.replace(':', '')}.log"
        s = Session(path, debug)
        s.log(f"coursecrafter {command}, started {date} {time_} UTC")
        return s

    def log(self, msg: str) -> None:
        """One line, prefixed with the time since the start."""
        at = secs(time.monotonic() - self.started)
        text = "".join(f"[{at:>7}] {line}\n" for line in lines(msg))
        try:
            with open(self.path, "ab") as f:
                f.write(text.encode("utf-8"))
        except OSError:
            pass
        if self.debug:
            print(text, end="", file=sys.stderr)

    def say(self, msg: str) -> None:
        """A line for the user: on the terminal, and in the log."""
        self.log(msg)
        if not self.debug:
            print(msg, file=sys.stderr)

    def chat(self, llm_: llm.Llm, purpose: str, messages: list[llm.Msg]) -> str:
        """`llm.chat`, timed and logged under `purpose`."""
        return self.chat_as(llm_, purpose, purpose, messages)

    def chat_as(self, llm_: llm.Llm, label: str, purpose: str, messages: list[llm.Msg]) -> str:
        """Like `chat`; `label` names the call in its log line (`contract-d02`),
        `purpose` groups it in the summary (`contract`)."""
        chars = sum(byte_len(m.content) for m in messages)
        self.log(f"call {len(self.calls) + 1} ({label}): {len(messages)} message(s), {chars} chars")
        err: llm.ChatError | None = None
        res = ""
        try:
            res = llm_.chat(messages)
        except llm.ChatError as e:
            err = e
        rec = llm_.history[-1] if llm_.history else llm.CallRecord()
        self.llm_time += rec.ms / 1000
        for msg in rec.errors:
            self.log(f"  error: {msg}")
        speed = rec.usage.output * 1000.0 / rec.ms if rec.ms > 0 else 0.0
        status = "answered" if rec.ok else "FAILED"
        attempts = f" after {rec.attempts} attempts" if rec.attempts > 1 else ""
        self.log(
            f"  {status} in {secs(rec.ms / 1000)}{attempts}: {tokens(rec.usage)}, {speed:.0f} output tokens/s"
        )
        n, u = self.purposes.get(purpose, (0, llm.Usage()))
        add(u, rec.usage)
        self.purposes[purpose] = (n + 1, u)
        self.calls.append((purpose, rec))
        if err is not None:
            raise err
        return res

    def cached(self, label: str) -> None:
        """A reply served from the on-disk cache."""
        self.cache_hits += 1
        self.log(f"cached reply ({label}): no call")

    def wait_user(self, f: Callable[[], T]) -> T:
        """Runs `f` (a question or a menu) and counts its time as the user's."""
        t = time.monotonic()
        out = f()
        self.user_time += time.monotonic() - t
        return out

    def finish(self, llm_: llm.Llm, outcome: str) -> None:
        """The closing summary; `outcome` says how the conversation ended."""
        total = time.monotonic() - self.started
        local = max(0.0, total - (self.llm_time + self.user_time))
        sum_ = llm.Usage()
        for _, r in self.calls:
            add(sum_, r.usage)
        failed = sum(1 for _, r in self.calls if not r.ok)
        retries = sum(max(0, r.attempts - 1) for _, r in self.calls)
        s = f"summary: {outcome}\n"
        s += f"  time        {secs(total)} total: {secs(self.llm_time)} waiting for the model, {secs(self.user_time)} for you, {secs(local)} local work\n"
        s += f"  calls       {len(self.calls)} ({failed} failed, {retries} HTTP retries) to {llm_.model} at {llm_.base_url}\n"
        for p, (n, u) in sorted(self.purposes.items()):
            s += f"    {p:<12} {n} call(s): {tokens(u)}\n"
        s += f"  tokens      {tokens(sum_)}\n"
        # `max_by_key` keeps the last of equal maxima.
        slowest: tuple[str, llm.CallRecord] | None = None
        for c in self.calls:
            if slowest is None or c[1].ms >= slowest[1].ms:
                slowest = c
        if slowest is not None:
            p, r = slowest
            s += f"  slowest     {secs(r.ms / 1000)} ({p}), average {secs(self.llm_time / max(1, len(self.calls)))} per call\n"
        if self.cache_hits > 0:
            s += f"  cache       {self.cache_hits} reply(ies) reused from disk at no cost\n"
        if self.drafts > 0 or self.questions > 0:
            s += f"  planning    {self.questions} question(s), {self.drafts} draft(s), {self.rejections} validator rejection(s), {self.autofixes} autofix(es), {self.revisions} revision(s) asked\n"
        for n_ in self.notes:
            s += f"  {n_}\n"
        self.log(trim_end(s))
