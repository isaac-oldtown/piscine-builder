from __future__ import annotations

from coursecrafter.linux import decode, linux_units

from coursecrafter import curriculum


def test_decodes_wsl_utf16_lists() -> None:
    utf16 = "Ubuntu\r\ndocker-desktop\r\n".encode("utf-16-le")
    assert decode(utf16) == "Ubuntu\r\ndocker-desktop\r\n"
    assert decode(b"Ubuntu\n") == "Ubuntu\n"


def test_lists_linux_units_by_project() -> None:
    text = "---\ntitle: T\nlanguage: rust\n---\n\n## d01-a: A\ngoal: g.\nconcept_difficulty: 1\ndifficulty: 1\nhours: 2\nintroduces: [x]\n\n## d02-fork: F\ngoal: g.\nconcept_difficulty: 2\ndifficulty: 2\nhours: 3\nplatform: linux\nassumes: [x]\nintroduces: [y]\n\n## p01-sh: S\ngoal: g.\nminimum: m.\nplatform: linux\nassumes: [y]\n\n### p01.1-a: A\ngoal: g.\nconcept_difficulty: 2\ndifficulty: 2\nhours: 4\nassumes: [y]\n\n### p01.2-b: B\ngoal: g.\nconcept_difficulty: 2\ndifficulty: 2\nhours: 5\nassumes: [y]\n"
    plan = curriculum.parse(text)
    names, hours, required = linux_units(plan)
    assert names == ["d02-fork", "p01-sh (all 2 stages)"]
    assert (hours, required) == (12.0, 14.0)
