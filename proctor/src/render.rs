//! Renders instruction files from unit records, in one fixed design. The
//! markdown is never written by an LLM: it is derived from the same record
//! proctor grades against, at the moment the unit is revealed.

use crate::course::CourseDir;
use crate::engine::DEFAULT_ROUNDS;
use crate::unit::{Rule, Target, Unit};

/// The course's display name for a language if it gives one, else the preset name.
pub fn display_language(c: &CourseDir, lang: &str) -> String {
    c.course
        .languages
        .get(lang)
        .and_then(|l| l.name.clone())
        .unwrap_or_else(|| language_name(lang))
}

pub fn language_name(lang: &str) -> String {
    match lang {
        "c" => "C".into(),
        "cpp" | "c++" => "C++".into(),
        "python" => "Python 3".into(),
        "rust" => "Rust".into(),
        "bash" | "sh" | "shell" => "Bash".into(),
        "javascript" | "js" => "JavaScript".into(),
        "typescript" | "ts" => "TypeScript".into(),
        "go" => "Go".into(),
        "java" => "Java".into(),
        "ruby" => "Ruby".into(),
        other => {
            let mut c = other.chars();
            c.next()
                .map(|f| f.to_uppercase().collect::<String>() + c.as_str())
                .unwrap_or_default()
        }
    }
}

fn cell(s: &str) -> String {
    s.trim().replace('|', "\\|").replace('\n', " ")
}

pub fn label(u: &Unit) -> String {
    u.id.split('-').next().unwrap_or(&u.id).to_string()
}

fn header_table(rows: &[(&str, String)]) -> String {
    let mut s = String::from("| Field | Value |\n|-------|-------|\n");
    for (k, v) in rows {
        if !v.is_empty() {
            s.push_str(&format!("| {k} | {} |\n", cell(v)));
        }
    }
    s
}

fn list_or_none(v: &[String]) -> String {
    if v.is_empty() {
        "none".into()
    } else {
        v.join(", ")
    }
}

fn code_list(v: &[String]) -> String {
    v.iter()
        .map(|x| format!("`{x}`"))
        .collect::<Vec<_>>()
        .join(", ")
}

