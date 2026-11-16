"""`coursecrafter new`: a short planning conversation in the terminal. It ends
with the same curriculum file the browser path produces, so both go
through one validator and one approval step.

The planner asks one multiple-choice question per reply, so each question
can build on the previous answers.
Its first valid draft gets one self-review against the planning checklist
before the student sees it."""

from __future__ import annotations

import enum
import functools
import math
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from proctor.compat import (
    float_display,
    io_error,
    is_terminal,
    read_text,
    trim,
    trim_end,
    write_text,
)
from proctor.errors import Err, Error, Ok, Result

from coursecrafter import curriculum, llm, prompts, session
from proctor import ask, journal, progress

MAX_AUTOFIX = 3
# Questions in a whole conversation, asked one per planner reply.
MAX_QUESTIONS = 10
OTHER = "Something else: type your own answer"


@dataclass(kw_only=True)
class Question:
    question: str
    options: list[str] = field(default_factory=list)
    recommended: int = 0


def parse_questions(body: str) -> list[Question]:
    """Parses the `<questions>` JSON. Accepts a bare array or `{"questions": [...]}`."""
    qs = _shape(trim(body))
    out: list[Question] = []
    for q in qs:
        if not trim(q.question):
            continue
        if len(out) == MAX_QUESTIONS:
            break
        q.options = [o for o in q.options if trim(o)]
        q.recommended = min(q.recommended, max(len(q.options) - 1, 0))
        out.append(q)
    if not out:
        raise Error("no questions")
    return out


# ------------------------------------------------- serde_json, for `Shape`
#
# The Rust code reads the block with `serde_json::from_str::<Shape>`, where
# `Shape` is `#[serde(untagged)] enum { List(Vec<Question>), Wrapped {
# questions: Vec<Question> } }`. Its error message goes back to the planner
# and into the log, so the parser below reports serde_json's syntax errors
# with its words and positions (line, byte column), and the shape rules are
# serde's: unknown keys ignored, a duplicate known key or a wrong type fails
# the variant, a struct may also be written as an array of its fields.


@dataclass(frozen=True)
class _Map:
    """A JSON object, its entries in order (duplicates kept, as serde sees them)."""

    pairs: list[tuple[str, Any]]


