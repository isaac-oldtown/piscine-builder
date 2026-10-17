mod build;
mod config;
mod curriculum;
mod env;
mod guided;
mod llm;
mod prompts;
mod show;

use curriculum::Plan;
use std::io::{BufRead, IsTerminal, Write};
use std::path::{Path, PathBuf};
use std::process::ExitCode;

const USAGE: &str = "\
taskmaster: creates levelup courses. The only part of levelup that uses an LLM.

  taskmaster prompt [--lang L] [--level beginner] [--hours 10]
        print a prompt for any chat assistant; it ends with a curriculum file
  taskmaster import <curriculum.md> [--course DIR] [--yes] [--paste]
        validate a curriculum, show it, and approve it into the course folder
  taskmaster new \"<what you want to learn>\" [--from FILE]... [--course DIR] [--yes]
        plan a curriculum in a short terminal conversation (uses the LLM)
  taskmaster revise \"<changes>\" [--course DIR] [--yes]
        have the planner rewrite curriculum.md with your changes (uses the LLM)
  taskmaster show [curriculum.md] [--course DIR] [--full]
        display the curriculum: units, hours, difficulties, prerequisites, approval
  taskmaster env
        show the environment taskmaster assumes the student has (detected here,
        or declared with `environment:` in the curriculum front matter)
  taskmaster build [--course DIR] [--yes] [--tests | --no-tests] [--only ID]... [--retries N]
        write the unit specs, notes, tests and hidden references (uses the LLM)

LLM options (build, new, revise): --provider anthropic|meridian|openai  --base-url URL  --model NAME
  --api-key-env VAR. Defaults come from LEVELUP_* variables, then the config file,
  then: provider anthropic, model claude-sonnet-5-5. Run `taskmaster config` to see the file path.";

pub fn ask_yes(question: &str) -> bool {
    if !std::io::stdin().is_terminal() {
        return false;
    }
    let a = read_line(&format!("{question} [Y/n] "));
    a.is_empty() || a == "y" || a == "yes"
}

pub fn read_line(prompt: &str) -> String {
    print!("{prompt}");
    let _ = std::io::stdout().flush();
    let mut line = String::new();
    let _ = std::io::stdin().lock().read_line(&mut line);
    line.trim().to_string()
}

pub fn open_editor(path: &Path) -> Result<(), String> {
    let editor = std::env::var("VISUAL")
        .or_else(|_| std::env::var("EDITOR"))
        .unwrap_or_else(|_| {
            if cfg!(windows) {
                "notepad".into()
            } else {
                "vi".into()
            }
        });
    let mut words = skillcheck::run::split_words(&editor);
    if words.is_empty() {
        return Err("no editor".into());
    }
    let prog = words.remove(0);
    std::process::Command::new(prog)
        .args(words)
        .arg(path)
        .status()
        .map_err(|e| format!("cannot start the editor `{editor}`: {e} (set EDITOR)"))?;
    Ok(())
}

struct Args {
    positional: Vec<String>,
    values: Vec<(String, String)>,
    flags: Vec<String>,
}

impl Args {
    fn parse(raw: &[String], valued: &[&str], flags: &[&str]) -> Result<Args, String> {
        let mut a = Args {
            positional: Vec::new(),
            values: Vec::new(),
            flags: Vec::new(),
        };
        let mut it = raw.iter();
        while let Some(x) = it.next() {
            match x.strip_prefix("--") {
                Some(name) if flags.contains(&name) => a.flags.push(name.to_string()),
                Some(name) if valued.contains(&name) => {
                    let v = it.next().ok_or_else(|| format!("--{name} needs a value"))?;
                    a.values.push((name.to_string(), v.clone()));
                }
                Some(name) => return Err(format!("unknown option --{name}")),
                None => a.positional.push(x.clone()),
            }
        }
        Ok(a)
    }
    fn get(&self, k: &str) -> Option<String> {
        self.values
            .iter()
            .rev()
            .find(|(n, _)| n == k)
            .map(|(_, v)| v.clone())
    }
    fn all(&self, k: &str) -> Vec<String> {
        self.values
            .iter()
            .filter(|(n, _)| n == k)
            .map(|(_, v)| v.clone())
            .collect()
    }
    fn has(&self, k: &str) -> bool {
        self.flags.iter().any(|f| f == k)
    }
    fn llm(&self) -> Result<llm::Llm, String> {
        let cfg = config::load()?;
        llm::Llm::resolve(
            llm::Choice {
                provider: self.get("provider"),
                base_url: self.get("base-url"),
                model: self.get("model"),
                key_env: self.get("api-key-env"),
            },
            &cfg,
        )
    }
    fn course(&self) -> PathBuf {
        self.get("course")
            .map(PathBuf::from)
            .unwrap_or_else(|| PathBuf::from("."))
    }
}