pub fn unit_md(c: &CourseDir, u: &Unit) -> String {
    let lang = c.language(u);
    let chain = c.chain(u);
    let mut s = format!("# {}: {}\n\n", label(u), u.title.trim());
    if let Some((pid, p)) = c.project(u) {
        let k = chain.len();
        let builds_on = match k {
            1 => "First stage.".to_string(),
            2 => format!("Builds on {}.", label(chain[0])),
            _ => format!("Builds on {} to {}.", label(chain[0]), label(chain[k - 2])),
        };
        s.push_str(&format!(
            "Part of project {} ({}). {builds_on} See `{}`.\n\n",
            pid.split('-').next().unwrap_or(pid),
            p.title,
            c.overview_rel(pid).to_string_lossy().replace('\\', "/")
        ));
    }
    s.push_str(&header_table(&[
        ("Language", display_language(c, &lang)),
        (
            "Difficulty",
            match (u.concept_difficulty, u.difficulty) {
                (Some(c), Some(d)) => format!("concepts {c} / 5, implementation {d} / 5"),
                (None, Some(d)) => format!("{d} / 5"),
                _ => String::new(),
            },
        ),
        (
            "Prerequisites",
            if u.is_stage() {
                String::new()
            } else {
                list_or_none(&u.prereqs)
            },
        ),
        ("Concepts", u.concepts.join(", ")),
        (
            "Status",
            if u.stretch {
                "Stretch goal: optional, `proctor skip` moves on without it".to_string()
            } else {
                String::new()
            },
        ),
    ]));
    s.push_str(&format!("\n## Goal\n\n{}\n", u.goal.trim()));
    if let Some(lesson) = u.lesson.as_deref().filter(|l| !l.trim().is_empty()) {
        s.push_str(&format!("\n## Walkthrough\n\n{}\n", lesson.trim()));
    }

    if !u.functions.is_empty() {
        s.push_str("\n## Expected functions (minimal)\n\n| Name | Signature | Returns |\n|------|-----------|---------|\n");
        for f in &u.functions {
            s.push_str(&format!(
                "| `{}` | `{}` | {} |\n",
                cell(&f.name),
                cell(&f.sig),
                cell(&f.returns)
            ));
        }
        s.push_str("\nThis is a minimum: add any helper you need.");
        if chain.len() > 1
            && chain[..chain.len() - 1]
                .iter()
                .any(|x| !x.functions.is_empty())
        {
            s.push_str(" Everything required by the earlier stages stays required.");
        }
        s.push('\n');
    }

    if !u.files.is_empty() {
        s.push_str("\n## Expected files\n\n| Path | Rule | Notes |\n|------|------|-------|\n");
        for f in &u.files {
            let rule = match f.rule {
                Rule::Required => "required",
                Rule::OneOrMore => "one or more",
                Rule::Free => "free",
                Rule::Forbidden => "forbidden",
            };
            s.push_str(&format!(
                "| `{}` | {rule} | {} |\n",
                cell(&f.path),
                cell(&f.note)
            ));
        }
        if !u
            .files
            .iter()
            .any(|f| f.rule == Rule::Free && f.path == "**")
        {
            s.push_str("| anything else | not allowed | build output is ignored |\n");
        }
    }

    if !u.requirements.is_empty() {
        s.push_str("\n## Requirements\n\n");
        for r in &u.requirements {
            s.push_str(&format!("- {}\n", r.trim()));
        }
    }

    s.push_str("\n## Allowed\n\n");
    match &u.allowed {
        None => s.push_str("No restriction: use whatever you need.\n"),
        Some(list) if list.is_empty() => {
            s.push_str("Only your own code and the language's built-ins.\n")
        }
        Some(list) => {
            s.push_str(&code_list(list));
            s.push_str("\n\nA module covers everything inside it. Anything not listed here, and not written by you, is off limits.\n");
        }
    }
    if !u.forbidden.is_empty() {
        s.push_str("\nForbidden even so: ");
        s.push_str(&code_list(&u.forbidden));
        s.push('\n');
    }
    if u.allowed.is_some() || !u.forbidden.is_empty() {
        s.push_str("\nYour own tests in `tests/` may use anything.\n");
    }

    if u.environment.is_some() || !u.env.is_empty() {
        s.push_str("\n## Environment\n\n");
        if let Some(e) = &u.environment {
            s.push_str(e.trim());
            s.push('\n');
        }
        if !u.env.is_empty() {
            if u.environment.is_some() {
                s.push('\n');
            }
            s.push_str("proctor sets these variables when it builds and runs your work:\n\n");
            for (k, v) in &u.env {
                s.push_str(&format!("- `{k}={v}`\n"));
            }
        }
    }

    s.push_str("\n## Passing conditions\n\n");
    s.push_str(&passing(c, u, &lang));
    s.push_str("\nPasses when `proctor check` prints `OK`.\n");

    let ws = c.workspace_name(u);
    let is_last_stage = u.is_stage()
        && c.stages_of(u.project.as_deref().unwrap_or_default())
            .last()
            .map(|x| x.id.as_str())
            == Some(u.id.as_str());
    let next = if is_last_stage {
        "the project is complete and proctor offers what comes next"
    } else if u.is_stage() {
        "proctor offers the next stage"
    } else {
        "proctor offers the next exercise"
    };
    s.push_str(&format!(
        "\n## Submission\n\nWork in `{ws}/` and run `proctor check` from there. When it prints `OK`, {next}.\n\nFair play: docs, books and forums are fine, AI assistants are not.\n"
    ));
    s
}

