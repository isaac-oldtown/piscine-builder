from __future__ import annotations

from typing import Any

import pytest
from coursecrafter.llm import (
    ChatError,
    Failure,
    Usage,
    block,
    files,
    is_loopback,
    parse_yaml,
    reply_text,
    status_failure,
    yaml_block,
)
from proctor.errors import Error

from coursecrafter import llm
from proctor import yamlio


def test_usage_counts_cache_reads_and_writes_once() -> None:
    # Meridian, first and second call with the same prompt.
    w = Usage.parse(
        {
            "prompt_tokens": 4658,
            "completion_tokens": 4,
            "prompt_tokens_details": {"cached_tokens": 0, "cache_write_tokens": 4656},
        }
    )
    assert (w.input, w.cache_write, w.fresh()) == (4658, 4656, 2)
    r = Usage.parse(
        {
            "prompt_tokens": 4658,
            "completion_tokens": 4,
            "prompt_tokens_details": {"cached_tokens": 4656, "cache_write_tokens": 0},
        }
    )
    assert (r.input, r.cache_read, r.output, r.fresh()) == (4658, 4656, 4, 2)
    # Anthropic's native shape: input_tokens exclude the cache.
    a = Usage.parse(
        {
            "input_tokens": 2,
            "output_tokens": 4,
            "cache_read_input_tokens": 4656,
            "cache_creation_input_tokens": 0,
        }
    )
    assert a == r
    assert Usage.parse(None) == Usage()


def test_yaml_block_ignores_extra_blocks() -> None:
    r = '<unit>\n```yaml\ngoal: x\n```\n\n<tests>\ncases: |\n  f() == 1\n</tests>\n<file path="a">b</file>\n</unit>'
    assert yaml_block(r, "unit") == "goal: x"
    r = "<unit>\ngoal: x\n\n<tests>\ncases: y\n</tests>\n</unit>"
    assert yaml_block(r, "unit") == "goal: x\n"


def test_repair_requotes_broken_scalars_only() -> None:
    bad = 'functions:\n  - name: f\n    returns: "ok " followed by the port\nrequirements:\n  - `f` never panics\n  - "fine"\ncases: |\n  f("a") == "b"\n  "x" ok\n'
    with pytest.raises(Error):
        yamlio.load(bad)
    v = parse_yaml(bad)
    assert v["functions"][0]["returns"] == '"ok " followed by the port'
    assert v["requirements"][0] == "`f` never panics"
    assert v["requirements"][1] == "fine"
    assert v["cases"] == 'f("a") == "b"\n"x" ok\n'


def test_blocks_and_files() -> None:
    r = "<unit>\n```yaml\ngoal: x\n```\n</unit>\n<file path=\"src/a.c\">\nint a;\n</file>\n<file path='bad'>x</file>"
    assert block(r, "unit") == "goal: x"
    assert files(r, "file") == [("src/a.c", "int a;\n")]
    assert is_loopback("http://127.0.0.1:3456/v1")


def test_only_unavailable_providers_are_worth_a_later_run() -> None:
    assert status_failure(429) == Failure.UNAVAILABLE
    assert status_failure(529) == Failure.UNAVAILABLE
    assert status_failure(401) == Failure.SETUP
    assert status_failure(404) == Failure.SETUP
    assert status_failure(400) == Failure.REQUEST
    assert status_failure(413) == Failure.REQUEST


def _unwrap_err(f: Any) -> ChatError:
    with pytest.raises(ChatError) as e:
        f()
    return e.value


def test_a_cut_off_reply_is_not_a_reply() -> None:
    def reply(finish: str, content: Any) -> str:
        v = {"choices": [{"finish_reason": finish, "message": {"content": content}}]}
        return reply_text(v, "")

    assert reply("stop", "<unit>") == "<unit>"
    assert reply("stop", [{"text": "a"}, {"text": "b"}]) == "ab"
    cut = _unwrap_err(lambda: reply("length", "<unit>\ngoal: half"))
    assert cut.kind == Failure.REQUEST
    assert "cut off" in cut.msg
    assert _unwrap_err(lambda: reply("stop", " ")).kind == Failure.REQUEST
    v = {"stop_reason": "max_tokens", "choices": [{"message": {"content": "x"}}]}
    assert _unwrap_err(lambda: reply_text(v, "")).kind == Failure.REQUEST
    shape = _unwrap_err(lambda: reply_text({"error": "overloaded"}, "{}"))
    assert shape.kind == Failure.UNAVAILABLE


def test_chat_error_is_an_error() -> None:
    # `?` turns a ChatError into its message (`From<ChatError> for String`).
    e = ChatError(Failure.SETUP, "HTTP 401: no")
    assert isinstance(e, Error)
    assert str(e) == "HTTP 401: no"
    assert llm.cut("abcdef", 3) == "abc..."
