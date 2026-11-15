"""Comfort around unlocking: a default `.vscode/` in the course folder, a
clear screen for the new unit, and the directory the shell should move to.
A program cannot change its parent shell's directory, so `proctor init`
prints a shell function that wraps proctor: proctor writes the target
folder to the file named by `PROCTOR_CD_FILE`, and the function `cd`s there."""

from __future__ import annotations

import os
import sys
from pathlib import Path

from proctor import compat, course
from proctor.compat import read_text, write_text
from proctor.errors import Error
from proctor.unit import Unit


def _asset(name: str) -> str:
    """A file of `assets/vscode/`, shipped with the package (Rust embeds them
    with `include_str!`)."""
    return read_text(Path(__file__).parent / "assets" / "vscode" / name)


def install_vscode(c: course.CourseDir) -> list[str]:
    """Writes `.vscode/tasks.json` and the keybindings example into the course
    folder when they are missing; never overwrites. Returns what it created."""
    dir = c.root / ".vscode"
    created: list[str] = []
    for name in ["tasks.json", "keybindings.example.json"]:
        path = dir / name
        if path.exists():
            continue
        try:
            dir.mkdir(parents=True, exist_ok=True)
            write_text(path, _asset(name))
        except OSError:
            continue
        created.append(f".vscode/{name}")
    return created


def clear_screen() -> None:
    """Clears the terminal before a new unit is shown (`PROCTOR_NO_CLEAR` keeps it)."""
    if compat.is_terminal(sys.stdout) and os.environ.get("PROCTOR_NO_CLEAR") is None:
        try:
            sys.stdout.write("\x1b[2J\x1b[3J\x1b[H")
            sys.stdout.flush()
        except OSError:
            pass


def request_cd(c: course.CourseDir, u: Unit) -> None:
    """Tells the shell function where to go: the unit's workspace, or the course
    folder when the student creates the workspace themselves."""
    file = os.environ.get("PROCTOR_CD_FILE")
    if file is None:
        return
    ws = c.workspace(u)
    target = ws if ws.is_dir() else c.root
    try:
        target = course._canonicalize(target)
    except OSError:
        pass
    target = course.strip_verbatim(target)
    try:
        write_text(file, str(target).replace("\\", "/"))
    except OSError:
        pass


def shell_init(shell: str) -> str:
    """The shell function for `proctor init <shell>`."""
    match shell:
        case "pwsh" | "powershell":
            return PWSH
        case "bash" | "zsh" | "sh":
            return POSIX
        case "fish":
            return FISH
        case other:
            raise Error(f"no shell function for `{other}` (pwsh, bash, zsh, fish)")


PWSH = r"""# proctor: move to the new unit's folder after `proctor next` or an unlock.
function proctor {
    $exe = Get-Command proctor -CommandType Application -ErrorAction SilentlyContinue | Select-Object -First 1
    if (-not $exe) { Write-Error 'proctor is not installed (uv tool install --editable ./proctor)'; return }
    $f = [System.IO.Path]::GetTempFileName()
    $env:PROCTOR_CD_FILE = $f
    try { & $exe.Source @args } finally { Remove-Item Env:PROCTOR_CD_FILE -ErrorAction SilentlyContinue }
    $code = $LASTEXITCODE
    $dir = Get-Content -Raw $f -ErrorAction SilentlyContinue
    Remove-Item $f -ErrorAction SilentlyContinue
    if ($dir) { Set-Location $dir.Trim() }
    $global:LASTEXITCODE = $code
}
"""

POSIX = r"""# proctor: move to the new unit's folder after `proctor next` or an unlock.
proctor() {
    local f code
    f="$(mktemp)" || { command proctor "$@"; return; }
    PROCTOR_CD_FILE="$f" command proctor "$@"
    code=$?
    if [ -s "$f" ]; then cd "$(cat "$f")" || true; fi
    rm -f "$f"
    return $code
}
"""

FISH = r"""# proctor: move to the new unit's folder after `proctor next` or an unlock.
function proctor
    set -l f (mktemp)
    env PROCTOR_CD_FILE=$f proctor $argv
    set -l code $status
    if test -s $f
        cd (cat $f)
    end
    rm -f $f
    return $code
end
"""
