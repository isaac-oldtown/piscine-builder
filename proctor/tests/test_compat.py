from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

from proctor import compat


def test_the_tools_leave_their_own_venv(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    venv = tmp_path / ".venv"
    scripts = venv / ("Scripts" if sys.platform == "win32" else "bin")
    other = tmp_path / "bin"
    monkeypatch.setattr(sys, "prefix", str(venv))
    monkeypatch.setattr(sys, "base_prefix", str(tmp_path / "python"))
    monkeypatch.setenv("PATH", os.pathsep.join([str(scripts), str(other), f"{scripts}{os.sep}"]))
    monkeypatch.setenv("VIRTUAL_ENV", str(venv))
    compat.leave_own_venv()
    assert os.environ["PATH"] == str(other)
    assert "VIRTUAL_ENV" not in os.environ

    # Outside a venv nothing changes.
    monkeypatch.setattr(sys, "base_prefix", str(venv))
    monkeypatch.setenv("PATH", str(scripts))
    compat.leave_own_venv()
    assert os.environ["PATH"] == str(scripts)