const LLM_OPTS: &[&str] = &["provider", "base-url", "model", "api-key-env"];

fn main() -> ExitCode {
    let raw: Vec<String> = std::env::args().skip(1).collect();
    let res = match raw.first().map(String::as_str) {
        None | Some("-h" | "--help" | "help") => {
            println!("{USAGE}");
            Ok(true)
        }
        Some("prompt") => cmd_prompt(&raw[1..]),
        Some("import") => cmd_import(&raw[1..]),
        Some("new") => guided::cmd_new(&raw[1..]),
        Some("revise") => guided::cmd_revise(&raw[1..]),
        Some("show" | "display") => show::cmd_show(&raw[1..]),
        Some("build") => cmd_build(&raw[1..]),
        Some("env") => {
            let declared = std::fs::read_to_string(PathBuf::from("curriculum.md"))
                .ok()
                .and_then(|t| curriculum::parse(&t).ok())
                .and_then(|p| p.meta.environment);
            print!("{}", env::detect(declared).summary());
            Ok(true)
        }
        Some("config") => {
            match config::path() {
                Some(p) => println!(
                    "{}{}",
                    p.display(),
                    if p.is_file() {
                        ""
                    } else {
                        "  (not created yet)"
                    }
                ),
                None => println!("no config directory on this system"),
            }
            println!(
                "keys: provider, base_url, model, api_key_env (the NAME of the variable holding the key)"
            );
            Ok(true)
        }
        Some(other) => Err(format!("unknown command `{other}`\n\n{USAGE}")),
    };
    match res {
        Ok(true) => ExitCode::SUCCESS,
        Ok(false) => ExitCode::from(1),
        Err(e) => {
            eprintln!("error: {e}");
            ExitCode::from(2)
        }
    }
}

fn cmd_prompt(raw: &[String]) -> Result<bool, String> {
    let a = Args::parse(raw, &["lang", "level", "hours"], &[])?;
    println!(
        "{}",
        prompts::browser_prompt(
            a.get("lang").as_deref(),
            a.get("level").as_deref(),
            a.get("hours").as_deref(),
            &env::detect(None).summary()
        )
    );
    Ok(true)
}

/// Extracts the curriculum when it is wrapped in a ``` block or tags.
pub fn unwrap_curriculum(text: &str) -> String {
    if let Some(b) = llm::block(text, "curriculum") {
        return b;
    }
    let t = text.trim();
    if !t.starts_with("---")
        && let Some(start) = t.find("```")
    {
        let body = &t[start..];
        let after = body.find('\n').map(|i| &body[i + 1..]).unwrap_or("");
        if let Some(end) = after.rfind("```") {
            return after[..end].to_string();
        }
    }
    t.to_string()
}

/// Parses and validates; prints problems. `None` when there are errors.
pub fn check_plan(text: &str, paste: bool) -> Option<Plan> {
    let plan = match curriculum::parse(text) {
        Ok(p) => p,
        Err(problems) => {
            print_problems(&problems, paste);
            return None;
        }
    };
    let mut problems = plan.validate();
    problems.extend(plan.preflight(&env::detect(plan.meta.environment.clone())));
    if problems.iter().any(|p| !p.warning) {
        print_problems(&problems, paste);
        return None;
    }
    if !problems.is_empty() {
        print!("{}", curriculum::report(&problems));
    }
    Some(plan)
}

fn print_problems(problems: &[curriculum::Problem], paste: bool) {
    let errors = problems.iter().filter(|p| !p.warning).count();
    println!(
        "The curriculum has {errors} problem(s):\n{}",
        curriculum::report(problems)
    );
    if paste {
        println!(
            "----- paste this back into your chat -----\n{}------------------------------------------",
            curriculum::paste_block(problems)
        );
    } else {
        println!("Use --paste to get a block to paste back into the chat that wrote it.");
    }
}

pub fn approval_path(course: &Path) -> PathBuf {
    course
        .join(".skillcheck")
        .join("taskmaster")
        .join("approved")
}

pub fn text_hash(text: &str) -> String {
    let mut h: u64 = 0xcbf2_9ce4_8422_2325;
    for b in text.replace("\r\n", "\n").bytes() {
        h ^= b as u64;
        h = h.wrapping_mul(0x0100_0000_01b3);
    }
    format!("{h:016x}")
}

pub fn approve(course: &Path, text: &str) -> Result<(), String> {
    let p = approval_path(course);
    if let Some(d) = p.parent() {
        std::fs::create_dir_all(d).map_err(|e| e.to_string())?;
    }
    std::fs::write(&p, text_hash(text)).map_err(|e| e.to_string())
}

