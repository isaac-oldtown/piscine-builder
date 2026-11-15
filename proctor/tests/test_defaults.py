from __future__ import annotations

from proctor.unit import LangConfig

from proctor import defaults


def test_presets_parse_and_override() -> None:
    assert "rust" in defaults.known()
    c = defaults.preset("c")
    assert c.flags is not None and len(c.flags) == 4
    over = LangConfig(flags=["-std=c99"], wrap="valgrind")
    m = c.with_(over)
    assert m.flags == ["-std=c99"]
    assert m.extensions is not None and len(m.extensions) == 2
    assert m.wrap == "valgrind"
    rust = defaults.preset("rust").with_(LangConfig(static_checks=[]))
    # An empty list replaces the default.
    assert rust.static_checks is not None and len(rust.static_checks) == 0
    assert defaults.preset("js").imports is not None
