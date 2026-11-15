"""Typed YAML loading for dataclasses, with the rules of Rust's serde
derives: an unknown field is an error (`deny_unknown_fields`), a field with
no default must be present, numbers must fit their type, and an untagged
union takes the first alternative that fits. Error messages follow
`yaml_serde`'s (`functions[0].sig: invalid type: map, expected a string at
line 7 column 9`), since they go back to the model and to course authors.

A dataclass field is read from the YAML key of the same name, or from
`field(metadata={"yaml": "fn"})`. Integer widths use the `U8`, `U32`, `U64`
aliases; `typing.Any` keeps the plain YAML value."""

from __future__ import annotations

import dataclasses
import enum
import types
import typing
from typing import Annotated, Any, TypeVar, get_args, get_origin

import yaml

from proctor import yamlio
from proctor.compat import debug_str
from proctor.errors import Error


@dataclasses.dataclass(frozen=True)
class Bounds:
    name: str
    lo: int
    hi: int


@dataclasses.dataclass(frozen=True)
class Untagged:
    """Names an untagged union in error messages: `Annotated[A | B, Untagged("Fuzz")]`."""

    name: str


U8 = Annotated[int, Bounds("u8", 0, (1 << 8) - 1)]
U16 = Annotated[int, Bounds("u16", 0, (1 << 16) - 1)]
U32 = Annotated[int, Bounds("u32", 0, (1 << 32) - 1)]
U64 = Annotated[int, Bounds("u64", 0, (1 << 64) - 1)]
I64 = Annotated[int, Bounds("i64", -(1 << 63), (1 << 63) - 1)]

T = TypeVar("T")

_STR = "tag:yaml.org,2002:str"
_INT = "tag:yaml.org,2002:int"
_FLOAT = "tag:yaml.org,2002:float"
_BOOL = "tag:yaml.org,2002:bool"
_NULL = "tag:yaml.org,2002:null"


class _Fail(Exception):
    def __init__(self, path: str, msg: str, node: yaml.Node | None) -> None:
        super().__init__(msg)
        self.path = path
        self.msg = msg
        self.node = node

    def text(self) -> str:
        at = yamlio.at(self.node.start_mark) if self.node is not None else ""
        prefix = f"{self.path}: " if self.path else ""
        return f"{prefix}{self.msg}{at}"


class _Broken(Exception):
    """Reading reached a parse error: reported as is, even inside an untagged
    union (serde buffers the whole value before trying the alternatives)."""

    def __init__(self, text: str) -> None:
        super().__init__(text)
        self.text = text


def _empty(node: yaml.Node) -> bool:
    """A plain empty scalar (`key:` with no value, or an empty text), which
    `yaml_serde` reads as an empty sequence, map or struct."""
    return isinstance(node, yaml.ScalarNode) and node.value == "" and node.style in (None, "")


def one_of(names: list[str]) -> str:
    """serde's list of expected names: "`a`", "`a` or `b`", "one of `a`, `b`, `c`"."""
    quoted = [f"`{n}`" for n in names]
    if len(quoted) == 1:
        return quoted[0]
    if len(quoted) == 2:
        return f"{quoted[0]} or {quoted[1]}"
    return "one of " + ", ".join(quoted)


def _unexpected(node: yaml.Node) -> str:
    """What a node is, in serde's words (`string "x"`, `integer `5``, `map`)."""
    if isinstance(node, yaml.MappingNode):
        return "map"
    if isinstance(node, yaml.SequenceNode):
        return "sequence"
    tag, value = node.tag, str(node.value)
    if tag == _NULL:
        return "unit value"
    if tag == _BOOL:
        return f"boolean `{value.lower()}`"
    if tag == _INT:
        return f"integer `{value}`"
    if tag == _FLOAT:
        return f"floating point `{value}`"
    return f"string {debug_str(value)}"


def _join(path: str, key: str) -> str:
    return f"{path}.{key}" if path else key


def _int_value(node: yaml.Node) -> int:
    v = yamlio.construct(node)
    assert isinstance(v, int)
    return v


_hints_cache: dict[type, dict[str, Any]] = {}


def _hints(cls: type) -> dict[str, Any]:
    h = _hints_cache.get(cls)
    if h is None:
        h = typing.get_type_hints(cls, include_extras=True)
        _hints_cache[cls] = h
    return h


