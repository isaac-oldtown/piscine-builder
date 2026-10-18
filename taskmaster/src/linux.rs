//! Units that need Linux APIs on a machine that is not Linux. The planner is
//! told what this machine offers (Docker, WSL), and once a curriculum has
//! `platform: linux` units, the student picks a way to run them and
//! taskmaster installs or starts it, after asking.

use crate::curriculum::Plan;
use skillcheck::ask::{self, Answer};
use skillcheck::docker::{self, Status};
use std::path::Path;
use std::process::{Command, Stdio};
use std::time::{Duration, Instant};

const DOCKER_DESKTOP_EXE: &str = r"C:\Program Files\Docker\Docker\Docker Desktop.exe";
const WSL_DISTRO: &str = "Ubuntu-24.04";

pub struct Access {
    pub native: bool,
    pub docker: Status,
    /// WSL can be used (Windows only).
    pub wsl: bool,
    /// Installed WSL distributions a student can work in (Docker's own excluded).
    pub distros: Vec<String>,
    pub winget: bool,
    pub brew: bool,
}

/// `wsl.exe` writes UTF-16 on Windows.
fn decode(bytes: &[u8]) -> String {
    if bytes.len() >= 2 && bytes.iter().skip(1).step_by(2).all(|b| *b == 0) {
        let units: Vec<u16> = bytes
            .as_chunks::<2>()
            .0
            .iter()
            .map(|c| u16::from_le_bytes(*c))
            .collect();
        String::from_utf16_lossy(&units)
    } else {
        String::from_utf8_lossy(bytes).into_owned()
    }
}

fn wsl_distros() -> Vec<String> {
    let Ok(o) = Command::new("wsl")
        .args(["--list", "--quiet"])
        .stdin(Stdio::null())
        .output()
    else {
        return Vec::new();
    };
    if !o.status.success() {
        return Vec::new();
    }
    decode(&o.stdout)
        .lines()
        .map(|l| l.trim().trim_matches('\0').to_string())
        .filter(|l| !l.is_empty() && !l.starts_with("docker-desktop"))
        .collect()
}

fn exists(program: &str) -> bool {
    skillcheck::run::exists(&skillcheck::lang::resolve(program))
}

pub fn detect() -> Access {
    let native = cfg!(target_os = "linux");
    Access {
        native,
        docker: if native {
            Status::Missing
        } else {
            docker::status()
        },
        wsl: cfg!(windows) && exists("wsl"),
        distros: if cfg!(windows) {
            wsl_distros()
        } else {
            Vec::new()
        },
        winget: cfg!(windows) && exists("winget"),
        brew: cfg!(target_os = "macos") && exists("brew"),
    }
}

impl Access {
    fn docker_text(&self) -> &'static str {
        match self.docker {
            Status::Ready => "Docker Desktop is installed and running with Linux containers",
            Status::Stopped => "Docker Desktop is installed but not running",
            Status::WindowsContainers => "Docker is set to Windows containers",
            Status::Missing => "Docker is not installed",
        }
    }

    /// For the planner: what the machine offers and how to plan with it.
    pub fn planning_block(&self) -> String {
        if self.native {
            return "# Linux\nThis machine runs Linux: units using Linux APIs run natively. Still mark them `platform: linux`.\n".into();
        }
        let wsl = if !self.wsl {
            String::new()
        } else if self.distros.is_empty() {
            " WSL is available, with no Linux distribution installed.".into()
        } else {
            format!(" WSL distributions installed: {}.", self.distros.join(", "))
        };
        format!(
            "# Linux\nThis machine is not Linux: Linux-only APIs (fork, exec, pipes, signals, process groups, /proc, Unix permissions, the nix crate) do not work natively. {}.{wsl}\n\
             Plan with that in mind:\n\
             - Prefer portable units and projects. Use Linux APIs only where the student's goal needs them, and mark those units (or the whole project) `platform: linux`.\n\
             - A `platform: linux` unit is built and tested in an Ubuntu container that skillcheck starts by itself with Docker, so the student can stay on this machine. taskmaster offers to install or start Docker, or WSL, once the curriculum is approved.\n\
             - When the student's goal could be met either way (for example a shell, process management, a server with signal handling), ask ONE question about it: a portable variant, or the Linux variant run through a container, and say which you recommend and why. Do not ask when the goal clearly needs Linux, or clearly does not.\n",
            self.docker_text()
        )
    }
}

