"""YAML as the Rust tools read and wrote it (`yaml_serde`, built on libyaml):
the YAML 1.2 core schema, so `yes`, `no`, `on`, `2026-01-02` and `1:30` are
strings, not booleans, dates or numbers; and output in the same style
(insertion order, `key:\\n- item` sequences, `|` blocks for multi-line
text, no line folding)."""

from __future__ import annotations

import io
import re
from dataclasses import dataclass
from typing import Any

import yaml

from proctor.errors import Error

_NULL = "tag:yaml.org,2002:null"

# (tag, pattern, possible first characters; "" stands for the empty scalar)
_CORE_RESOLVERS: list[tuple[str, re.Pattern[str], list[str]]] = [
    ("tag:yaml.org,2002:bool", re.compile(r"^(?:true|True|TRUE|false|False|FALSE)$"), list("tTfF")),
    ("tag:yaml.org,2002:null", re.compile(r"^(?:~|null|Null|NULL|)$"), ["~", "n", "N", ""]),
    (
        "tag:yaml.org,2002:int",
        re.compile(r"^(?:[-+]?[0-9]+|0o[0-7]+|0x[0-9a-fA-F]+)$"),
        list("-+0123456789"),
    ),
    (
        "tag:yaml.org,2002:float",
        re.compile(
            r"^(?:[-+]?(?:\.[0-9]+|[0-9]+(?:\.[0-9]*)?)(?:[eE][-+]?[0-9]+)?"
            r"|[-+]?\.(?:inf|Inf|INF)|\.(?:nan|NaN|NAN))$"
        ),
        list("-+.0123456789"),
    ),
]


class CoreResolver(yaml.resolver.BaseResolver):
    """Plain scalars resolved by the YAML 1.2 core schema."""


for _tag, _regexp, _first in _CORE_RESOLVERS:
    CoreResolver.add_implicit_resolver(_tag, _regexp, _first)


class CoreConstructor(yaml.constructor.SafeConstructor):
    """Builds Python values with the core schema's number rules."""

    def construct_yaml_int(self, node: yaml.Node) -> int:
        text = str(self.construct_scalar(node))  # type: ignore[arg-type]
        if text.startswith("0o"):
            return int(text[2:], 8)
        if text.startswith("0x"):
            return int(text[2:], 16)
        return int(text)

    def construct_yaml_float(self, node: yaml.Node) -> float:
        text = str(self.construct_scalar(node)).lower()  # type: ignore[arg-type]
        if text.endswith(".inf"):
            return float("-inf") if text.startswith("-") else float("inf")
        if text == ".nan":
            return float("nan")
        return float(text)

    def construct_yaml_bool(self, node: yaml.Node) -> bool:
        return str(self.construct_scalar(node)).lower() == "true"  # type: ignore[arg-type]


CoreConstructor.add_constructor("tag:yaml.org,2002:int", CoreConstructor.construct_yaml_int)  # type: ignore[type-var]
CoreConstructor.add_constructor("tag:yaml.org,2002:float", CoreConstructor.construct_yaml_float)  # type: ignore[type-var]
CoreConstructor.add_constructor("tag:yaml.org,2002:bool", CoreConstructor.construct_yaml_bool)  # type: ignore[type-var]


class _PyParser(
    yaml.reader.Reader, yaml.scanner.Scanner, yaml.parser.Parser, yaml.composer.Composer
):
    def __init__(self, stream: str) -> None:
        yaml.reader.Reader.__init__(self, stream)
        yaml.scanner.Scanner.__init__(self)
        yaml.parser.Parser.__init__(self)
        yaml.composer.Composer.__init__(self)


# Reading goes through libyaml itself when PyYAML has it, as yaml_serde does
# (same error messages, faster). Writing uses PyYAML's own emitter: the libyaml
# bundled with PyYAML quotes any text holding a 4-byte character (an emoji),
# where yaml_serde's keeps the `|` block.
_Parser: Any = getattr(yaml.cyaml, "CParser", _PyParser) if yaml.__with_libyaml__ else _PyParser


class _Emitter(yaml.emitter.Emitter, yaml.serializer.Serializer):
    def __init__(self, stream: Any) -> None:
        yaml.emitter.Emitter.__init__(self, stream, width=1 << 30, allow_unicode=True)
        yaml.serializer.Serializer.__init__(self)