/// Shows the plan and asks: approve, edit, or stop. Returns the approved text.
pub fn review(course: &Path, mut text: String, yes: bool) -> Result<Option<String>, String> {
    let file = course.join("curriculum.md");
    loop {
        std::fs::create_dir_all(course).map_err(|e| e.to_string())?;
        std::fs::write(&file, &text).map_err(|e| format!("{}: {e}", file.display()))?;
        let Some(plan) = check_plan(&text, false) else {
            if yes || !std::io::stdin().is_terminal() {
                return Ok(None);
            }
            let a = read_line("[e]dit the file, or [q]uit? ");
            if a.starts_with('e') {
                open_editor(&file)?;
                text = std::fs::read_to_string(&file).map_err(|e| e.to_string())?;
                continue;
            }
            return Ok(None);
        };
        println!("\n{}\n{}", plan.meta.title, curriculum::table(&plan));
        if yes {
            approve(course, &text)?;
            return Ok(Some(text));
        }
        let a = read_line("Approve this curriculum? [Y]es / [e]dit / [q]uit: ").to_lowercase();
        match a.as_str() {
            "" | "y" | "yes" => {
                approve(course, &text)?;
                println!("Approved: {}", file.display());
                return Ok(Some(text));
            }
            x if x.starts_with('e') => {
                open_editor(&file)?;
                text = std::fs::read_to_string(&file).map_err(|e| e.to_string())?;
            }
            _ => {
                println!("Not approved. The draft is in {}.", file.display());
                return Ok(None);
            }
        }
    }
}

fn cmd_import(raw: &[String]) -> Result<bool, String> {
    let a = Args::parse(raw, &["course"], &["yes", "paste"])?;
    let [file] = a.positional.as_slice() else {
        return Err("usage: taskmaster import <curriculum.md> [--course DIR]".into());
    };
    let raw =
        unwrap_curriculum(&std::fs::read_to_string(file).map_err(|e| format!("{file}: {e}"))?);
    let (text, fixed) = curriculum::autofix(&raw);
    for f in &fixed {
        println!("fixed: {f}");
    }
    curriculum::log_validation(
        &a.course(),
        "import",
        &fixed,
        &curriculum::problems_of(&text),
    );
    if a.has("paste") && check_plan(&text, true).is_none() {
        return Ok(false);
    }
    Ok(review(&a.course(), text, a.has("yes"))?.is_some())
}

fn cmd_build(raw: &[String]) -> Result<bool, String> {
    let mut valued = vec!["course", "only", "retries"];
    valued.extend(LLM_OPTS);
    let a = Args::parse(raw, &valued, &["yes", "tests", "no-tests"])?;
    let course = a.course();
    let file = course.join("curriculum.md");
    let text = std::fs::read_to_string(&file).map_err(|_| {
        format!(
            "no curriculum at {}: run `taskmaster import` or `taskmaster new` first",
            file.display()
        )
    })?;
    let approved = std::fs::read_to_string(approval_path(&course)).unwrap_or_default();
    let text = if approved.trim() == text_hash(&text) {
        text
    } else {
        println!("curriculum.md changed since it was approved (or was never approved).");
        match review(&course, text, a.has("yes"))? {
            Some(t) => t,
            None => return Ok(false),
        }
    };
    let Some(plan) = check_plan(&text, false) else {
        return Ok(false);
    };
    let llm = a.llm()?;
    eprintln!("model {} at {}", llm.model, llm.base_url);
    let opts = build::Options {
        retries: a
            .get("retries")
            .map(|r| r.parse().map_err(|_| "--retries takes a number"))
            .transpose()?
            .unwrap_or(3),
        tests: if a.has("tests") {
            Some(true)
        } else if a.has("no-tests") {
            Some(false)
        } else {
            None
        },
        only: a.all("only"),
        yes: a.has("yes"),
    };
    let root = std::fs::canonicalize(&course).map_err(|e| e.to_string())?;
    let root = skillcheck::course::strip_verbatim(&root);
    let environment = env::detect(plan.meta.environment.clone());
    let env_file = root
        .join(".skillcheck")
        .join("taskmaster")
        .join("environment.txt");
    if let Some(d) = env_file.parent() {
        let _ = std::fs::create_dir_all(d);
    }
    let _ = std::fs::write(&env_file, environment.summary());
    let ok =
        build::Builder::new(root.clone(), &plan, &llm, opts, &environment.prompt_block()).run()?;
    println!(
        "\nCourse ready in {}. Students start with: cd {} && skillcheck next",
        root.display(),
        root.display()
    );
    Ok(ok)
}