/// Plain-text summary of the Linux units: drills by id, projects with their stage count.
fn linux_units(plan: &Plan) -> (Vec<String>, f64, f64) {
    let mut names: Vec<String> = Vec::new();
    let mut hours = 0.0;
    for u in plan.units() {
        if plan.platform(u).is_none() {
            continue;
        }
        if !u.f.stretch {
            hours += u.f.hours.unwrap_or(0.0);
        }
        let name = match &u.parent {
            Some(p) => {
                let n = plan
                    .stages(p)
                    .iter()
                    .filter(|s| plan.platform(s).is_some())
                    .count();
                let all = plan.stages(p).len();
                if n == all {
                    format!("{p} (all {n} stages)")
                } else {
                    format!("{p} ({n} of {all} stages)")
                }
            }
            None => u.id.clone(),
        };
        if !names.contains(&name) {
            names.push(name);
        }
    }
    (names, hours, plan.required_hours())
}

fn run_visible(program: &str, args: &[&str]) -> bool {
    println!("$ {program} {}", args.join(" "));
    Command::new(program)
        .args(args)
        .status()
        .is_ok_and(|s| s.success())
}

/// Starts Docker Desktop and waits until it answers (about a minute).
fn start_docker() -> bool {
    let launched = if cfg!(windows) {
        Path::new(DOCKER_DESKTOP_EXE).is_file() && Command::new(DOCKER_DESKTOP_EXE).spawn().is_ok()
    } else if cfg!(target_os = "macos") {
        Command::new("open").args(["-a", "Docker"]).status().is_ok()
    } else {
        false
    };
    if !launched {
        println!("Could not start Docker Desktop: start it from the Start menu.");
        return false;
    }
    print!("Starting Docker Desktop");
    let deadline = Instant::now() + Duration::from_secs(180);
    while Instant::now() < deadline {
        if docker::status() == Status::Ready {
            println!(" ready.");
            return true;
        }
        print!(".");
        let _ = std::io::Write::flush(&mut std::io::stdout());
        std::thread::sleep(Duration::from_secs(3));
    }
    println!(
        "\nDocker Desktop did not answer within 3 minutes. Check its window, then run `taskmaster setup` again."
    );
    false
}

fn install_docker(a: &Access) -> bool {
    let (program, args): (&str, Vec<&str>) = if a.winget {
        (
            "winget",
            vec![
                "install",
                "--exact",
                "--id",
                "Docker.DockerDesktop",
                "--accept-package-agreements",
                "--accept-source-agreements",
            ],
        )
    } else if a.brew {
        ("brew", vec!["install", "--cask", "docker"])
    } else {
        println!(
            "Install Docker Desktop from https://www.docker.com/products/docker-desktop/, start it, then run `taskmaster setup` again."
        );
        return false;
    };
    println!(
        "This installs Docker Desktop (about 2 GB, may ask for administrator rights{}).",
        if cfg!(windows) {
            " and enable WSL 2, which can need a restart"
        } else {
            ""
        }
    );
    if !crate::ask_yes(&format!("Run `{program} {}` now?", args.join(" "))) {
        println!("Nothing installed.");
        return false;
    }
    if !run_visible(program, &args) {
        println!(
            "The installation did not finish. Fix the problem shown above, or install Docker Desktop by hand, then run `taskmaster setup` again."
        );
        return false;
    }
    println!(
        "Docker Desktop is installed. Start it once (accept its terms), open a NEW terminal so `docker` is on the PATH, then run `taskmaster setup` again to check."
    );
    true
}

fn docker_path(a: &Access) -> bool {
    match a.docker {
        Status::Ready => {
            println!("Docker is ready.");
            prebuild_image()
        }
        Status::Stopped => start_docker() && prebuild_image(),
        Status::WindowsContainers => {
            println!(
                "Docker Desktop runs Windows containers: right-click its tray icon, choose \"Switch to Linux containers\", then run `taskmaster setup` again."
            );
            false
        }
        Status::Missing => install_docker(a),
    }
}

/// Builds skillcheck's Linux image now rather than at the first check.
fn prebuild_image() -> bool {
    if !crate::ask_yes(
        "Build skillcheck's Linux image now (a few minutes, once), instead of at the first Linux unit?",
    ) {
        println!("It will be built at the first Linux unit.");
        return true;
    }
    match docker::image() {
        Ok(tag) => {
            println!("Linux image ready: {tag}.");
            true
        }
        Err(e) => {
            println!("{e}");
            false
        }
    }
}

