"""Chat client. Speaks Anthropic's native `/messages` API to Anthropic, marking
the prompt for its cache, and the OpenAI-compatible `/chat/completions` to
OpenAI, Meridian (which fronts Claude Code's own login and caches on its own)
and any other server."""

from __future__ import annotations

import enum
import http.client
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Any

from proctor.compat import (
    as_millis,
    byte_len,
    io_error,
    lines,
    regex,
    trim,
    trim_end,
    trim_end_matches,
    trim_start,
)
from proctor.errors import Error

from coursecrafter import config
from proctor import yamlio

DEFAULT_MODEL = "claude-sonnet-5-5"

# Most tokens a reply may use; a reply that reaches it is cut off.
MAX_TOKENS = 16000

# The whole request, retries excluded (ureq's `timeout_global`).
TIMEOUT = 900
# How long a local provider gets to answer `/health` before the first call.
WAKE_SECONDS = 60

ANTHROPIC_VERSION = "2023-06-01"

# Marks the end of a prompt prefix the provider keeps for five minutes.
EPHEMERAL = {"type": "ephemeral"}


class Api(enum.Enum):
    """The request format a provider speaks."""

    # OpenAI's `/chat/completions`.
    CHAT = "chat"
    # Anthropic's `/messages`, where the request says what to cache.
    MESSAGES = "messages"


class Failure(enum.Enum):
    """Why a `chat` failed, which decides what a long run does next."""

    # No usable answer (network, rate limit, quota, server error): the same
    # request may work later.
    UNAVAILABLE = "unavailable"
    # The provider refused the setup (key, permission, model, address):
    # every request would fail the same way.
    SETUP = "setup"
    # This request or its reply was unusable (too large, cut off, empty):
    # other requests may still work.
    REQUEST = "request"


class ChatError(Error):
    def __init__(self, kind: Failure, msg: str) -> None:
        super().__init__(msg)
        self.kind = kind

    @staticmethod
    def new(kind: Failure, msg: str) -> ChatError:
        return ChatError(kind, msg)


def status_failure(status: int) -> Failure:
    """What an HTTP error status says about the run."""
    if status in (408, 429) or status >= 500:
        return Failure.UNAVAILABLE
    if 401 <= status <= 404:
        return Failure.SETUP
    return Failure.REQUEST


def _get(v: Any, key: str) -> Any:
    """`v[key]` of a `serde_json::Value`: `Null` (None) when absent."""
    return v.get(key) if isinstance(v, dict) else None


def _at(v: Any, i: int) -> Any:
    """`v[i]` of a `serde_json::Value`: `Null` (None) when absent."""
    return v[i] if isinstance(v, list) and i < len(v) else None


def _as_str(v: Any) -> str | None:
    return v if isinstance(v, str) else None


def _as_u64(v: Any) -> int | None:
    if isinstance(v, int) and not isinstance(v, bool) and 0 <= v < 1 << 64:
        return v
    return None


def reply_text(v: Any, raw: str) -> str:
    """The text of a completion, or why there is none."""
    choice = _at(_get(v, "choices"), 0)
    stop = _as_str(_get(choice, "finish_reason"))
    if stop is None:
        stop = _as_str(_get(v, "stop_reason"))
    if stop in ("length", "max_tokens"):
        raise ChatError(
            Failure.REQUEST,
            f"the reply was cut off at the {MAX_TOKENS}-token limit",
        )

    def empty() -> ChatError:
        return ChatError(Failure.REQUEST, "empty model response")

    if choice is None:
        # Anthropic's native reply: a list of content blocks.
        content = _get(v, "content")
    else:
        content = _get(_get(choice, "message"), "content")
    if isinstance(content, str):
        if trim(content):
            return content
        raise empty()
    if isinstance(content, list):
        s = "".join(t for p in content if (t := _as_str(_get(p, "text"))) is not None)
        if not trim(s):
            raise empty()
        return s
    raise ChatError(
        Failure.UNAVAILABLE,
        f"unexpected response shape: {cut(raw, 300)}",
    )


@dataclass(kw_only=True)
class Msg:
    role: str
    content: str

    @staticmethod
    def system(c: str) -> Msg:
        return Msg(role="system", content=c)

    @staticmethod
    def user(c: str) -> Msg:
        return Msg(role="user", content=c)

    @staticmethod
    def assistant(c: str) -> Msg:
        return Msg(role="assistant", content=c)


