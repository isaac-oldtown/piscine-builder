from __future__ import annotations

from pathlib import Path

import pytest
from coursecrafter.guided import MAX_QUESTIONS, answers_message, ask_all, parse_questions
from proctor.compat import read_text
from proctor.errors import Error

from coursecrafter import cli, env, guided, linux, llm, prompts
from proctor import ask, docker


def test_parses_question_arrays() -> None:
    qs = parse_questions(
        """[{"question": "Final project?", "options": ["A shell", "An HTTP server"], "recommended": 1},
                {"question": "Weeks?", "options": ["4", "6"]}]"""
    )
    assert len(qs) == 2
    assert qs[0].recommended == 1
    assert qs[1].recommended == 0


def test_accepts_wrapped_questions_and_clamps() -> None:
    qs = parse_questions(
        """{"questions": [{"question": "Q", "options": ["a", "", "b"], "recommended": 9}]}"""
    )
    assert qs[0].options == ["a", "b"]
    assert qs[0].recommended == 1


def test_caps_the_number_of_questions() -> None:
    one = """{"question": "Q", "options": ["x", "y"]}"""
    body = f"[{','.join([one] * 7)}]"
    assert len(parse_questions(body)) == MAX_QUESTIONS


def test_rejects_prose_and_empty_lists() -> None:
    with pytest.raises(Error):
        parse_questions("1. What language?")
    with pytest.raises(Error):
        parse_questions("[]")


def test_defaults_are_taken_without_a_terminal() -> None:
    qs = parse_questions(
        """[{"question": "Q1", "options": ["x", "y"], "recommended": 1}, {"question": "Q2"}]"""
    )
    a = ask_all(qs, False, 0)
    assert a[0] == "y"
    assert "No preference" in a[1]
    assert "- Q1 -> y" in answers_message(qs, a)


# The messages serde_json gives for these, from the Rust code: the planner
# reads them in the format-fix request.
@pytest.mark.parametrize(
    ("body", "msg"),
    [
        ("1. What language?", "invalid number at line 1 column 3"),
        ("[1,]", "trailing comma at line 1 column 4"),
        ('{"a" 1}', "expected `:` at line 1 column 6"),
        ('"a\\x"', "invalid escape at line 1 column 4"),
        ("1e999", "number out of range at line 1 column 5"),
        ("\n\n[1,\n  2 x]", "expected `,` or `]` at line 2 column 5"),
        ('["é" x]', "expected `,` or `]` at line 1 column 7"),
        (
            '[{"question": "Q", "options": ["a"]}] trailing',
            "trailing characters at line 1 column 39",
        ),
        (
            '[{"question": "Q", "recommended": -1}]',
            "data did not match any variant of untagged enum Shape",
        ),
        ("[" * 130, "recursion limit exceeded at line 1 column 128"),
        ("[]", "no questions"),
    ],
)
def test_errors_are_serde_jsons(body: str, msg: str) -> None:
    with pytest.raises(Error) as e:
        parse_questions(body)
    assert e.value.msg == msg


def test_a_question_may_be_an_array_of_its_fields() -> None:
    qs = parse_questions('[["Q", ["a", "b"], 1]]')
    assert qs == [guided.Question(question="Q", options=["a", "b"], recommended=1)]
    # The wrapped form is only read from an object.
    with pytest.raises(Error):
        parse_questions('[[{"question": "Q"}]]')


def test_mode_names() -> None:
    assert [m.name() for m in guided.Mode] == ["new", "revise", "convert"]


class ScriptedLlm(llm.Llm):
    """Answers from a list, as a provider would."""

    def __init__(self, replies: list[str]) -> None:
        super().__init__(base_url="http://127.0.0.1:1/v1", model="m", key=None)
        self.replies = replies
        self.seen: list[list[llm.Msg]] = []

    def chat(self, messages: list[llm.Msg]) -> str:
        self.seen.append(list(messages))
        self.history.append(llm.CallRecord(ms=1, attempts=1, ok=True))
        return self.replies.pop(0)


def _quiet_machine(monkeypatch: pytest.MonkeyPatch) -> None:
    e = env.Env(os="windows", arch="x86_64", found=[], missing=[], facts=[], declared=None)
    monkeypatch.setattr(env, "detect", lambda declared: e)
    a = linux.Access(
        native=False, docker=docker.Status.MISSING, wsl=False, distros=[], winget=False, brew=False
    )
    monkeypatch.setattr(linux, "detect", lambda: a)


# The menus need a terminal, so this flow cannot be compared with the Rust
# binary: it runs the interactive branches with scripted answers instead.
def test_interactive_flow_revises_then_approves(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _quiet_machine(monkeypatch)
    renamed = prompts.PLAN_EXAMPLE.replace("d02-strings", "d02-text")
    fake = ScriptedLlm(
        [
            '<questions>[{"question": "Q1", "options": ["x", "y"]}, {"question": "Q2"}]</questions>',
            f"<curriculum>\n{prompts.PLAN_EXAMPLE}\n</curriculum>",
            f"<changes>\n- nothing\n</changes>\n<curriculum>\n{prompts.PLAN_EXAMPLE}\n</curriculum>",
            f"<curriculum>\n{renamed}\n</curriculum>",
        ]
    )
    choices: list[ask.Answer] = [ask.AnswerOther("my own")]
    menu: list[ask.Answer] = [ask.AnswerPick(1), ask.AnswerOther("rename d02"), ask.AnswerPick(0)]
    lines = ["", ""]
    monkeypatch.setattr(ask, "choose", lambda *a: choices.pop(0))
    monkeypatch.setattr(ask, "choose_or_type", lambda *a: menu.pop(0))
    monkeypatch.setattr(cli, "read_line", lambda prompt: lines.pop(0))
    assert guided.run(tmp_path, fake, "learn C", True, None, guided.Mode.NEW, False)
    out = capsys.readouterr().out
    assert "A few questions first." in out
    assert "\nPlanner review:\n- nothing\n" in out
    assert "\nChanges: added d02-text; removed d02-strings\n" in out
    assert f"Approved: {tmp_path / 'curriculum.md'}.\nNext: coursecrafter build\n" in out
    assert (
        fake.seen[1][-1].content
        == "My answers:\n- Q1 -> my own\n- Q2 -> No preference, use your judgement.\n"
    )
    assert fake.seen[3][-1].content.startswith("Revise the curriculum: rename d02\n")
    written = read_text(tmp_path / "curriculum.md")
    assert "d02-text" in written
    assert cli.text_hash(written) == read_text(tmp_path / ".proctor" / "coursecrafter" / "approved")
    assert not menu and not choices and not lines


@pytest.mark.parametrize(("answer", "approved"), [("q", False), ("y", True)])
def test_interactive_edit_and_quit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    answer: str,
    approved: bool,
) -> None:
    _quiet_machine(monkeypatch)
    for pick, expected in ((2, approved), (3, False)):
        fake = ScriptedLlm([f"<curriculum>\n{prompts.PLAN_EXAMPLE}\n</curriculum>"])
        monkeypatch.setattr(ask, "choose_or_type", lambda *a, p=pick: ask.AnswerPick(p))
        monkeypatch.setattr(cli, "read_line", lambda prompt: answer)
        assert guided.run(tmp_path, fake, "x", True, None, guided.Mode.CONVERT, False) is expected
    out = capsys.readouterr().out
    assert f"Draft kept in {tmp_path / 'curriculum.md'}. Resume with:" in out
    if not approved:
        assert f"Not approved. The draft is in {tmp_path / 'curriculum.md'}." in out
