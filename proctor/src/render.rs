//! Renders instruction files from unit records, in one fixed design. The
//! markdown is never written by an LLM: it is derived from the same record
//! proctor grades against, at the moment the unit is revealed.

use crate::course::CourseDir;
use crate::engine::DEFAULT_ROUNDS;
use crate::unit::{Rule, Target, Unit};
use std::collections::BTreeSet;

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
        let heading = if u.guided {
            "Walkthrough"
        } else {
            "Before you start"
        };
        s.push_str(&format!("\n## {heading}\n\n{}\n", lesson.trim()));
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
        // With `**` free, other free entries add nothing: they are left out.
        let all_free = u
            .files
            .iter()
            .any(|f| f.rule == Rule::Free && f.path == "**");
        s.push_str("\n## Expected files\n\n| Path | Rule | Notes |\n|------|------|-------|\n");
        for f in &u.files {
            if all_free && f.rule == Rule::Free && f.path != "**" {
                continue;
            }
            let rule = match f.rule {
                Rule::Required => "required",
                Rule::OneOrMore => "one or more",
                Rule::Free => "free",
                Rule::Forbidden => "forbidden",
            };
            let path = if f.path == "**" {
                "anything else".to_string()
            } else {
                format!("`{}`", cell(&f.path))
            };
            s.push_str(&format!("| {path} | {rule} | {} |\n", cell(&f.note)));
        }
        if !all_free {
            s.push_str("| anything else | not allowed | build output is ignored |\n");
        }
    }

    if !u.requirements.is_empty() {
        s.push_str("\n## Requirements\n\n");
        s.push_str(&u.requirements.markdown());
    }

    let examples = examples_md(c, u);
    if !examples.is_empty() {
        s.push_str(&format!("\n## Examples\n\n{examples}"));
    }
    if !u.think_about.is_empty() {
        s.push_str("\n### Think about\n\n");
        for t in &u.think_about {
            s.push_str(&format!("- {}\n", t.trim()));
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
            s.push('\n');
        }
    }
    if !u.forbidden.is_empty() {
        s.push_str("\n**Forbidden even so:** ");
        s.push_str(&code_list(&u.forbidden));
        s.push('\n');
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

    s.push_str("\n## Checking your work\n\n");
    s.push_str(&checking(c, u, &lang));
    s
}

/// Path from a unit's instructions to the course's RULES.md.
fn rules_link(c: &CourseDir, u: &Unit) -> String {
    let depth = c.subject_rel(u).components().count().saturating_sub(1);
    format!("{}{}", "../".repeat(depth), crate::rules::FILE)
}

/// What is specific to grading this unit; the rest is in RULES.md.
fn checking(c: &CourseDir, u: &Unit, lang: &str) -> String {
    let chain = c.chain(u);
    let mut lines: Vec<String> = Vec::new();
    match u.target {
        Target::Function => {
            let how = match lang {
                "python" => {
                    "proctor imports your code and calls them itself; keep scripts behind `if __name__ == \"__main__\":`."
                }
                "rust" => {
                    "proctor calls them from your library crate (`src/lib.rs`), where they must be `pub`; a `main` is not run."
                }
                "c" => {
                    "proctor compiles your files with its own `main` and calls them; yours is never run."
                }
                _ => "proctor calls them itself; your `main`, if any, is not run.",
            };
            lines.push(format!("**Graded: your functions.** {how}"));
        }
        Target::Program => {
            let mut l =
                "**Graded: your program as a whole:** its output and its exit status.".to_string();
            if let Some(r) = c.run_cmd(u)
                && !r.contains('{')
            {
                l.push_str(&format!(" It is run as `{r}`."));
            }
            lines.push(l);
        }
    }
    if chain.len() > 1 && chain[..chain.len() - 1].iter().any(|x| x.has_tests()) {
        lines.push(format!(
            "**Regression:** the tests of {} to {} run again, so earlier behaviour must keep working.",
            label(chain[0]),
            label(chain[chain.len() - 2])
        ));
    }
    if !u.random.fuzz.is_empty() || !u.random.laws.is_empty() {
        let rounds = u.random.rounds.unwrap_or(DEFAULT_ROUNDS);
        lines.push(format!(
            "**Randomized tests:** about {rounds} generated inputs each, compared with a hidden reference or checked against invariants."
        ));
    }
    if let Some(w) = c.wrap(u) {
        lines.push(format!(
            "Every run goes through `{w}`: anything it reports (memory errors, leaks) fails the run."
        ));
    }
    if let Some(ms) = c
        .timeout_ms(u)
        .filter(|ms| *ms != crate::engine::DEFAULT_TIMEOUT_MS)
    {
        lines.push(format!(
            "**Time limit:** {} s per call or run.",
            ms as f64 / 1000.0
        ));
    }
    let ws = c.workspace_name(u);
    let is_last_stage = u.is_stage()
        && c.stages_of(u.project.as_deref().unwrap_or_default())
            .last()
            .map(|x| x.id.as_str())
            == Some(u.id.as_str());
    let next = if is_last_stage {
        "the project is complete"
    } else if u.is_stage() {
        "proctor offers the next stage"
    } else {
        "proctor offers the next exercise"
    };
    lines.push(format!(
        "Work in `{ws}/` and run `proctor check` until it prints `OK`; then {next}."
    ));
    let mut s: String = lines.iter().map(|l| format!("- {l}\n")).collect();
    s.push_str(&format!(
        "\nThis course is graded at **{}** rigor: edge cases, exact output, linters and time limits are in [`{}`]({}).\n",
        crate::rules::rigor(c),
        crate::rules::FILE,
        rules_link(c, u)
    ));
    s
}

