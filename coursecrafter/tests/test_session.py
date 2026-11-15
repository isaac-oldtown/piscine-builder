from __future__ import annotations

from coursecrafter.session import civil


def test_civil_dates() -> None:
    assert civil(0) == ("1970-01-01", "00:00:00")
    assert civil(1_792_345_022) == ("2026-10-18", "17:37:02")
    assert civil(951_782_400)[0] == "2000-02-29"
