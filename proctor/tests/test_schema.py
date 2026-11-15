from __future__ import annotations

from dataclasses import dataclass, field

import pytest
from proctor.errors import Error
from proctor.schema import U8

from proctor import schema, yamlio


@dataclass(kw_only=True)
class Inner:
    x: str | None = None


@dataclass(kw_only=True)
class Doc:
    title: str
    tags: list[str] = field(default_factory=list)
    level: U8 | None = None
    inner: Inner = field(default_factory=Inner)


def err(text: str) -> str:
    with pytest.raises(Error) as e:
        schema.load(Doc, text)
    return str(e.value)


def test_empty_values_read_as_yaml_serde_reads_them() -> None:
    d = schema.load(Doc, "title:\ntags:\ninner:\n")
    assert d.title == ""
    assert d.tags == []
    assert d.inner == Inner()
    assert schema.load(Doc, "title: ~\nlevel: ~\n").title == "~"
    assert schema.load(Doc, "title: 5\n").title == "5"


def test_empty_text_is_an_empty_struct() -> None:
    assert err("") == "missing field `title`"
    assert err("# only a comment\n") == "missing field `title` at line 2 column 1"


def test_positions_at_the_start_are_left_out() -> None:
    assert err("foo: 1\n").startswith("unknown field `foo`, expected one of")
    assert err("title: a\ntitle: b\n") == "duplicate field `title`"
    assert err("title: a\nlevel: -1\n") == (
        "level: invalid type: integer `-1`, expected u8 at line 2 column 8"
    )
    assert err("title: a\nlevel: 300\n") == (
        "level: invalid value: integer `300`, expected u8 at line 2 column 8"
    )


def test_a_type_error_before_a_parse_error_wins() -> None:
    assert err("title: [a,\n") == (
        "title: invalid type: sequence, expected a string at line 1 column 8"
    )
    assert err("title: a\ntags: [a,\n").startswith("did not find expected")


def test_one_document_only() -> None:
    assert err("title: a\n---\ntitle: b\n") == yamlio.MORE_THAN_ONE
    with pytest.raises(Error, match="more than one document"):
        yamlio.load("a: 1\n---\nb: 2\n")
    assert yamlio.load("") is None