/// Splits `lhs == rhs` at its only top-level `==` (outside strings and brackets).
fn split_eq(line: &str) -> Option<(&str, &str)> {
    let b = line.as_bytes();
    let (mut depth, mut quote, mut at) = (0i32, None::<u8>, None);
    let mut i = 0;
    while i < b.len() {
        let ch = b[i];
        match quote {
            Some(q) => {
                if ch == b'\\' {
                    i += 1;
                } else if ch == q {
                    quote = None;
                }
            }
            None => match ch {
                b'"' | b'\'' => quote = Some(ch),
                b'(' | b'[' | b'{' => depth += 1,
                b')' | b']' | b'}' => depth -= 1,
                b'=' if depth == 0 && b.get(i + 1) == Some(&b'=') => {
                    if at.is_some() {
                        return None;
                    }
                    at = Some(i);
                    i += 1;
                }
                b'&' | b'|' if depth == 0 && b.get(i + 1) == Some(&ch) => return None,
                _ => {}
            },
        }
        i += 1;
    }
    let at = at?;
    Some((line[..at].trim(), line[at + 2..].trim()))
}

/// Case lines and whether a `# example` comment line marks them.
fn marked_cases(u: &Unit) -> Vec<(String, bool)> {
    let mut out = Vec::new();
    let mut mark = false;
    for l in u.cases.as_deref().unwrap_or("").lines() {
        let t = l.trim();
        if t.is_empty() {
            continue;
        }
        if let Some(c) = t.strip_prefix('#') {
            mark = c.trim().eq_ignore_ascii_case("example");
            continue;
        }
        out.push((t.to_string(), mark));
        mark = false;
    }
    out
}

/// Inputs too small to show: they belong to the edge cases the student finds.
fn looks_empty(s: &str) -> bool {
    ["\"\"", "''", "[]", "{}", "vec![]", "String::new()"]
        .iter()
        .any(|e| s.contains(e))
}

/// A function that returns nothing and changes an argument (`&mut`, `char *`):
/// the harness evaluates the call to that argument.
fn in_place(sig: &str) -> bool {
    let s = sig.trim();
    let no_return = if s.starts_with("fn ") || s.starts_with("pub fn ") {
        !s.contains("->")
    } else {
        s.starts_with("void ")
    };
    no_return
        && (s.contains("&mut") || (s.contains("char") && s.contains('*') && !s.contains("const")))
}