fn passing(c: &CourseDir, u: &Unit, lang: &str) -> String {
    let chain = c.chain(u);
    let mut lines: Vec<String> = Vec::new();
    match u.target {
        Target::Function => {
            let how = match lang {
                "python" => {
                    "proctor imports your code and calls the functions above itself. Code at module level runs on import, so keep scripts behind `if __name__ == \"__main__\":`."
                }
                "rust" => {
                    "proctor links your library crate (`src/lib.rs`) and calls the functions above itself; they must be `pub` at the crate root. A `main`, if any, is not run."
                }
                "c" => {
                    "proctor compiles your files with its own `main` and calls the functions above itself. Your `main`, if any, is compiled but never run."
                }
                _ => "proctor calls the functions above itself; your `main`, if any, is not run.",
            };
            lines.push(format!("**Graded target: your functions.** {how}"));
        }
        Target::Program => {
            let mut l = "**Graded target: your program as a whole**, including `main`, its arguments, its output and its exit code.".to_string();
            if let Some(r) = c.run_cmd(u)
                && !r.contains('{')
            {
                l.push_str(&format!(" It is run as `{r}`."));
            }
            lines.push(l);
        }
    }
    let mut steps = Vec::new();
    if !u.files.is_empty() {
        steps.push("structure");
    }
    if chain.iter().any(|x| !x.functions.is_empty()) {
        steps.push("prototypes");
    }
    if u.allowed.is_some() {
        steps.push("allowed");
    }
    steps.push("build");
    if u.has_tests() {
        steps.push("tests");
    }
    if chain.len() > 1 && chain[..chain.len() - 1].iter().any(|x| x.has_tests()) {
        steps.push("regression");
    }
    let mut run_line = format!("`proctor check` runs, in order: {}.", steps.join(", "));
    if steps.contains(&"regression") {
        run_line.push_str(&format!(
            " Regression re-runs the tests of {} to {}, so earlier behaviour must keep working.",
            label(chain[0]),
            label(chain[chain.len() - 2])
        ));
    }
    lines.push(run_line);
    let cfg = c.lang_config(u);
    if let Some(flags) = cfg.flags.as_ref().filter(|f| !f.is_empty()) {
        let werror = flags.iter().any(|f| f == "-Werror");
        lines.push(format!(
            "Your code is compiled with `{}`{}",
            flags.join(" "),
            if werror {
                ": a warning fails the build."
            } else {
                "."
            }
        ));
    }
    let statics = c.static_checks(u);
    if !statics.is_empty() {
        let cmds: Vec<String> = statics.iter().map(|x| format!("`{}`", x.cmd)).collect();
        lines.push(format!("{} must pass.", cmds.join(" and ")));
    }
    if let Some(w) = c.wrap(u) {
        lines.push(format!(
            "Every run of your code goes through `{w}`: anything it reports (memory errors, leaks) fails the run."
        ));
    }
    let random = !u.random.fuzz.is_empty() || !u.random.laws.is_empty();
    if random {
        let rounds = u.random.rounds.unwrap_or(DEFAULT_ROUNDS);
        lines.push(format!(
            "Some tests are randomized: about {rounds} generated inputs each, with a new seed on every run. A failure prints the seed, and `proctor check --seed <seed>` replays it."
        ));
    }
    let secs = c.timeout_ms(u).unwrap_or(crate::engine::DEFAULT_TIMEOUT_MS) as f64 / 1000.0;
    lines.push(format!(
        "Each call or run is stopped after {} s; a crash or a hang fails the test.",
        if secs.fract() == 0.0 {
            format!("{secs:.0}")
        } else {
            format!("{secs}")
        }
    ));
    lines.iter().map(|l| format!("- {l}\n")).collect()
}

pub fn overview_md(c: &CourseDir, pid: &str) -> String {
    let Some(p) = c.course.projects.get(pid) else {
        return String::new();
    };
    let code = pid.split('-').next().unwrap_or(pid);
    let stages = c.stages_of(pid);
    let lang = stages
        .first()
        .map(|u| c.language(u))
        .or_else(|| p.language.clone())
        .unwrap_or_default();
    let mut s = format!("# {code}: {}\n\n", p.title.trim());
    s.push_str(&header_table(&[
        ("Language", display_language(c, &lang)),
        (
            "Difficulty",
            p.difficulty.map(|d| format!("{d} / 5")).unwrap_or_default(),
        ),
        ("Prerequisites", list_or_none(&p.prereqs)),
        ("Concepts", p.concepts.join(", ")),
    ]));
    s.push_str(&format!("\n## Goal\n\n{}\n", p.goal.trim()));
    if let Some(m) = p.minimum.as_deref().filter(|m| !m.trim().is_empty()) {
        s.push_str(&format!(
            "\n## Minimum viable result\n\n{}\n\nThe required stages deliver this. Stretch stages add to it, only once it is stable.\n",
            m.trim()
        ));
    }
    s.push_str("\n## Stages\n\n");
    for u in &stages {
        s.push_str(&format!(
            "- {} {}: {}{}\n",
            label(u),
            u.title.trim(),
            first_sentence(&u.goal),
            if u.stretch { " (stretch goal)" } else { "" }
        ));
    }
    s.push_str(&format!(
        "\n## How a project works\n\nYou keep one workspace, `{pid}/`, from the first stage to the last. A stage's\ninstructions appear only once the previous stage passes, and each stage adds to the\nsame workspace: proctor creates missing files and stubs and never touches a file\nyou already wrote.\n\nEach stage's check also re-runs the tests of the earlier stages. The Allowed list of a\nstage always includes the previous ones.\n\n## Submission\n\nWork in `{pid}/` and run `proctor check` from there. When it prints `OK`,\nproctor offers the next stage.\n\nFair play: docs, books and forums are fine, AI assistants are not.\n"
    ));
    s
}

fn first_sentence(s: &str) -> String {
    let t = s.trim();
    match t.find(". ") {
        Some(i) => t[..=i].to_string(),
        None => t.to_string(),
    }
}