fn wsl_path(a: &Access) -> bool {
    let ubuntu = a
        .distros
        .iter()
        .find(|d| d.to_lowercase().starts_with("ubuntu"));
    if ubuntu.is_none() {
        println!(
            "This installs {WSL_DISTRO} in WSL (a few minutes; a restart may be needed the first time WSL is enabled)."
        );
        if !crate::ask_yes(&format!("Run `wsl --install -d {WSL_DISTRO}` now?")) {
            println!("Nothing installed.");
            return false;
        }
        if !run_visible("wsl", &["--install", "-d", WSL_DISTRO]) {
            println!(
                "The installation did not finish. Fix the problem shown above, then run `taskmaster setup` again."
            );
            return false;
        }
    }
    let distro = ubuntu.map_or(WSL_DISTRO, String::as_str);
    println!(
        "\nWith WSL you follow the whole course inside Linux. In `wsl -d {distro}`:\n\
         \x20 sudo apt update && sudo apt install -y build-essential curl git\n\
         \x20 curl -sSf https://sh.rustup.rs | sh -s -- -y --component clippy,rustfmt\n\
         \x20 then clone levelup there and run `cargo install --path skillcheck` and `cargo install --path taskmaster`.\n\
         Keep the course in the Linux home folder (~/), not under /mnt/c: builds there are much faster.\n\
         VS Code opens it with its WSL extension (`code .` inside WSL)."
    );
    true
}

/// After approval: when the plan has Linux units and this machine is not
/// Linux, says which units, what the machine has, and offers to set it up.
/// Nothing is installed without a terminal and an explicit yes.
pub fn offer(plan: &Plan, interactive: bool) -> bool {
    let (names, hours, required) = linux_units(plan);
    if names.is_empty() {
        return true;
    }
    let a = detect();
    if a.native {
        println!(
            "\nThis machine runs Linux: the Linux units ({}) run natively.",
            names.join(", ")
        );
        return true;
    }
    println!(
        "\nThese units need Linux (about {} h of the {} h required): {}.",
        crate::curriculum::round5(hours),
        crate::curriculum::round5(required),
        names.join(", ")
    );
    let wsl = if !a.wsl {
        String::new()
    } else if a.distros.is_empty() {
        "; WSL has no Linux distribution".into()
    } else {
        format!("; WSL has {}", a.distros.join(", "))
    };
    println!("On this machine: {}{wsl}.", a.docker_text());
    if !interactive {
        println!("Run `taskmaster setup` in a terminal to install or start what they need.");
        return a.docker == Status::Ready;
    }
    let docker_label = match a.docker {
        Status::Ready => {
            "Docker Desktop: already running; skillcheck runs the Linux units in a container, you stay on this machine"
        }
        Status::Stopped => {
            "Docker Desktop: start it; skillcheck runs the Linux units in a container, you stay on this machine"
        }
        Status::WindowsContainers => {
            "Docker Desktop: switch it to Linux containers; skillcheck then runs the Linux units in a container"
        }
        Status::Missing => {
            "Docker Desktop: install it; skillcheck runs the Linux units in a container, you stay on this machine"
        }
    };
    let mut options = vec![docker_label.to_string()];
    if a.wsl {
        options.push(
            "WSL Ubuntu: follow the whole course inside a Linux system (install the tools there too)"
                .to_string(),
        );
    }
    options.push(
        "Not now: the Linux units cannot be checked until one is set up (`taskmaster setup` later)"
            .to_string(),
    );
    let pick = match ask::choose("How do you want to run the Linux units?", &options, 0, None) {
        Answer::Pick(i) => i,
        Answer::Other(_) => options.len() - 1,
    };
    if pick == 0 {
        docker_path(&a)
    } else if a.wsl && pick == 1 {
        wsl_path(&a)
    } else {
        println!("Later: `taskmaster setup` asks again.");
        false
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn decodes_wsl_utf16_lists() {
        let utf16: Vec<u8> = "Ubuntu\r\ndocker-desktop\r\n"
            .encode_utf16()
            .flat_map(u16::to_le_bytes)
            .collect();
        assert_eq!(decode(&utf16), "Ubuntu\r\ndocker-desktop\r\n");
        assert_eq!(decode(b"Ubuntu\n"), "Ubuntu\n");
    }

    #[test]
    fn lists_linux_units_by_project() {
        let text = "---\ntitle: T\nlanguage: rust\n---\n\n## d01-a: A\ngoal: g.\nconcept_difficulty: 1\ndifficulty: 1\nhours: 2\nintroduces: [x]\n\n## d02-fork: F\ngoal: g.\nconcept_difficulty: 2\ndifficulty: 2\nhours: 3\nplatform: linux\nassumes: [x]\nintroduces: [y]\n\n## p01-sh: S\ngoal: g.\nminimum: m.\nplatform: linux\nassumes: [y]\n\n### p01.1-a: A\ngoal: g.\nconcept_difficulty: 2\ndifficulty: 2\nhours: 4\nassumes: [y]\n\n### p01.2-b: B\ngoal: g.\nconcept_difficulty: 2\ndifficulty: 2\nhours: 5\nassumes: [y]\n";
        let plan = crate::curriculum::parse(text).unwrap();
        let (names, hours, required) = linux_units(&plan);
        assert_eq!(names, vec!["d02-fork", "p01-sh (all 2 stages)"]);
        assert_eq!((hours, required), (12.0, 14.0));
    }
}
