from __future__ import annotations

from pathlib import Path

from proctor.journal import Event, date, duration, week_start

from proctor import journal


def test_calendar() -> None:
    assert date(0) == "1970-01-01"
    assert date(1_792_195_200) == "2026-10-17"
    # 2026-10-17 is a Saturday; its week starts Monday 2026-10-12.
    assert date(week_start(1_792_195_200) * 86_400) == "2026-10-12"
    assert duration(5400) == "1 h 30"


def test_lines_match_serde(tmp_path: Path) -> None:
    (tmp_path / ".proctor").mkdir()
    journal.append(tmp_path, Event(t=5, ev="unlock", unit="u1"))
    journal.append(
        tmp_path,
        Event(t=6, ev="check", unit="u1", ok=False, cat="tests", item='a "b"\n', seed="00ff"),
    )
    text = (tmp_path / ".proctor" / "journal.jsonl").read_bytes().decode()
    assert text == (
        '{"t":5,"ev":"unlock","unit":"u1"}\n'
        '{"t":6,"ev":"check","unit":"u1","ok":false,"cat":"tests","item":"a \\"b\\"\\n","seed":"00ff"}\n'
    )
    events = journal.read(tmp_path)
    assert [e.ev for e in events] == ["unlock", "check"]
    assert events[1].item == 'a "b"\n'


def test_read_skips_what_serde_rejects(tmp_path: Path) -> None:
    (tmp_path / ".proctor").mkdir()
    (tmp_path / ".proctor" / "journal.jsonl").write_bytes(
        b'{"t":1,"ev":"pass","unit":"a","extra":[1,{"x":NaN}]}\n'
        b'{"t":1,"ev":"pass","unit":"b","extra":{"x":1}}\n'
        b'{"t":1.0,"ev":"pass","unit":"c"}\n'
        b'{"t":1,"ev":"pass"}\n'
        b'{"t":1,"t":2,"ev":"pass","unit":"d"}\n'
        b'{"t":1,"ev":"pass","unit":"e","ok":null,"passed":-1}\n'
        b"not json\n"
        b'[2,"pass","f"]\n'
    )
    assert [e.unit for e in journal.read(tmp_path)] == ["b", "f"]