/// Examples built from the unit's own test cases, so they cannot disagree
/// with the grading: the cases marked `# example`, else the first ordinary
/// case of each function (or the first runs with an exact output).
fn examples_md(c: &CourseDir, u: &Unit) -> String {
    let cases = marked_cases(u);
    let any_marked = cases.iter().any(|(_, m)| *m);
    match u.target {
        Target::Function => {
            let short = |n: &str| n.rsplit('.').next().unwrap_or(n).to_string();
            let mut rows: Vec<(String, String, bool)> = Vec::new();
            let mut seen: BTreeSet<String> = BTreeSet::new();
            for (line, marked) in &cases {
                let Some((lhs, rhs)) = split_eq(line) else {
                    continue;
                };
                let Some(open) = lhs.find('(') else {
                    continue;
                };
                let name = lhs[..open].trim();
                let f = u
                    .functions
                    .iter()
                    .find(|f| f.name == name || short(&f.name) == short(name));
                let ok = if any_marked {
                    *marked
                } else {
                    !lhs.contains("raises(")
                        && !looks_empty(lhs)
                        && !looks_empty(rhs)
                        && lhs.ends_with(')')
                        && (f.is_some() || u.functions.is_empty())
                        && seen.insert(name.to_string())
                };
                if ok {
                    rows.push((
                        lhs.to_string(),
                        rhs.to_string(),
                        f.is_some_and(|f| in_place(&f.sig)),
                    ));
                }
                if rows.len() >= 8 {
                    break;
                }
            }
            if rows.is_empty() {
                return String::new();
            }
            let mut s = String::from("| Call | Result |\n|------|--------|\n");
            for (call, result, inplace) in rows {
                let result = if inplace {
                    format!("the argument becomes `{}`", cell(&result))
                } else {
                    format!("`{}`", cell(&result))
                };
                s.push_str(&format!("| `{}` | {result} |\n", cell(&call)));
            }
            s
        }
        Target::Program => {
            let cmd = if c.language(u) == "rust" {
                "cargo run --quiet --".to_string()
            } else {
                c.run_cmd(u)
                    .filter(|r| !r.contains('{'))
                    .unwrap_or_else(|| "./program".into())
            };
            let mut shown = Vec::new();
            for (line, marked) in &cases {
                if any_marked && !marked {
                    continue;
                }
                let Ok(case) = crate::program::parse_case(line) else {
                    continue;
                };
                let out = case.asserts.iter().find_map(|a| match a {
                    crate::program::Assert::Eq(crate::program::Stream::Out, s) => Some(s.clone()),
                    _ => None,
                });
                let Some(out) = out.filter(|o| !o.is_empty()) else {
                    continue;
                };
                if !marked && (!case.files.is_empty() || case.stdin.as_deref() == Some("")) {
                    continue;
                }
                let exit = case.asserts.iter().find_map(|a| match a {
                    crate::program::Assert::Exit(n) => Some(*n),
                    _ => None,
                });
                let args: Vec<String> = case
                    .args
                    .iter()
                    .map(|a| {
                        if a.is_empty() || a.contains(char::is_whitespace) {
                            format!("\"{a}\"")
                        } else {
                            a.clone()
                        }
                    })
                    .collect();
                let run = if args.is_empty() {
                    cmd.clone()
                } else {
                    format!("{cmd} {}", args.join(" "))
                };
                let mut s = format!("Running `{run}`");
                for (name, body) in &case.files {
                    s.push_str(&format!(" with a file `{name}` holding `{}`", cell(body)));
                }
                match &case.stdin {
                    Some(input) => s.push_str(&format!(
                        " with this input:\n\n```text\n{}\n```\n\nprints:\n",
                        input.trim_end_matches('\n')
                    )),
                    None => s.push_str(" prints:\n"),
                }
                s.push_str(&format!("\n```text\n{}\n```\n", out.trim_end_matches('\n')));
                if let Some(n) = exit.filter(|n| *n != 0) {
                    s.push_str(&format!("\nand exits with status `{n}`.\n"));
                }
                shown.push(s);
                if shown.len() >= 2 {
                    break;
                }
            }
            if shown.is_empty() {
                return match u.example.as_deref().filter(|e| !e.trim().is_empty()) {
                    Some(ex) => format!("```text\n{}\n```\n", ex.trim_end()),
                    None => String::new(),
                };
            }
            shown.join("\n")
        }
    }
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
        "\n## How a project works\n\nYou keep one workspace, `{pid}/`, from the first stage to the last. A stage's\ninstructions appear only once the previous stage passes, and each stage adds to the\nsame workspace: proctor creates missing files and stubs and never touches a file\nyou already wrote.\n\nEach stage's check also re-runs the tests of the earlier stages. The Allowed list of a\nstage always includes the previous ones.\n\n## Submission\n\nWork in `{pid}/` and run `proctor check` from there. When it prints `OK`,\nproctor offers the next stage. The course's rules are in [`RULES.md`](../../RULES.md).\n"
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

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn splits_one_top_level_equality() {
        assert_eq!(
            split_eq(r#"ft_repeat("a==b", 2) == "a==ba==b""#),
            Some((r#"ft_repeat("a==b", 2)"#, r#""a==ba==b""#))
        );
        assert_eq!(split_eq("f(1) == 1 && g(2) == 2"), None);
        assert_eq!(split_eq("raises(f(1))"), None);
    }

    #[test]
    fn in_place_signatures() {
        assert!(in_place("pub fn ft_capitalize(s: &mut String)"));
        assert!(!in_place("pub fn ft_concat(a: String, b: &str) -> String"));
        assert!(in_place("void str_rev(char *s)"));
        assert!(!in_place("size_t str_len(const char *s)"));
    }
}