class Loader(_Parser, CoreConstructor, CoreResolver):
    def __init__(self, stream: str) -> None:
        _Parser.__init__(self, stream)
        CoreConstructor.__init__(self)
        CoreResolver.__init__(self)


class CoreRepresenter(yaml.representer.SafeRepresenter):
    def represent_str(self, data: str) -> yaml.ScalarNode:
        if "\n" in data:
            return self.represent_scalar("tag:yaml.org,2002:str", data, style="|")
        return self.represent_scalar("tag:yaml.org,2002:str", data)


CoreRepresenter.add_representer(str, CoreRepresenter.represent_str)


class Dumper(_Emitter, CoreRepresenter, CoreResolver):
    def __init__(self, stream: Any) -> None:
        _Emitter.__init__(self, stream)
        CoreRepresenter.__init__(self, sort_keys=False)
        CoreResolver.__init__(self)


MORE_THAN_ONE = "deserializing from YAML containing more than one document is not supported"


def _is_start(mark: Any) -> bool:
    return mark is None or (mark.line == 0 and mark.column == 0)


def at(mark: Any) -> str:
    """` at line L column C`, or nothing at the very start of the text, as
    `yaml_serde` leaves that position out."""
    return "" if _is_start(mark) else f" at line {mark.line + 1} column {mark.column + 1}"


def error_text(e: yaml.YAMLError) -> str:
    """A parser error worded as `yaml_serde` words it:
    `problem at line L column C, context at line L column C`, the context's
    position left out when it is the problem's."""
    if isinstance(e, yaml.MarkedYAMLError):
        pm, cm = e.problem_mark, e.context_mark
        msg = f"{e.problem or 'invalid YAML'}{at(pm)}"
        if e.context:
            msg += f", {e.context}"
            same = (
                pm is not None and cm is not None and (cm.line, cm.column) == (pm.line, pm.column)
            )
            if not same:
                msg += at(cm)
        return msg
    if isinstance(e, yaml.reader.ReaderError):
        return f"{e.reason} at position {e.position}" if e.position else str(e.reason)
    return str(e)


class Cut(yaml.Node):
    """Where a parse error stopped the document. `yaml_serde` reads a document
    as it goes, so the error is only met when reading reaches this node: an
    earlier type error is reported first."""

    id = "cut"

    def __init__(self, text: str) -> None:
        super().__init__("", None, None, None)
        self.text = text


@dataclass
class Document:
    root: yaml.Node
    """An empty plain scalar for an empty stream."""
    error: str | None
    """The parse error, reported once the nodes before it are read."""
    more: bool
    """Another document follows (an error once the first is read)."""


