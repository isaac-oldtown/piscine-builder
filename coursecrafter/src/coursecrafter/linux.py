"""Units that need Linux APIs on a machine that is not Linux. The planner is
told what this machine offers (Docker, WSL), and once a curriculum has
`platform: linux` units, the student picks a way to run them and
coursecrafter installs or starts it, after asking."""

from __future__ import annotations

import sys
import time
from dataclasses import dataclass
from pathlib import Path

from proctor.command import Command, Stdio
from proctor.compat import from_utf8_lossy, lines, trim
from proctor.errors import Error

from coursecrafter import curriculum
from proctor import ask, docker

DOCKER_DESKTOP_EXE = r"C:\Program Files\Docker\Docker\Docker Desktop.exe"
WSL_DISTRO = "Ubuntu-24.04"


@dataclass(kw_only=True)
class Access:
    native: bool
    docker: docker.Status
    # WSL can be used (Windows only).
    wsl: bool
    # Installed WSL distributions a student can work in (Docker's own excluded).
    distros: list[str]
    winget: bool
    brew: bool

    def docker_text(self) -> str:
        match self.docker:
            case docker.Status.READY:
                return "Docker Desktop is installed and running with Linux containers"
            case docker.Status.STOPPED:
                return "Docker Desktop is installed but not running"
            case docker.Status.WINDOWS_CONTAINERS:
                return "Docker is set to Windows containers"
            case docker.Status.MISSING:
                return "Docker is not installed"

    def planning_block(self) -> str:
        """For the planner: what the machine offers and how to plan with it."""
        if self.native:
            return "# Linux\nThis machine runs Linux: units using Linux APIs run natively. Still mark them `platform: linux`.\n"
        if not self.wsl:
            wsl = ""
        elif not self.distros:
            wsl = " WSL is available, with no Linux distribution installed."
        else:
            wsl = f" WSL distributions installed: {', '.join(self.distros)}."
        return (
            f"# Linux\nThis machine is not Linux: Linux-only APIs (fork, exec, pipes, signals, process groups, /proc, Unix permissions, the nix crate) do not work natively. {self.docker_text()}.{wsl}\n"
            "Plan with that in mind:\n"
            "- Prefer portable units and projects. Use Linux APIs only where the student's goal needs them, and mark those units (or the whole project) `platform: linux`.\n"
            "- A `platform: linux` unit is built and tested in an Ubuntu container that proctor starts by itself with Docker, so the student can stay on this machine. coursecrafter offers to install or start Docker, or WSL, once the curriculum is approved.\n"
            "- When the student's goal could be met either way (for example a shell, process management, a server with signal handling), ask ONE question about it: a portable variant, or the Linux variant run through a container, and say which you recommend and why. Do not ask when the goal clearly needs Linux, or clearly does not.\n"
        )