class _Json:
    """serde_json's `Deserializer` over a `&str`, building plain values:
    `str`, `int` (an unsigned integer that fits u64), `float` (any other
    number), `bool`, `None`, `list`, `_Map`."""

    def __init__(self, text: str) -> None:
        self.s = text.encode("utf-8", "surrogatepass")
        self.i = 0
        self.remaining_depth = 128

    def _at(self, i: int, msg: str) -> Error:
        start = self.s.rfind(b"\n", 0, i) + 1
        line = 1 + self.s.count(b"\n", 0, start)
        return Error(f"{msg} at line {line} column {i - start}")

    def error(self, msg: str) -> Error:
        return self._at(self.i, msg)

    def peek_error(self, msg: str) -> Error:
        return self._at(min(len(self.s), self.i + 1), msg)

    def peek(self) -> int | None:
        return self.s[self.i] if self.i < len(self.s) else None

    def peek_or_null(self) -> int:
        return self.s[self.i] if self.i < len(self.s) else 0

    def next_char(self) -> int | None:
        c = self.peek()
        if c is not None:
            self.i += 1
        return c

    def whitespace(self) -> int | None:
        while self.i < len(self.s) and self.s[self.i] in b" \n\t\r":
            self.i += 1
        return self.peek()

    def value(self) -> Any:
        c = self.whitespace()
        if c is None:
            raise self.peek_error("EOF while parsing a value")
        for word, v in ((b"null", None), (b"true", True), (b"false", False)):
            if c == word[0]:
                self.i += 1
                for expected in word[1:]:
                    n = self.next_char()
                    if n is None:
                        raise self.error("EOF while parsing a value")
                    if n != expected:
                        raise self.error("expected ident")
                return v
        if c == ord("-"):
            self.i += 1
            return self.number(False)
        if ord("0") <= c <= ord("9"):
            return self.number(True)
        if c == ord('"'):
            self.i += 1
            return self.string()
        if c == ord("["):
            return self.seq()
        if c == ord("{"):
            return self.map()
        raise self.peek_error("expected value")

    def digits(self) -> int:
        n = 0
        while ord("0") <= self.peek_or_null() <= ord("9"):
            self.i += 1
            n += 1
        return n

    def number(self, positive: bool) -> Any:
        start = self.i
        c = self.next_char()
        if c is None:
            raise self.error("EOF while parsing a value")
        if c == ord("0"):
            # There can be only one leading '0'.
            if ord("0") <= self.peek_or_null() <= ord("9"):
                raise self.peek_error("invalid number")
        elif ord("1") <= c <= ord("9"):
            self.digits()
        else:
            raise self.error("invalid number")
        integer = True
        if self.peek_or_null() == ord("."):
            integer = False
            self.i += 1
            if self.digits() == 0:
                if self.peek() is not None:
                    raise self.peek_error("invalid number")
                raise self.peek_error("EOF while parsing a value")
        if self.peek_or_null() in (ord("e"), ord("E")):
            integer = False
            self.i += 1
            if self.peek_or_null() in (ord("+"), ord("-")):
                self.i += 1
            n = self.next_char()
            if n is None:
                raise self.error("EOF while parsing a value")
            if not ord("0") <= n <= ord("9"):
                raise self.error("invalid number")
            self.digits()
        text = self.s[start : self.i].decode("ascii")
        if integer and positive and int(text) <= (1 << 64) - 1:
            return int(text)
        f = float(text)
        if math.isinf(f):
            raise self.error("number out of range")
        return f if positive else -f

    def string(self) -> str:
        out = bytearray()
        while True:
            start = self.i
            while self.i < len(self.s) and self.s[self.i] not in b'"\\' and self.s[self.i] >= 0x20:
                self.i += 1
            out += self.s[start : self.i]
            if self.i == len(self.s):
                raise self.error("EOF while parsing a string")
            c = self.s[self.i]
            self.i += 1
            if c == ord('"'):
                return out.decode("utf-8", "surrogatepass")
            if c == ord("\\"):
                self.escape(out)
                continue
            raise self.error("control character (\\u0000-\\u001F) found while parsing a string")

    def escape(self, out: bytearray) -> None:
        c = self.next_char()
        if c is None:
            raise self.error("EOF while parsing a string")
        simple = {
            '"': '"',
            "\\": "\\",
            "/": "/",
            "b": "\b",
            "f": "\f",
            "n": "\n",
            "r": "\r",
            "t": "\t",
        }
        if chr(c) in simple:
            out += simple[chr(c)].encode()
            return
        if c != ord("u"):
            raise self.error("invalid escape")
        n = self.hex_escape()
        if 0xDC00 <= n <= 0xDFFF:
            raise self.error("lone leading surrogate in hex escape")
        if not 0xD800 <= n <= 0xDBFF:
            out += chr(n).encode("utf-8")
            return
        # A leading surrogate: a trailing one must follow.
        for expected in b"\\u":
            c2 = self.peek()
            if c2 is None:
                raise self.error("EOF while parsing a string")
            self.i += 1
            if c2 != expected:
                raise self.error("unexpected end of hex escape")
        n2 = self.hex_escape()
        if not 0xDC00 <= n2 <= 0xDFFF:
            raise self.error("lone leading surrogate in hex escape")
        out += chr((((n - 0xD800) << 10) | (n2 - 0xDC00)) + 0x1_0000).encode("utf-8")

    def hex_escape(self) -> int:
        if self.i + 4 > len(self.s):
            self.i = len(self.s)
            raise self.error("EOF while parsing a string")
        digits = self.s[self.i : self.i + 4]
        self.i += 4
        if not all(chr(d) in "0123456789abcdefABCDEF" for d in digits):
            raise self.error("invalid escape")
        return int(digits, 16)

    def enter(self) -> None:
        self.remaining_depth -= 1
        if self.remaining_depth == 0:
            raise self.peek_error("recursion limit exceeded")
        self.i += 1

    def seq(self) -> list[Any]:
        self.enter()
        items: list[Any] = []
        first = True
        while True:
            c = self.whitespace()
            if c is None:
                raise self.peek_error("EOF while parsing a list")
            if c == ord("]"):
                break
            if first:
                first = False
            elif c == ord(","):
                self.i += 1
                c = self.whitespace()
                if c == ord("]"):
                    raise self.peek_error("trailing comma")
                if c is None:
                    raise self.peek_error("EOF while parsing a value")
            else:
                raise self.peek_error("expected `,` or `]`")
            items.append(self.value())
        self.remaining_depth += 1
        self.i += 1
        return items

    def map(self) -> _Map:
        self.enter()
        pairs: list[tuple[str, Any]] = []
        first = True
        while True:
            c = self.whitespace()
            if c is None:
                raise self.peek_error("EOF while parsing an object")
            if c == ord("}"):
                break
            if first:
                first = False
                if c != ord('"'):
                    raise self.peek_error("key must be a string")
            elif c == ord(","):
                self.i += 1
                c = self.whitespace()
                if c == ord("}"):
                    raise self.peek_error("trailing comma")
                if c is None:
                    raise self.peek_error("EOF while parsing a value")
                if c != ord('"'):
                    raise self.peek_error("key must be a string")
            else:
                raise self.peek_error("expected `,` or `}`")
            self.i += 1
            key = self.string()
            c = self.whitespace()
            if c is None:
                raise self.peek_error("EOF while parsing an object")
            if c != ord(":"):
                raise self.peek_error("expected `:`")
            self.i += 1
            pairs.append((key, self.value()))
        self.remaining_depth += 1
        self.i += 1
        return _Map(pairs)