def _decode(tp: Any, node: yaml.Node, path: str, buffered: bool = False) -> Any:
    """`node` read as `tp`. `buffered` is serde's reading of an untagged
    union's alternatives from its buffered content, where a number or a null
    is no string and an empty scalar no empty list or struct."""
    if isinstance(node, yamlio.Cut):
        raise _Broken(node.text)
    origin = get_origin(tp)
    if origin is Annotated:
        base, *meta = get_args(tp)
        for m in meta:
            if isinstance(m, Bounds):
                return _bounded(m, node, path)
            if isinstance(m, Untagged):
                return _untagged(get_args(base), m.name, node, path)
        return _decode(base, node, path, buffered)
    if tp is Any:
        if (cut := yamlio.first_cut(node)) is not None:
            raise _Broken(cut.text)
        return yamlio.construct(node)
    if origin in (typing.Union, types.UnionType):
        args = get_args(tp)
        if type(None) in args:
            if isinstance(node, yaml.ScalarNode) and node.tag == _NULL:
                return None
            rest = [a for a in args if a is not type(None)]
            inner = rest[0] if len(rest) == 1 else typing.Union[tuple(rest)]  # noqa: UP007
            return _decode(inner, node, path, buffered)
        return _untagged(args, "", node, path)
    if origin is list:
        (item,) = get_args(tp)
        if not buffered and _empty(node):
            return []
        if not isinstance(node, yaml.SequenceNode):
            raise _Fail(path, f"invalid type: {_unexpected(node)}, expected a sequence", node)
        return [_decode(item, n, f"{path}[{i}]", buffered) for i, n in enumerate(node.value)]
    if origin is dict:
        key_tp, val_tp = get_args(tp)
        if not buffered and _empty(node):
            return {}
        if not isinstance(node, yaml.MappingNode):
            raise _Fail(path, f"invalid type: {_unexpected(node)}, expected a map", node)
        out: dict[Any, Any] = {}
        for k, v in node.value:
            key = _decode(key_tp, k, path, buffered)
            out[key] = _decode(val_tp, v, _join(path, str(key)), buffered)
        # Maps of the Rust schema are BTreeMaps: iterated in key order.
        return dict(sorted(out.items()))
    if tp is str:
        # yaml_serde reads any scalar as its text (`5`, `true`, and `~` too).
        if not isinstance(node, yaml.ScalarNode) or (buffered and node.tag != _STR):
            raise _Fail(path, f"invalid type: {_unexpected(node)}, expected a string", node)
        return str(node.value)
    if tp is bool:
        if not isinstance(node, yaml.ScalarNode) or node.tag != _BOOL:
            raise _Fail(path, f"invalid type: {_unexpected(node)}, expected a boolean", node)
        return str(node.value).lower() == "true"
    if tp is int:
        if not isinstance(node, yaml.ScalarNode) or node.tag != _INT:
            raise _Fail(path, f"invalid type: {_unexpected(node)}, expected an integer", node)
        return _int_value(node)
    if tp is float:
        if not isinstance(node, yaml.ScalarNode) or node.tag not in (_INT, _FLOAT):
            raise _Fail(path, f"invalid type: {_unexpected(node)}, expected f64", node)
        return float(yamlio.construct(node))
    if isinstance(tp, type) and issubclass(tp, enum.Enum):
        names = [str(m.value) for m in tp]
        if not isinstance(node, yaml.ScalarNode) or (buffered and node.tag != _STR):
            raise _Fail(
                path,
                f"invalid type: {_unexpected(node)}, expected a YAML tag starting with '!'",
                node,
            )
        for m in tp:
            if str(m.value) == node.value:
                return m
        raise _Fail(path, f"unknown variant `{node.value}`, expected {one_of(names)}", node)
    if isinstance(tp, type) and dataclasses.is_dataclass(tp):
        return _struct(tp, node, path, buffered)
    raise TypeError(f"schema: unsupported type {tp!r}")


def _bounded(m: Bounds, node: yaml.Node, path: str) -> int:
    if not isinstance(node, yaml.ScalarNode) or node.tag != _INT:
        raise _Fail(path, f"invalid type: {_unexpected(node)}, expected {m.name}", node)
    v = _int_value(node)
    if m.lo <= v <= m.hi:
        return v
    # A negative number is no unsigned number at all for yaml_serde; a number
    # that is too big has the right type but not a value the type can hold.
    what = "type" if v < 0 <= m.lo else "value"
    raise _Fail(path, f"invalid {what}: integer `{v}`, expected {m.name}", node)


def _untagged(args: tuple[Any, ...], name: str, node: yaml.Node, path: str) -> Any:
    if (cut := yamlio.first_cut(node)) is not None:
        raise _Broken(cut.text)
    for alt in args:
        try:
            return _decode(alt, node, path, True)
        except _Fail:
            continue
    what = f"untagged enum {name}" if name else "untagged enum"
    raise _Fail(path, f"data did not match any variant of {what}", node)


def _struct(cls: type, node: yaml.Node, path: str, buffered: bool) -> Any:
    pairs: list[tuple[yaml.Node, yaml.Node]]
    if not buffered and _empty(node):
        pairs = []
    elif isinstance(node, yaml.MappingNode):
        pairs = node.value
    else:
        raise _Fail(
            path, f"invalid type: {_unexpected(node)}, expected struct {cls.__name__}", node
        )
    hints = _hints(cls)
    fields = [f for f in dataclasses.fields(cls) if f.init]
    by_key = {f.metadata.get("yaml", f.name): f for f in fields}
    values: dict[str, Any] = {}
    for k, v in pairs:
        if isinstance(k, yamlio.Cut):
            raise _Broken(k.text)
        key = str(k.value) if isinstance(k, yaml.ScalarNode) else "?"
        f = by_key.get(key)
        if f is None:
            raise _Fail(path, f"unknown field `{key}`, expected {one_of(list(by_key))}", k)
        if f.name in values:
            raise _Fail(path, f"duplicate field `{key}`", node)
        values[f.name] = _decode(hints[f.name], v, _join(path, key), buffered)
    for key, f in by_key.items():
        if f.name in values:
            continue
        if f.default is dataclasses.MISSING and f.default_factory is dataclasses.MISSING:
            raise _Fail(path, f"missing field `{key}`", node)
    return cls(**values)


def _read(tp: Any, doc: yamlio.Document) -> Any:
    try:
        value = _decode(tp, doc.root, "")
    except _Fail as e:
        raise Error(e.text()) from None
    except _Broken as e:
        raise Error(e.text) from None
    if doc.error is not None:
        raise Error(doc.error)
    if doc.more:
        raise Error(yamlio.MORE_THAN_ONE)
    return value


def load(tp: type[T], text: str) -> T:
    """A YAML text read as `tp` (`yaml_serde::from_str`)."""
    return typing.cast(T, _read(tp, yamlio.parse(text)))