@dataclass(kw_only=True)
class Usage:
    """Tokens of one reply. `input` counts every prompt token, cached or not."""

    input: int = 0
    output: int = 0
    cache_read: int = 0
    cache_write: int = 0

    @staticmethod
    def parse(u: Any) -> Usage:
        """Reads the usage object of OpenAI-style replies (Meridian, OpenAI,
        Anthropic's compatibility endpoint) and of Anthropic's native one.
        OpenAI-style `prompt_tokens` already include cache reads and writes;
        Anthropic's `input_tokens` exclude them."""

        def n(v: Any) -> int:
            x = _as_u64(v)
            return 0 if x is None else x

        d = _get(u, "prompt_tokens_details")
        cache_read = max(n(_get(d, "cached_tokens")), n(_get(u, "cache_read_input_tokens")))
        cache_write = max(
            n(_get(d, "cache_write_tokens")),
            n(_get(d, "cache_creation_tokens")),
            n(_get(u, "cache_creation_input_tokens")),
        )
        if _as_u64(_get(u, "prompt_tokens")) is not None:
            input = n(_get(u, "prompt_tokens"))
        else:
            input = n(_get(u, "input_tokens")) + cache_read + cache_write
        return Usage(
            input=input,
            output=max(n(_get(u, "completion_tokens")), n(_get(u, "output_tokens"))),
            cache_read=cache_read,
            cache_write=cache_write,
        )

    def fresh(self) -> int:
        """Input tokens neither read from nor written to the cache."""
        return max(0, self.input - (self.cache_read + self.cache_write))


@dataclass(kw_only=True)
class CallRecord:
    """What one `chat` cost: wall time (retries and back-off included), HTTP
    attempts, the errors met on the way, and the tokens of the reply."""

    ms: int = 0
    attempts: int = 0
    errors: list[str] = field(default_factory=list)
    ok: bool = False
    usage: Usage = field(default_factory=Usage)


@dataclass(kw_only=True)
class Preset:
    base_url: str
    key_envs: list[str]
    default_model: str | None
    keyless_ok: bool
    api: Api = Api.CHAT


def preset(name: str) -> Preset:
    match name:
        case "anthropic":
            return Preset(
                base_url="https://api.anthropic.com/v1",
                key_envs=["ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN"],
                default_model=DEFAULT_MODEL,
                keyless_ok=False,
                api=Api.MESSAGES,
            )
        case "meridian":
            # Meridian drops cache marks and lets Claude Code cache, so the
            # simpler format serves as well.
            return Preset(
                base_url="http://127.0.0.1:3456/v1",
                key_envs=["MERIDIAN_API_KEY"],
                default_model=DEFAULT_MODEL,
                keyless_ok=True,
            )
        case "openai":
            return Preset(
                base_url="https://api.openai.com/v1",
                key_envs=["OPENAI_API_KEY"],
                default_model=None,
                keyless_ok=False,
            )
        case "custom":
            return Preset(
                base_url="",
                key_envs=["OPENAI_API_KEY"],
                default_model=None,
                keyless_ok=False,
            )
        case other:
            raise Error(f"unknown provider `{other}` (anthropic | meridian | openai)")


def is_loopback(url: str) -> bool:
    parts = url.split("://")
    host = parts[1] if len(parts) > 1 else url
    return any(host.startswith(h) for h in ["127.0.0.1", "localhost", "[::1]"])


def env_get(k: str) -> str | None:
    v = os.environ.get(k)
    return v if v is not None and trim(v) else None


@dataclass(kw_only=True)
class Choice:
    """What the command line asked for; `None` falls back to env, then config."""

    provider: str | None = None
    base_url: str | None = None
    model: str | None = None
    key_env: str | None = None


def _first(*options: str | None) -> str | None:
    """`a.or(b).or(c)...` of `Option<String>`s (an empty string counts)."""
    return next((o for o in options if o is not None), None)