def decode(data: bytes) -> str:
    """`wsl.exe` writes UTF-16 on Windows."""
    if len(data) >= 2 and all(b == 0 for b in data[1::2]):
        even = data[: len(data) // 2 * 2]
        return even.decode("utf-16-le", errors="replace")
    return from_utf8_lossy(data)


def wsl_distros() -> list[str]:
    try:
        o = Command("wsl").args(["--list", "--quiet"]).stdin(Stdio.null()).output()
    except OSError:
        return []
    if not o.status.success():
        return []
    out = []
    for line in lines(decode(o.stdout)):
        name = trim(line).strip("\0")
        if name and not name.startswith("docker-desktop"):
            out.append(name)
    return out


def exists(program: str) -> bool:
    from proctor import lang, run

    return run.exists(lang.resolve(program))


def detect() -> Access:
    native = sys.platform.startswith("linux")
    windows = sys.platform == "win32"
    return Access(
        native=native,
        docker=docker.Status.MISSING if native else docker.status(),
        wsl=windows and exists("wsl"),
        distros=wsl_distros() if windows else [],
        winget=windows and exists("winget"),
        brew=sys.platform == "darwin" and exists("brew"),
    )


def linux_units(plan: curriculum.Plan) -> tuple[list[str], float, float]:
    """Plain-text summary of the Linux units: drills by id, projects with their stage count."""
    names: list[str] = []
    hours = 0.0
    for u in plan.units():
        if plan.platform(u) is None:
            continue
        if not u.f.stretch:
            hours += u.f.hours if u.f.hours is not None else 0.0
        if u.parent is not None:
            p = u.parent
            n = sum(1 for s in plan.stages(p) if plan.platform(s) is not None)
            all_ = len(plan.stages(p))
            if n == all_:
                name = f"{p} (all {n} stages)"
            else:
                name = f"{p} ({n} of {all_} stages)"
        else:
            name = u.id
        if name not in names:
            names.append(name)
    return names, hours, plan.required_hours()


def run_visible(program: str, args: list[str]) -> bool:
    # Rust's stdout is line-buffered: flush before the child writes.
    print(f"$ {program} {' '.join(args)}", flush=True)
    try:
        return Command(program).args(args).status().success()
    except OSError:
        return False


def start_docker() -> bool:
    """Starts Docker Desktop and waits until it answers (about a minute)."""
    if sys.platform == "win32":
        launched = Path(DOCKER_DESKTOP_EXE).is_file()
        if launched:
            try:
                Command(DOCKER_DESKTOP_EXE).spawn()
            except OSError:
                launched = False
    elif sys.platform == "darwin":
        try:
            Command("open").args(["-a", "Docker"]).status()
            launched = True
        except OSError:
            launched = False
    else:
        launched = False
    if not launched:
        print("Could not start Docker Desktop: start it from the Start menu.")
        return False
    print("Starting Docker Desktop", end="")
    deadline = time.monotonic() + 180.0
    while time.monotonic() < deadline:
        if docker.status() == docker.Status.READY:
            print(" ready.")
            return True
        print(".", end="", flush=True)
        time.sleep(3.0)
    print(
        "\nDocker Desktop did not answer within 3 minutes. Check its window, then run `coursecrafter setup` again."
    )
    return False


def install_docker(a: Access) -> bool:
    from coursecrafter import cli

    if a.winget:
        program, args = (
            "winget",
            [
                "install",
                "--exact",
                "--id",
                "Docker.DockerDesktop",
                "--accept-package-agreements",
                "--accept-source-agreements",
            ],
        )
    elif a.brew:
        program, args = "brew", ["install", "--cask", "docker"]
    else:
        print(
            "Install Docker Desktop from https://www.docker.com/products/docker-desktop/, start it, then run `coursecrafter setup` again."
        )
        return False
    restart = " and enable WSL 2, which can need a restart" if sys.platform == "win32" else ""
    print(f"This installs Docker Desktop (about 2 GB, may ask for administrator rights{restart}).")
    if not cli.ask_yes(f"Run `{program} {' '.join(args)}` now?"):
        print("Nothing installed.")
        return False
    if not run_visible(program, args):
        print(
            "The installation did not finish. Fix the problem shown above, or install Docker Desktop by hand, then run `coursecrafter setup` again."
        )
        return False
    print(
        "Docker Desktop is installed. Start it once (accept its terms), open a NEW terminal so `docker` is on the PATH, then run `coursecrafter setup` again to check."
    )
    return True


def docker_path(a: Access) -> bool:
    match a.docker:
        case docker.Status.READY:
            print("Docker is ready.")
            return prebuild_image()
        case docker.Status.STOPPED:
            return start_docker() and prebuild_image()
        case docker.Status.WINDOWS_CONTAINERS:
            print(
                'Docker Desktop runs Windows containers: right-click its tray icon, choose "Switch to Linux containers", then run `coursecrafter setup` again.'
            )
            return False
        case docker.Status.MISSING:
            return install_docker(a)


def prebuild_image() -> bool:
    """Builds proctor's Linux image now rather than at the first check."""
    from coursecrafter import cli

    if not cli.ask_yes(
        "Build proctor's Linux image now (a few minutes, once), instead of at the first Linux unit?"
    ):
        print("It will be built at the first Linux unit.")
        return True
    try:
        tag = docker.image()
    except Error as e:
        print(e.msg)
        return False
    print(f"Linux image ready: {tag}.")
    return True


def wsl_path(a: Access) -> bool:
    from coursecrafter import cli

    ubuntu = next((d for d in a.distros if d.lower().startswith("ubuntu")), None)
    if ubuntu is None:
        print(
            f"This installs {WSL_DISTRO} in WSL (a few minutes; a restart may be needed the first time WSL is enabled)."
        )
        if not cli.ask_yes(f"Run `wsl --install -d {WSL_DISTRO}` now?"):
            print("Nothing installed.")
            return False
        if not run_visible("wsl", ["--install", "-d", WSL_DISTRO]):
            print(
                "The installation did not finish. Fix the problem shown above, then run `coursecrafter setup` again."
            )
            return False
    distro = ubuntu if ubuntu is not None else WSL_DISTRO
    print(
        f"\nWith WSL you follow the whole course inside Linux. In `wsl -d {distro}`:\n"
        "  sudo apt update && sudo apt install -y build-essential curl git\n"
        "  curl -sSf https://sh.rustup.rs | sh -s -- -y --component clippy,rustfmt\n"
        "  curl -LsSf https://astral.sh/uv/install.sh | sh\n"
        "  then clone PiscineBuilder there and, in it, run `uv tool install --editable ./proctor` and `uv tool install --editable ./coursecrafter`.\n"
        "Keep the course in the Linux home folder (~/), not under /mnt/c: builds there are much faster.\n"
        "VS Code opens it with its WSL extension (`code .` inside WSL)."
    )
    return True


def offer(plan: curriculum.Plan, interactive: bool) -> bool:
    """After approval: when the plan has Linux units and this machine is not
    Linux, says which units, what the machine has, and offers to set it up.
    Nothing is installed without a terminal and an explicit yes."""
    names, hours, required = linux_units(plan)
    if not names:
        return True
    a = detect()
    if a.native:
        print(f"\nThis machine runs Linux: the Linux units ({', '.join(names)}) run natively.")
        return True
    print(
        f"\nThese units need Linux (about {curriculum.round5(hours)} h of the {curriculum.round5(required)} h required): {', '.join(names)}."
    )
    if not a.wsl:
        wsl = ""
    elif not a.distros:
        wsl = "; WSL has no Linux distribution"
    else:
        wsl = f"; WSL has {', '.join(a.distros)}"
    print(f"On this machine: {a.docker_text()}{wsl}.")
    if not interactive:
        print("Run `coursecrafter setup` in a terminal to install or start what they need.")
        return a.docker == docker.Status.READY
    match a.docker:
        case docker.Status.READY:
            docker_label = "Docker Desktop: already running; proctor runs the Linux units in a container, you stay on this machine"
        case docker.Status.STOPPED:
            docker_label = "Docker Desktop: start it; proctor runs the Linux units in a container, you stay on this machine"
        case docker.Status.WINDOWS_CONTAINERS:
            docker_label = "Docker Desktop: switch it to Linux containers; proctor then runs the Linux units in a container"
        case docker.Status.MISSING:
            docker_label = "Docker Desktop: install it; proctor runs the Linux units in a container, you stay on this machine"
    options = [docker_label]
    if a.wsl:
        options.append(
            "WSL Ubuntu: follow the whole course inside a Linux system (install the tools there too)"
        )
    options.append(
        "Not now: the Linux units cannot be checked until one is set up (`coursecrafter setup` later)"
    )
    match ask.choose("How do you want to run the Linux units?", options, 0, None):
        case ask.AnswerPick(i):
            pick = i
        case _:
            pick = len(options) - 1
    if pick == 0:
        return docker_path(a)
    elif a.wsl and pick == 1:
        return wsl_path(a)
    else:
        print("Later: `coursecrafter setup` asks again.")
        return False
