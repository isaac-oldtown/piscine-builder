from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import pytest
from coursecrafter.llm import (
    Api,
    ChatError,
    Choice,
    Failure,
    Llm,
    Msg,
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

from coursecrafter import config, llm
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


def test_anthropic_requests_mark_the_prompt_for_caching() -> None:
    messages = [Msg.system("rules"), Msg.user("unit"), Msg.assistant("draft"), Msg.user("fix")]
    url, body = Llm(base_url="https://x/v1", model="m", key="k").request(messages)
    assert url == "https://x/v1/chat/completions"
    assert body["messages"][0] == {"role": "system", "content": "rules"}
    native = Llm(base_url="https://x/v1", model="m", key="k", api=Api.MESSAGES, bearer=False)
    url, body = native.request(messages)
    assert url == "https://x/v1/messages"
    mark = {"type": "ephemeral"}
    assert body["system"] == [{"type": "text", "text": "rules", "cache_control": mark}]
    assert [m["role"] for m in body["messages"]] == ["user", "assistant", "user"]
    assert body["messages"][0]["content"] == [{"type": "text", "text": "unit"}]
    assert body["messages"][2]["content"] == [
        {"type": "text", "text": "fix", "cache_control": mark}
    ]
    assert native.headers()["x-api-key"] == "k"
    assert "authorization" not in native.headers()
    assert native.headers()["anthropic-version"] == llm.ANTHROPIC_VERSION


def test_anthropic_provider_speaks_the_native_api(monkeypatch: pytest.MonkeyPatch) -> None:
    for k in ["PISCINEBUILDER_API_KEY", "PISCINEBUILDER_BASE_URL", "PISCINEBUILDER_PROVIDER"]:
        monkeypatch.delenv(k, raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "t")
    a = Llm.resolve(Choice(provider="anthropic"), config.Config())
    assert (a.api, a.bearer, a.key) == (Api.MESSAGES, True, "t")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "k")
    a = Llm.resolve(Choice(provider="anthropic"), config.Config())
    assert (a.api, a.bearer, a.key) == (Api.MESSAGES, False, "k")
    m = Llm.resolve(Choice(provider="meridian"), config.Config())
    assert (m.api, m.bearer) == (Api.CHAT, True)


def test_native_replies_are_read() -> None:
    v = {
        "content": [{"type": "thinking", "thinking": "hm"}, {"type": "text", "text": "<unit>"}],
        "stop_reason": "end_turn",
    }
    assert reply_text(v, "") == "<unit>"
    v["stop_reason"] = "max_tokens"
    assert _unwrap_err(lambda: reply_text(v, "")).kind == Failure.REQUEST


def test_a_native_call_round_trip() -> None:
    seen: list[tuple[str, dict[str, str], Any]] = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            body = json.loads(self.rfile.read(int(self.headers["content-length"])))
            headers = {k.lower(): v for k, v in self.headers.items()}
            seen.append((self.path, headers, body))
            reply = {
                "content": [{"type": "text", "text": "hello"}],
                "stop_reason": "end_turn",
                "usage": {
                    "input_tokens": 3,
                    "output_tokens": 2,
                    "cache_read_input_tokens": 100,
                    "cache_creation_input_tokens": 20,
                },
            }
            data = json.dumps(reply).encode()
            self.send_response(200)
            self.send_header("content-type", "application/json")
            self.send_header("content-length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, format: str, *args: Any) -> None:
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        port = server.server_address[1]
        c = Llm(
            base_url=f"http://127.0.0.1:{port}/v1",
            model="m",
            key="k",
            api=Api.MESSAGES,
            bearer=False,
        )
        assert c.chat([Msg.system("s"), Msg.user("u")]) == "hello"
    finally:
        server.shutdown()
        server.server_close()
    path, headers, body = seen[0]
    assert path == "/v1/messages"
    assert headers["x-api-key"] == "k"
    assert body["system"][0]["cache_control"] == {"type": "ephemeral"}
    assert (c.prompt_tokens, c.cached_tokens, c.cache_write_tokens) == (123, 100, 20)
    assert (
        c.usage_line()
        == "1 call(s), 123 input tokens (100 cached, 20 written to cache), 2 output tokens (m)"
    )


def test_a_local_provider_is_woken_once_before_the_first_call() -> None:
    paths: list[str] = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            paths.append(self.path)
            self.send_response(200)
            self.send_header("content-length", "2")
            self.end_headers()
            self.wfile.write(b"ok")

        def log_message(self, format: str, *args: Any) -> None:
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        port = server.server_address[1]
        c = Llm(base_url=f"http://127.0.0.1:{port}/v1", model="m", key=None)
        c.wake()
        c.wake()
    finally:
        server.shutdown()
        server.server_close()
    assert paths == ["/health"]


def test_waking_gives_up_quietly_and_skips_remote_providers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(llm, "WAKE_SECONDS", 0)
    dead = Llm(base_url="http://127.0.0.1:9/v1", model="m", key=None)
    dead.wake()
    assert dead.woken
    remote = Llm(base_url="https://api.example.com/v1", model="m", key="k")
    remote.wake()
    assert not remote.woken


def test_chat_error_is_an_error() -> None:
    # `?` turns a ChatError into its message (`From<ChatError> for String`).
    e = ChatError(Failure.SETUP, "HTTP 401: no")
    assert isinstance(e, Error)
    assert str(e) == "HTTP 401: no"
    assert llm.cut("abcdef", 3) == "abc..."