def _fields(v: Any, names: list[str]) -> dict[str, Any] | None:
    """A struct's fields as serde reads them from a map (unknown keys
    ignored, a repeated field rejected) or from an array of the fields in
    order (none missing that has no default, none extra)."""
    if isinstance(v, _Map):
        out: dict[str, Any] = {}
        for k, x in v.pairs:
            if k in names:
                if k in out:
                    return None
                out[k] = x
        return out if names[0] in out else None
    if isinstance(v, list):
        if not 1 <= len(v) <= len(names):
            return None
        return dict(zip(names, v, strict=False))
    return None


def _question(v: Any) -> Question | None:
    f = _fields(v, ["question", "options", "recommended"])
    if f is None:
        return None
    question, options, recommended = f["question"], f.get("options", []), f.get("recommended", 0)
    if not isinstance(question, str):
        return None
    if not isinstance(options, list) or not all(isinstance(o, str) for o in options):
        return None
    if type(recommended) is not int:
        return None
    return Question(question=question, options=list(options), recommended=recommended)


def _questions(v: Any) -> list[Question] | None:
    if not isinstance(v, list):
        return None
    out: list[Question] = []
    for x in v:
        q = _question(x)
        if q is None:
            return None
        out.append(q)
    return out


def _shape(text: str) -> list[Question]:
    """`serde_json::from_str::<Shape>(text)`, the error as `e.to_string()`."""
    de = _Json(text)
    v = de.value()
    qs = _questions(v)
    if qs is None and isinstance(v, _Map):
        # Shape::Wrapped: an untagged struct variant is only read from a map.
        f = _fields(v, ["questions"])
        if f is not None:
            qs = _questions(f["questions"])
    if qs is None:
        raise Error("data did not match any variant of untagged enum Shape")
    if de.whitespace() is not None:
        raise de.peek_error("trailing characters")
    return qs


# ------------------------------------------------------------ conversation


def answers_message(qs: list[Question], answers: list[str]) -> str:
    """One line per question for the planner: "question -> answer"."""
    s = "My answers:\n"
    for q, a in zip(qs, answers, strict=False):
        s += f"- {trim(q.question)} -> {a}\n"
    return s