@dataclass(kw_only=True)
class Llm:
    base_url: str
    model: str
    key: str | None
    api: Api = Api.CHAT
    # The key goes in `Authorization: Bearer`; Anthropic API keys go in
    # `x-api-key` instead (its auth tokens still use Bearer).
    bearer: bool = True
    woken: bool = False
    calls: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cached_tokens: int = 0
    # Input tokens written to the provider's prompt cache (billed above the
    # normal input price by Anthropic).
    cache_write_tokens: int = 0
    # One record per `chat`, in order, failed ones included.
    history: list[CallRecord] = field(default_factory=list)

    @staticmethod
    def resolve(c: Choice, cfg: config.Config) -> Llm:
        base_override = _first(c.base_url, env_get("PISCINEBUILDER_BASE_URL"), cfg.base_url)
        provider = _first(c.provider, env_get("PISCINEBUILDER_PROVIDER"), cfg.provider)
        if provider is None:
            provider = "custom" if base_override is not None else "anthropic"
        p = preset(provider)
        base_url = trim_end_matches(base_override if base_override is not None else p.base_url, "/")
        if not base_url:
            raise Error(
                "provider `custom` needs --base-url (or PISCINEBUILDER_BASE_URL, or base_url in the config)"
            )
        model = _first(c.model, env_get("PISCINEBUILDER_MODEL"), cfg.model, p.default_model)
        if model is None:
            raise Error(f"provider `{provider}` has no default model; pass --model")
        key_env = _first(c.key_env, cfg.api_key_env)
        key: str | None
        if key_env is not None:
            key = env_get(key_env)
            if key is None:
                raise Error(f"API key variable `{key_env}` is not set")
        else:
            key_env = next(
                (k for k in ["PISCINEBUILDER_API_KEY", *p.key_envs] if env_get(k) is not None),
                None,
            )
            key = None if key_env is None else env_get(key_env)
        if key is None and not (p.keyless_ok or is_loopback(base_url)):
            raise Error(
                f"no API key for {base_url}: set PISCINEBUILDER_API_KEY (or {' / '.join(p.key_envs)}), or name a variable with --api-key-env"
            )
        return Llm(
            base_url=base_url,
            model=model,
            key=key,
            api=p.api,
            bearer=p.api == Api.CHAT or key_env == "ANTHROPIC_AUTH_TOKEN",
        )

    def chat(self, messages: list[Msg]) -> str:
        """One non-streaming completion; retries transient failures."""
        start = time.monotonic()
        rec = CallRecord()
        err: ChatError | None = None
        res = ""
        try:
            res = self.chat_inner(messages, rec)
        except ChatError as e:
            err = e
        rec.ms = as_millis(time.monotonic() - start)
        rec.ok = err is None
        if err is not None and (not rec.errors or rec.errors[-1] != err.msg):
            rec.errors.append(err.msg)
        self.history.append(rec)
        if err is not None:
            raise err
        return res

    def request(self, messages: list[Msg]) -> tuple[str, dict[str, Any]]:
        """The endpoint and JSON body of a call."""
        if self.api == Api.CHAT:
            return f"{self.base_url}/chat/completions", {
                "model": self.model,
                "stream": False,
                "max_tokens": MAX_TOKENS,
                "messages": [{"role": m.role, "content": m.content} for m in messages],
            }
        # Two cache marks: the system prompt, shared by every call of a run,
        # and the end of the conversation, which a retry repeats before adding
        # the reply and the feedback.
        body: dict[str, Any] = {"model": self.model, "max_tokens": MAX_TOKENS}
        system = "\n\n".join(m.content for m in messages if m.role == "system")
        if system:
            body["system"] = [{"type": "text", "text": system, "cache_control": EPHEMERAL}]
        turns = [m for m in messages if m.role != "system"]
        body["messages"] = []
        for i, m in enumerate(turns):
            part: dict[str, Any] = {"type": "text", "text": m.content}
            if i == len(turns) - 1:
                part["cache_control"] = EPHEMERAL
            body["messages"].append({"role": m.role, "content": [part]})
        return f"{self.base_url}/messages", body

    def headers(self) -> dict[str, str]:
        headers = {"content-type": "application/json"}
        if self.api == Api.MESSAGES:
            headers["anthropic-version"] = ANTHROPIC_VERSION
        if self.key is not None:
            if self.bearer:
                headers["authorization"] = f"Bearer {self.key}"
            else:
                headers["x-api-key"] = self.key
        return headers

    def wake(self) -> None:
        """Ping a local provider's `/health` until it answers, once per run.

        A local proxy (Meridian) that has sat idle for hours can refuse the
        first request; asking for its health first wakes it. Giving up is
        harmless: the call then goes ahead and fails (or not) as usual.
        """
        if self.woken or not is_loopback(self.base_url):
            return
        self.woken = True
        parts = urllib.parse.urlsplit(self.base_url)
        url = f"{parts.scheme}://{parts.netloc}/health"
        deadline = time.monotonic() + WAKE_SECONDS
        while True:
            try:
                with urllib.request.urlopen(url, timeout=10):
                    return
            except urllib.error.HTTPError:
                return
            except (urllib.error.URLError, http.client.HTTPException, OSError, ValueError):
                if time.monotonic() >= deadline:
                    return
                time.sleep(2)

    def chat_inner(self, messages: list[Msg], rec: CallRecord) -> str:
        self.wake()
        url, v = self.request(messages)
        # serde_json's `json!` objects keep their keys sorted, compact.
        body = json.dumps(v, separators=(",", ":"), ensure_ascii=False, sort_keys=True).encode(
            "utf-8"
        )
        headers = self.headers()
        last = ""
        for attempt in range(4):
            if attempt > 0:
                rec.errors.append(last)
                time.sleep(2 << attempt)
            rec.attempts += 1
            req = urllib.request.Request(url, data=body, headers=headers, method="POST")
            resp: Any
            try:
                resp = urllib.request.urlopen(req, timeout=TIMEOUT)
            except urllib.error.HTTPError as e:
                # An error status is a normal response (`http_status_as_error(false)`).
                resp = e
            except (urllib.error.URLError, http.client.HTTPException, OSError, ValueError) as e:
                last = f"request to {url} failed: {_send_error(e)}"
                continue
            try:
                with resp:
                    status = resp.status if resp.status is not None else resp.code
                    text = resp.read().decode("utf-8")
            except (http.client.HTTPException, OSError, UnicodeDecodeError) as e:
                raise ChatError(
                    Failure.UNAVAILABLE, f"reading response: {_read_error(e)}"
                ) from None
            if status >= 400:
                kind = status_failure(status)
                if kind == Failure.UNAVAILABLE:
                    last = f"HTTP {status}: {cut(text, 300)}"
                    continue
                raise ChatError(kind, f"HTTP {status}: {cut(text, 500)}")
            return self.extract(text, rec)
        raise ChatError(Failure.UNAVAILABLE, last)

    def extract(self, text: str, rec: CallRecord) -> str:
        try:
            v = json.loads(text, parse_constant=_reject_constant)
        except json.JSONDecodeError as e:
            raise ChatError(
                Failure.UNAVAILABLE,
                f"response is not JSON ({e.msg} at line {e.lineno} column {e.colno}): {cut(text, 200)}",
            ) from None
        # Counted even when the content turns out unusable: the tokens are billed.
        self.calls += 1
        u = Usage.parse(_get(v, "usage"))
        rec.usage = u
        self.prompt_tokens += u.input
        self.completion_tokens += u.output
        self.cached_tokens += u.cache_read
        self.cache_write_tokens += u.cache_write
        return reply_text(v, text)

    def usage_line(self) -> str:
        read, write = self.cached_tokens, self.cache_write_tokens
        if read == 0 and write == 0:
            cache = ""
        elif write == 0:
            cache = f" ({read} cached)"
        elif read == 0:
            cache = f" ({write} written to cache)"
        else:
            cache = f" ({read} cached, {write} written to cache)"
        return f"{self.calls} call(s), {self.prompt_tokens} input tokens{cache}, {self.completion_tokens} output tokens ({self.model})"


