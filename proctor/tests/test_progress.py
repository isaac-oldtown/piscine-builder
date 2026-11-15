from __future__ import annotations

import re

from common import demo_copy

from proctor import course, journal, progress, render


def cells(row: str) -> list[str]:
    return re.split(r"(?<!\\)\|", row.strip())[1:-1]


def test_check_names_never_break_progress_tables() -> None:
    assert render.code_span("a`b") == "``a`b``"
    assert render.code_span("`x`") == "`` `x` ``"
    with demo_copy("progress") as s:
        c = course.CourseDir.load(s.path)
        u = c.units[0].id
        t = journal.now() - 3600
        journal.append(s.path, journal.Event(t=t, ev="unlock", unit=u))
        page = progress.render(c, journal.read(s.path))
        assert "| Planned work done | 0 h of " in page and "(0%)" in page, page
        item = 'case split("a|b", "`|`") == ["a", "b"]'
        for k in range(3):
            e = journal.Event(t=t + 60 * k, ev="check", unit=u, ok=False, cat="cases", item=item)
            journal.append(s.path, e)
        page = progress.render(c, journal.read(s.path))
        rows = [x for x in page.splitlines() if x.startswith(f"| `{u}` |")]
        assert rows and all(len(cells(x)) == 9 for x in rows), rows
        assert '``case split("a\\|b", "`\\|`") == ["a", "b"]`` (3x)' in rows[0], rows[0]
        recent = [x for x in page.splitlines() if f"| `{u}` | KO" in x]
        assert len(recent) == 3 and all(len(cells(x)) == 4 for x in recent), recent
        assert 'Most failed checks: ``case split("a|b", "`|`") == ["a", "b"]`` (3x).' in page