class _Composer:
    """Nodes from libyaml's events, as far as they parse."""

    def __init__(self, loader: Any) -> None:
        self.loader = loader
        self.anchors: dict[str, yaml.Node] = {}
        self.error: str | None = None

    def peek(self) -> Any:
        if self.error is not None:
            return None
        try:
            return self.loader.peek_event()
        except yaml.YAMLError as e:
            self.error = error_text(e)
            return None

    def next(self) -> Any:
        ev = self.peek()
        if ev is not None:
            self.loader.get_event()
        return ev

    def cut(self) -> Cut:
        assert self.error is not None
        return Cut(self.error)

    def node(self) -> yaml.Node:
        ev = self.next()
        if ev is None:
            return self.cut()
        # libyaml's marks are `yaml._yaml.Mark`, which the stubs do not take for nodes.
        start: Any = ev.start_mark
        if isinstance(ev, yaml.AliasEvent):
            found = self.anchors.get(str(ev.anchor))
            if found is None:
                self.error = "unknown anchor" + at(start)
                return self.cut()
            return found
        tag = ev.tag
        node: yaml.Node
        if isinstance(ev, yaml.ScalarEvent):
            if tag is None or tag == "!":
                tag = self.loader.resolve(yaml.ScalarNode, ev.value, ev.implicit)
            end: Any = ev.end_mark
            node = yaml.ScalarNode(tag, ev.value, start, end, style=ev.style)
            if ev.anchor is not None:
                self.anchors[ev.anchor] = node
            return node
        if isinstance(ev, yaml.SequenceStartEvent):
            if tag is None or tag == "!":
                tag = self.loader.resolve(yaml.SequenceNode, None, ev.implicit)
            seq = yaml.SequenceNode(tag, [], start, None, flow_style=ev.flow_style)
            if ev.anchor is not None:
                self.anchors[ev.anchor] = seq
            while True:
                nxt = self.peek()
                if nxt is None:
                    seq.value.append(self.cut())
                    return seq
                if isinstance(nxt, yaml.SequenceEndEvent):
                    self.next()
                    seq.end_mark = nxt.end_mark
                    return seq
                seq.value.append(self.node())
                if self.error is not None:
                    return seq
        assert isinstance(ev, yaml.MappingStartEvent)
        if tag is None or tag == "!":
            tag = self.loader.resolve(yaml.MappingNode, None, ev.implicit)
        m = yaml.MappingNode(tag, [], start, None, flow_style=ev.flow_style)
        if ev.anchor is not None:
            self.anchors[ev.anchor] = m
        while True:
            nxt = self.peek()
            if nxt is None:
                m.value.append((self.cut(), self.cut()))
                return m
            if isinstance(nxt, yaml.MappingEndEvent):
                self.next()
                m.end_mark = nxt.end_mark
                return m
            key = self.node()
            if self.error is not None:
                m.value.append((key, self.cut()))
                return m
            m.value.append((key, self.node()))
            if self.error is not None:
                return m


def parse(text: str) -> Document:
    """The first document of a YAML text, read the way `yaml_serde` reads it."""
    loader = Loader(text)
    try:
        c = _Composer(loader)
        c.next()
        first = c.next()
        if first is None:
            return Document(root=c.cut(), error=c.error, more=False)
        if isinstance(first, yaml.StreamEndEvent):
            mark: Any = first.start_mark
            empty = yaml.ScalarNode(_NULL, "", mark, mark, style="")
            return Document(root=empty, error=None, more=False)
        root = c.node()
        if c.error is None:
            c.next()
        error = c.error
        # A parse error past the first document belongs to the next one.
        more = error is None and not isinstance(c.peek(), yaml.StreamEndEvent)
        return Document(root=root, error=error, more=more)
    finally:
        loader.dispose()


def first_cut(node: yaml.Node) -> Cut | None:
    """The parse error inside `node`, if reading it would meet one."""
    if isinstance(node, Cut):
        return node
    if isinstance(node, yaml.SequenceNode):
        for n in node.value:
            if (c := first_cut(n)) is not None:
                return c
    elif isinstance(node, yaml.MappingNode):
        for k, v in node.value:
            if (c := first_cut(k) or first_cut(v)) is not None:
                return c
    return None


def compose(text: str) -> yaml.Node | None:
    """The node tree of a one-document YAML text (`None` when it is empty)."""
    doc = parse(text)
    if (c := first_cut(doc.root)) is not None:
        raise Error(c.text)
    if doc.error is not None:
        raise Error(doc.error)
    if doc.more:
        raise Error(MORE_THAN_ONE)
    if isinstance(doc.root, yaml.ScalarNode) and doc.root.tag == _NULL and doc.root.value == "":
        return None
    return doc.root


def construct(node: yaml.Node) -> Any:
    """The Python value of a node: dict, list, str, int, float, bool or None."""
    loader = Loader("")
    try:
        return loader.construct_document(node)
    finally:
        loader.dispose()


def load(text: str) -> Any:
    """A YAML text as plain Python values (`None` for an empty document)."""
    node = compose(text)
    return None if node is None else construct(node)


def dump(value: Any) -> str:
    """`yaml_serde::to_string`: block style, keys in insertion order."""
    out = io.StringIO()
    dumper = Dumper(out)
    try:
        dumper.open()
        dumper.represent(value)
        dumper.close()
    finally:
        dumper.dispose()
    text = out.getvalue()
    # PyYAML closes a top-level plain scalar with a `...` line; libyaml does not.
    if not isinstance(value, dict | list) and text.endswith("\n...\n"):
        text = text[: -len("...\n")]
    return text