def _reject_constant(name: str) -> Any:
    """serde_json has no `NaN` or `Infinity` literals."""
    raise json.JSONDecodeError(f"expected value, found {name}", name, 0)


def _send_error(e: BaseException) -> str:
    """Why a request could not be sent, as ureq words its transport errors."""
    if isinstance(e, urllib.error.URLError):
        reason = e.reason
        if isinstance(reason, OSError):
            return f"io: {io_error(reason)}"
        return str(reason)
    if isinstance(e, TimeoutError):
        return "timeout: global"
    if isinstance(e, OSError):
        return f"io: {io_error(e)}"
    return str(e)


def _read_error(e: BaseException) -> str:
    if isinstance(e, TimeoutError):
        return "timeout: global"
    if isinstance(e, OSError):
        return f"io: {io_error(e)}"
    if isinstance(e, UnicodeDecodeError):
        return "invalid utf-8 in body"
    return str(e)


def cut(s: str, max: int) -> str:
    if len(s) <= max:
        return s
    return f"{s[:max]}..."


def block(text: str, tag: str) -> str | None:
    """Text between `<tag>` and `</tag>` (last closing tag), without code fences."""
    open = f"<{tag}>"
    close = f"</{tag}>"
    i = text.find(open)
    if i < 0:
        return None
    start = i + len(open)
    end = text.rfind(close, start)
    if end < 0:
        return None
    return strip_fences(text[start:end])