def ask_all(qs: list[Question], interactive: bool, first: int) -> list[str]:
    """Asks each question in turn; `first` is how many were asked before.
    Without a terminal, or with `--yes`, the recommended options are taken."""
    from coursecrafter import cli

    out: list[str] = []
    for i, q in enumerate(qs):
        n = first + i + 1
        if not q.options:
            if not interactive:
                out.append("No preference, use your judgement.")
                continue
            print(f"\n? Question {n}: {q.question}")
            a = cli.read_line("Your answer (Enter: no preference): ")
            out.append("No preference, use your judgement." if not a else a)
            continue
        if not interactive:
            out.append(q.options[q.recommended])
            continue
        title = f"Question {n}: {q.question}"
        out.append(ask.choose(title, q.options, q.recommended, OTHER).text(q.options))
    return out


def ids(plan: curriculum.Plan) -> set[str]:
    return {e.id for e in plan.entries}


def list_(v: list[str]) -> str:
    if not v:
        return "none"
    return ", ".join(v)


class Decision:
    """What the student does with a draft."""


@dataclass(frozen=True)
class DecisionApprove(Decision):
    pass


@dataclass(frozen=True)
class DecisionEdit(Decision):
    pass


@dataclass(frozen=True)
class DecisionQuit(Decision):
    pass


@dataclass(frozen=True)
class DecisionRevise(Decision):
    f0: str


def decide() -> Decision:
    from coursecrafter import cli

    options = [
        "Approve it",
        "Ask the planner for changes",
        "Edit curriculum.md myself",
        "Quit and keep the draft",
    ]
    # Typing the changes directly at the prompt also works (same as b).
    match ask.choose_or_type("What do you want to do with this curriculum?", options, 0):
        case ask.AnswerPick(0):
            return DecisionApprove()
        case ask.AnswerPick(1):
            c = cli.read_line("Changes you want: ")
            if not c:
                return decide()
            return DecisionRevise(c)
        case ask.AnswerPick(2):
            return DecisionEdit()
        case ask.AnswerPick(_):
            return DecisionQuit()
        case ask.AnswerOther(c):
            return DecisionRevise(c)
    raise AssertionError("unreachable")


def cmd_new(raw: list[str]) -> bool:
    from coursecrafter import cli

    valued = ["course", "from"]
    valued.extend(cli.LLM_OPTS)
    a = cli.Args.parse(raw, valued, ["yes", "debug"])
    course = a.course()
    intake = " ".join(a.positional)
    for f in a.all("from"):
        try:
            body = read_text(f)
        except OSError as e:
            raise Error(f"{f}: {io_error(e)}") from None
        intake += f"\n\nMaterial from {f}:\n{body}"
    if not trim(intake):
        raise Error(
            'say what you want to learn: coursecrafter new "learn C pointers" [--from syllabus.txt]'
        )
    interactive = is_terminal(sys.stdin) and not a.has("yes")
    llm_ = a.llm()
    return run(course, llm_, intake, interactive, None, Mode.NEW, a.has("debug"))


def cmd_revise(raw: list[str]) -> bool:
    """`coursecrafter revise` : the planner rewrites the current curriculum.md."""
    from coursecrafter import cli

    valued = ["course"]
    valued.extend(cli.LLM_OPTS)
    a = cli.Args.parse(raw, valued, ["yes", "debug"])
    changes = " ".join(a.positional)
    if not trim(changes):
        raise Error('say what to change: coursecrafter revise "split p01.4 into two stages"')
    course = a.course()
    file = course / "curriculum.md"
    try:
        text = read_text(file)
    except OSError:
        raise Error(
            f"no curriculum at {file}: run `coursecrafter new` or `coursecrafter import` first"
        ) from None
    try:
        plan = curriculum.parse(text)
    except curriculum.ParseError as p:
        raise Error(
            "curriculum.md does not parse; fix it by hand or with `coursecrafter import curriculum.md`:\n"
            f"{curriculum.report(p.problems)}"
        ) from None
    # The draft replaces curriculum.md while you review it: keep the old one.
    backup = course / ".proctor" / "coursecrafter" / "curriculum.previous.md"
    try:
        backup.parent.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        raise Error(io_error(e)) from None
    try:
        write_text(backup, text)
    except OSError as e:
        raise Error(f"{backup}: {io_error(e)}") from None
    interactive = is_terminal(sys.stdin) and not a.has("yes")
    llm_ = a.llm()
    print(f"previous version saved to {backup}", file=sys.stderr)
    return run(
        course,
        llm_,
        prompts.revise_request(text, changes, measured(course, plan)),
        interactive,
        plan,
        Mode.REVISE,
        a.has("debug"),
    )


