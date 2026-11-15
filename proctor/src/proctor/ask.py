"""Multiple-choice questions in the terminal, one at a time:

```text
? What should the final project be?
  a) A Unix shell (recommended)
  b) A multithreaded HTTP server
  c) Something else: type your own answer
Choice [a]:
```

Enter takes the recommended option; a letter picks one; any other text is
taken as a free answer. Without a terminal the recommended option is used."""

from __future__ import annotations

import sys
from dataclasses import dataclass

from proctor.compat import is_terminal, trim


class Answer:
    def text(self, options: list[str]) -> str:
        """The chosen option's label, or the free text."""
        match self:
            case AnswerPick(i):
                return options[i] if 0 <= i < len(options) else ""
            case AnswerOther(s):
                return str(s)
        raise AssertionError(self)


@dataclass(frozen=True)
class AnswerPick(Answer):
    """Index into the options."""

    index: int


@dataclass(frozen=True)
class AnswerOther(Answer):
    """Free text typed instead of a letter."""

    value: str


def interactive() -> bool:
    return is_terminal(sys.stdin)


def letter(i: int) -> str:
    return chr(ord("a") + min(i & 0xFF, 25))


def read_line(prompt: str) -> str | None:
    try:
        print(prompt, end="", flush=True)
    except OSError:
        pass
    try:
        line = sys.stdin.readline()
    except (OSError, ValueError):
        return None
    if line == "":
        return None
    return trim(line)


def choose(question: str, options: list[str], recommended: int, other: str | None) -> Answer:
    """Asks one question. `other` is the label of the free-answer line (for
    example "Something else: type your own answer"), or `None` when only the
    listed options make sense."""
    return ask(question, options, recommended, other, other is not None)


def choose_or_type(question: str, options: list[str], recommended: int) -> Answer:
    """Like `choose` without a free-answer line, but text typed instead of a
    letter is still taken as the answer (for menus where an option already
    asks for text)."""
    return ask(question, options, recommended, None, True)


def ask(
    question: str,
    options: list[str],
    recommended: int,
    other: str | None,
    accept_text: bool,
) -> Answer:
    recommended = min(recommended, max(len(options) - 1, 0))
    if not interactive() or not options:
        return AnswerPick(recommended)
    print(f"\n? {question}")
    for i, o in enumerate(options):
        tag = " (recommended)" if i == recommended else ""
        print(f"  {letter(i)}) {o}{tag}")
    other_letter = letter(len(options))
    if other is not None:
        print(f"  {other_letter}) {other}")
    while True:
        input_ = read_line(f"Choice [{letter(recommended)}]: ")
        if input_ is None:
            return AnswerPick(recommended)
        lower = input_.lower()
        picked: int | None
        if lower == "":
            picked = recommended
        elif len(lower) == 1:
            i = ord(lower) - ord("a")
            picked = i if 0 <= i < len(options) else None
        else:
            picked = None
        if picked is not None:
            print(f"  -> {options[picked]}")
            return AnswerPick(picked)
        if other is not None and lower == other_letter:
            t = read_line("Your answer: ")
            if t is not None and t != "":
                print(f"  -> {t}")
                return AnswerOther(t)
            continue
        if accept_text and len(input_) > 1:
            print(f"  -> {input_}")
            return AnswerOther(input_)
        last = other_letter if other is not None else letter(len(options) - 1)
        text = ", your own text" if accept_text and other is None else ""
        print(f"  Type a letter from a to {last}{text}, or press Enter for the recommended one.")


def yes_no(question: str, yes: str, no: str, default_no: bool) -> bool:
    """A yes/no question as two options, "yes" recommended unless `default_no`."""
    options = [yes, no]
    match choose(question, options, int(default_no), None):
        case AnswerPick(0):
            return True
        case _:
            return False