def yaml_block(text: str, tag: str) -> str | None:
    """The YAML inside `<tag>...</tag>`. Models sometimes put more inside the
    block than asked (a fenced YAML block followed by other tagged blocks):
    keep the first fenced block if there is one, else stop at the first line
    that opens another tag."""
    body = block(text, tag)
    if body is None:
        return None
    ls = lines(body)
    open = next((i for i, line in enumerate(ls) if trim_start(line).startswith("```")), None)
    if open is not None:
        close = next(
            (
                open + 1 + c
                for c, line in enumerate(ls[open + 1 :])
                if trim_start(line).startswith("```")
            ),
            len(ls),
        )
        return "\n".join(ls[open + 1 : close])

    def opens_tag(line: str) -> bool:
        return len(line) >= 2 and line[0] == "<" and line[1].isascii() and line[1].isalpha()

    end = next((i for i, line in enumerate(ls) if opens_tag(line)), len(ls))
    return "\n".join(ls[:end])


def cleanly_quoted(v: str) -> bool:
    """A cleanly quoted YAML scalar: `"..."` with only escaped inner quotes, or
    `'...'` with only doubled inner quotes."""
    v = trim(v)
    if byte_len(v) < 2:
        return False
    if v.startswith('"') and v[1:].endswith('"'):
        inner = v[1:-1]
        escaped = False
        for c in inner:
            if c == "\\" and not escaped:
                escaped = True
            elif c == '"' and not escaped:
                return False
            else:
                escaped = False
        return not escaped
    if v.startswith("'") and v[1:].endswith("'"):
        inner = v[1:-1]
        return "'" not in inner.replace("''", "")
    return False


def requote(v: str) -> str:
    return '"' + v.replace("\\", "\\\\").replace('"', '\\"') + '"'


def repair_yaml(text: str) -> str:
    """Re-quotes scalars YAML rejects (`returns: "ok " followed by the port`,
    a list item starting with a backtick). Block scalars (`cases: |`) are
    left alone."""
    keyed = regex(r"^(\s*(?:- )?[A-Za-z_][\w]*:[ \t]+)(.+?)\s*$")
    item = regex(r"^(\s*- )(.+?)\s*$")
    out: list[str] = []
    block_indent: int | None = None
    for line in lines(text):
        indent = byte_len(line) - byte_len(trim_start(line))
        if block_indent is not None:
            if not trim(line) or indent > block_indent:
                out.append(line)
                continue
            block_indent = None

        def bad(v: str) -> bool:
            first = v[0] if v else " "
            return first in "\"'`@%" and not cleanly_quoted(v)

        fixed: str | None = None
        c = keyed.search(line)
        if c is not None:
            v = c[2]
            if v.startswith("|") or v.startswith(">"):
                block_indent = indent
            elif bad(v):
                fixed = f"{c[1]}{requote(v)}"
        else:
            c = item.search(line)
            if c is not None and bad(c[2]):
                fixed = f"{c[1]}{requote(c[2])}"
        out.append(fixed if fixed is not None else line)
    s = "\n".join(out)
    if text.endswith("\n"):
        s += "\n"
    return s


def parse_yaml(text: str) -> Any:
    """Parses YAML, retrying once after `repair_yaml`; the first error is kept."""
    try:
        return yamlio.load(text)
    except Error as e:
        first = e.msg
    try:
        return yamlio.load(repair_yaml(text))
    except Error:
        raise Error(first) from None


def files(text: str, tag: str) -> list[tuple[str, str]]:
    """Every `<tag path="...">...</tag>` block, in order."""
    re = regex(f'(?s)<{tag}\\s+path="([^"]+)"\\s*>(.*?)</{tag}>')
    return [(trim(c[1]).replace("\\", "/"), strip_fences(c[2]) + "\n") for c in re.finditer(text)]


def strip_fences(s: str) -> str:
    t = s.strip("\n\r")
    ls = lines(t)
    if len(ls) >= 2 and trim_start(ls[0]).startswith("```") and trim(ls[-1]) == "```":
        return "\n".join(ls[1:-1])
    return trim_end(t)