def measured(course: Path, plan: curriculum.Plan) -> str:
    """The planned and measured hours of every unit started so far, as a
    table ("" before the first unlock)."""
    stats = progress.compute(journal.read(course)).units
    rows = []
    planned_done = measured_done = 0.0
    for e in plan.units():
        s = stats.get(e.id)
        if s is None or (s.unlocked is None and s.attempts == 0):
            continue
        h = s.active / 3600
        planned = "?" if e.f.hours is None else float_display(e.f.hours)
        state = "passed" if s.passed is not None else "in progress"
        rows.append(f"| {e.id} | {planned} | {h:.1f} | {s.attempts} | {state} |")
        if s.passed is not None and e.f.hours is not None:
            planned_done += e.f.hours
            measured_done += h
    if not rows:
        return ""
    table = "\n".join(
        ["| Unit | Planned h | Measured h | Checks | State |", "|---|---|---|---|---|", *rows]
    )
    if planned_done > 0:
        planned = float_display(round(planned_done, 1))
        table += f"\n\nPassed units: {planned} h planned, {measured_done:.1f} h measured."
    return f"{table}\n"


def cmd_convert(raw: list[str]) -> bool:
    """`coursecrafter convert`: a curriculum written elsewhere (any outline,
    syllabus or plan) rewritten in the curriculum format, keeping its content."""
    from coursecrafter import cli

    valued = ["course"]
    valued.extend(cli.LLM_OPTS)
    a = cli.Args.parse(raw, valued, ["yes", "debug"])
    if not a.positional:
        raise Error(
            "give the file(s) holding your curriculum: coursecrafter convert my-plan.md [notes.txt]..."
        )
    source = ""
    for f in a.positional:
        try:
            body = read_text(f)
        except OSError as e:
            raise Error(f"{f}: {io_error(e)}") from None
        source += f'<file name="{f}">\n{trim_end(body)}\n</file>\n'
    course = a.course()
    file = course / "curriculum.md"
    try:
        old: str | None = read_text(file)
    except OSError:
        old = None
    if old is not None:
        backup = course / ".proctor" / "coursecrafter" / "curriculum.previous.md"
        try:
            backup.parent.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            raise Error(io_error(e)) from None
        try:
            write_text(backup, old)
        except OSError as e:
            raise Error(f"{backup}: {io_error(e)}") from None
        print(f"the existing curriculum.md is saved to {backup}", file=sys.stderr)
    interactive = is_terminal(sys.stdin) and not a.has("yes")
    llm_ = a.llm()
    return run(
        course,
        llm_,
        prompts.convert_request(source),
        interactive,
        None,
        Mode.CONVERT,
        a.has("debug"),
    )


class Mode(enum.Enum):
    # Plan from a wish: questions, then one self-review of the first draft.
    NEW = "new"
    # Change the current curriculum.md; the request includes the review.
    REVISE = "revise"
    # Rewrite an outside curriculum in the format, no restructuring review.
    CONVERT = "convert"

    def name(self) -> str:  # type: ignore[override]
        """Rust `Mode::name` (also the member's value)."""
        return str(self.value)


def run(
    course: Path,
    llm_: llm.Llm,
    intake: str,
    interactive: bool,
    current: curriculum.Plan | None,
    mode: Mode,
    debug: bool,
) -> bool:
    """`converse` with its log: the details go to the session log (and to the
    terminal with `--debug`), the conversation itself stays on screen."""
    log = session.Session.start(course, mode.name(), debug)
    log.log(f"model {llm_.model} at {llm_.base_url}")
    failure: Error | None = None
    ok = False
    try:
        ok = converse(log, course, llm_, intake, interactive, current, mode)
    except Error as e:
        failure = e
    if failure is not None:
        outcome = f"error: {failure.msg}"
    elif ok:
        outcome = "approved"
    else:
        outcome = "not approved, the draft is kept"
    log.finish(llm_, outcome)
    if debug or failure is not None:
        print(f"log: {log.path}", file=sys.stderr)
    if failure is not None:
        raise failure
    return ok


