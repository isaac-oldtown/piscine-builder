//! Comfort around unlocking: a default `.vscode/` in the course folder, a
//! clear screen for the new unit, and the directory the shell should move to.
//! A program cannot change its parent shell's directory, so `proctor init`
//! prints a shell function that wraps proctor: proctor writes the target
//! folder to the file named by `PROCTOR_CD_FILE`, and the function `cd`s there.

use crate::course::{CourseDir, strip_verbatim};
use crate::unit::Unit;
use std::fs;
use std::io::{IsTerminal, Write};

const TASKS: &str = include_str!("../assets/vscode/tasks.json");
const KEYS: &str = include_str!("../assets/vscode/keybindings.example.json");

/// Writes `.vscode/tasks.json` and the keybindings example into the course
/// folder when they are missing; never overwrites. Returns what it created.
pub fn install_vscode(c: &CourseDir) -> Vec<String> {
    let dir = c.root.join(".vscode");
    let mut created = Vec::new();
    for (name, body) in [("tasks.json", TASKS), ("keybindings.example.json", KEYS)] {
        let path = dir.join(name);
        if path.exists() {
            continue;
        }
        if fs::create_dir_all(&dir).is_ok() && fs::write(&path, body).is_ok() {
            created.push(format!(".vscode/{name}"));
        }
    }
    created
}

/// Clears the terminal before a new unit is shown (`PROCTOR_NO_CLEAR` keeps it).
pub fn clear_screen() {
    let mut out = std::io::stdout();
    if out.is_terminal() && std::env::var_os("PROCTOR_NO_CLEAR").is_none() {
        let _ = out.write_all(b"\x1b[2J\x1b[3J\x1b[H");
        let _ = out.flush();
    }
}

/// Tells the shell function where to go: the unit's workspace, or the course
/// folder when the student creates the workspace themselves.
pub fn request_cd(c: &CourseDir, u: &Unit) {
    let Some(file) = std::env::var_os("PROCTOR_CD_FILE") else {
        return;
    };
    let ws = c.workspace(u);
    let target = if ws.is_dir() { ws } else { c.root.clone() };
    let target = strip_verbatim(&target.canonicalize().unwrap_or(target));
    let _ = fs::write(file, target.to_string_lossy().replace('\\', "/"));
}

/// The shell function for `proctor init <shell>`.
pub fn shell_init(shell: &str) -> Result<&'static str, String> {
    Ok(match shell {
        "pwsh" | "powershell" => PWSH,
        "bash" | "zsh" | "sh" => POSIX,
        "fish" => FISH,
        other => {
            return Err(format!(
                "no shell function for `{other}` (pwsh, bash, zsh, fish)"
            ));
        }
    })
}

const PWSH: &str = r#"# proctor: move to the new unit's folder after `proctor next` or an unlock.
function proctor {
    $exe = Get-Command proctor -CommandType Application -ErrorAction SilentlyContinue | Select-Object -First 1
    if (-not $exe) { Write-Error 'proctor is not installed (cargo install --path proctor)'; return }
    $f = [System.IO.Path]::GetTempFileName()
    $env:PROCTOR_CD_FILE = $f
    try { & $exe.Source @args } finally { Remove-Item Env:PROCTOR_CD_FILE -ErrorAction SilentlyContinue }
    $code = $LASTEXITCODE
    $dir = Get-Content -Raw $f -ErrorAction SilentlyContinue
    Remove-Item $f -ErrorAction SilentlyContinue
    if ($dir) { Set-Location $dir.Trim() }
    $global:LASTEXITCODE = $code
}
"#;

const POSIX: &str = r#"# proctor: move to the new unit's folder after `proctor next` or an unlock.
proctor() {
    local f code
    f="$(mktemp)" || { command proctor "$@"; return; }
    PROCTOR_CD_FILE="$f" command proctor "$@"
    code=$?
    if [ -s "$f" ]; then cd "$(cat "$f")" || true; fi
    rm -f "$f"
    return $code
}
"#;

const FISH: &str = r#"# proctor: move to the new unit's folder after `proctor next` or an unlock.
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
"#;