def _write_draft(course: Path, text: str) -> None:
    """`create_dir_all(course)` and `fs::write(course/curriculum.md)`, errors as `e.to_string()`."""
    try:
        course.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        raise Error(io_error(e)) from None
    try:
        write_text(course / "curriculum.md", text)
    except OSError as e:
        raise Error(io_error(e)) from None


def _review(course: Path, text: str) -> Result[str | None]:
    """`crate::review(course, text, false)` with its error returned, not
    raised: `wait_user` counts the time as the user's even when it fails."""
    from coursecrafter import cli

    try:
        return Ok(cli.review(course, text, False))
    except Error as e:
        return Err(e.msg)


def _parse_ok(text: str) -> curriculum.Plan | None:
    """`curriculum::parse(text).ok()`."""
    try:
        return curriculum.parse(text)
    except curriculum.ParseError:
        return None


def converse(
    log: session.Session,
    course: Path,
    llm_: llm.Llm,
    intake: str,
    interactive: bool,
    current: curriculum.Plan | None,
    mode: Mode,
) -> bool:
    """The planning loop shared by `new`, `revise` and `convert`. With `current`
    (a revision), its ids are the baseline of the change list. Only `new` gets
    the separate self-review: a revision request includes it, and a
    conversion must keep the author's plan rather than redesign it."""
    from coursecrafter import cli, linux

    system = (
        f"{prompts.GUIDED_SYSTEM}\n\n{prompts.PLAN_FORMAT}\n## Example\n\n{prompts.PLAN_EXAMPLE}"
    )
    messages = [llm.Msg.system(system), llm.Msg.user(intake)]
    asked = 0
    fixes = 0
    bad_questions = False
    reviewed = mode != Mode.NEW
    # The self-review's list of changes, kept until a draft is shown: the
    # reviewed draft may still need validator fixes first.
    review_notes: str | None = None
    previous: set[str] | None = ids(current) if current is not None else None
    purpose = "first reply"
    while True:
        print("thinking...", file=sys.stderr)
        reply = log.chat(llm_, purpose, messages)
        q = llm.block(reply, "questions")
        if q is not None and llm.block(reply, "curriculum") is None:
            messages.append(llm.Msg.assistant(reply))
            if asked >= MAX_QUESTIONS:
                log.log("question limit reached: asking for the curriculum")
                purpose = "draft"
                messages.append(
                    llm.Msg.user("Use your recommended options and write the curriculum now.")
                )
                continue
            try:
                qs = parse_questions(q)
            except Error as e:
                if not bad_questions:
                    bad_questions = True
                    log.log(f"questions not in the expected JSON ({e.msg}): asking again")
                    purpose = "format fix"
                    messages.append(
                        llm.Msg.user(
                            f"The <questions> block is not valid JSON in the required shape ({e.msg}). "
                            'Send it again as a JSON array of {"question", "options", "recommended"}.'
                        )
                    )
                    continue
                log.log(f"questions still malformed ({e.msg}): asking for the curriculum")
                purpose = "draft"
                messages.append(
                    llm.Msg.user("Use your recommended options and write the curriculum now.")
                )
                continue
            qs = qs[: MAX_QUESTIONS - asked]
            if interactive and asked == 0:
                print(
                    "\nA few questions first. Enter keeps the recommended option; type a letter, or your own answer."
                )
            answers = log.wait_user(functools.partial(ask_all, qs, interactive, asked))
            for question, a in zip(qs, answers, strict=False):
                log.log(f"question: {trim(question.question)} -> {a}")
            asked += len(qs)
            log.questions = asked
            purpose = "after answers"
            messages.append(llm.Msg.user(answers_message(qs, answers)))
            continue
        changes = llm.block(reply, "changes")
        if changes is not None and trim(changes):
            review_notes = trim(changes)
        text, fixed = curriculum.autofix(cli.unwrap_curriculum(reply))
        log.drafts += 1
        log.autofixes += len(fixed)
        for f in fixed:
            log.log(f"autofix: {f}")
        # The planner continues from the corrected file, so later edits use the new ids.
        if fixed:
            reply = f"<curriculum>\n{text}\n</curriculum>"
        problems = curriculum.problems_of(text)
        source = mode.name()
        curriculum.log_validation(course, source, fixed, problems)
        parsed = _parse_ok(text)
        units = len(parsed.units()) if parsed is not None else 0
        errors = sum(1 for p in problems if not p.warning)
        log.log(
            f"draft {log.drafts}: {units} unit(s), {errors} error(s), {len(problems) - errors} warning(s)"
        )
        if any(not p.warning for p in problems):
            if fixes >= MAX_AUTOFIX:
                _write_draft(course, text)
                print(curriculum.report(problems))
                raise Error(
                    "the planner could not produce a valid curriculum; the last draft is in curriculum.md"
                )
            fixes += 1
            log.rejections += 1
            rejected = [p for p in problems if not p.warning]
            log.log(
                "draft rejected by the validator, asking the planner to fix:\n"
                f"{trim_end(curriculum.report(rejected))}"
            )
            purpose = "validator fix"
            messages.append(llm.Msg.assistant(reply))
            messages.append(llm.Msg.user(curriculum.paste_block(problems)))
            continue
        fixes = 0
        if not reviewed:
            # One self-review per conversation, on the first valid draft.
            reviewed = True
            log.log("asking the planner to review its draft against the checklist")
            purpose = "self-review"
            messages.append(llm.Msg.assistant(reply))
            messages.append(llm.Msg.user(prompts.review_request()))
            continue
        if review_notes is not None:
            notes, review_notes = review_notes, None
            heading = (
                "What the conversion added or changed" if mode == Mode.CONVERT else "Planner review"
            )
            print(f"\n{heading}:\n{notes}")
        parsed = _parse_ok(text)
        if previous is not None and parsed is not None:
            after = ids(parsed)
            added = sorted(after - previous)
            removed = sorted(previous - after)
            if added or removed:
                print(f"\nChanges: added {list_(added)}; removed {list_(removed)}")
        if not interactive:
            ok = cli.review(course, text, True) is not None
            if ok:
                parsed = _parse_ok(text)
                if parsed is not None:
                    linux.offer(parsed, False)
            return ok
        _write_draft(course, text)
        # Already validated above; this prints the environment warnings.
        plan = cli.check_plan(text, False)
        if plan is None:
            raise Error(
                "internal: a validated curriculum failed to load; the draft is in curriculum.md"
            )
        print(f"\n{plan.meta.title}\n{curriculum.table(plan)}")
        previous = ids(plan)
        match log.wait_user(decide):
            case DecisionApprove():
                cli.approve(course, text)
                print(f"Approved: {course / 'curriculum.md'}.")
                linux.offer(plan, True)
                print("Next: coursecrafter build")
                return True
            case DecisionEdit():
                res = log.wait_user(functools.partial(_review, course, text))
                if isinstance(res, Err):
                    raise Error(res.msg)
                approved = res.value
                if approved is not None:
                    parsed = _parse_ok(approved)
                    if parsed is not None:
                        linux.offer(parsed, True)
                return approved is not None
            case DecisionQuit():
                print(
                    f"Draft kept in {course / 'curriculum.md'}. Resume with: coursecrafter import curriculum.md"
                )
                return False
            case DecisionRevise(c):
                log.revisions += 1
                log.log(f"revision asked: {c}")
                purpose = "revision"
                messages.append(llm.Msg.assistant(reply))
                messages.append(
                    llm.Msg.user(
                        f"Revise the curriculum: {c}\n"
                        "Check the result against the review list, then send the complete file again."
                    )
                )
