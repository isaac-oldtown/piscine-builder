//! `coursecrafter new`: a short planning conversation in the terminal. It ends
//! with the same curriculum file the browser path produces, so both go
//! through one validator and one approval step.
//!
//! The planner asks one multiple-choice question per reply, so each question
//! can build on the previous answers.
//! Its first valid draft gets one self-review against the planning checklist
//! before the student sees it.

use crate::curriculum::{self, Plan};
use crate::llm::{self, Llm, Msg};
use crate::prompts;
use crate::{Args, LLM_OPTS, read_line};
use proctor::ask::{self, Answer};
use serde::Deserialize;
use std::collections::BTreeSet;
use std::io::IsTerminal;
use std::path::Path;

const MAX_AUTOFIX: usize = 3;
/// Questions in a whole conversation, asked one per planner reply.
const MAX_QUESTIONS: usize = 6;
const OTHER: &str = "Something else: type your own answer";

#[derive(Deserialize, Debug, PartialEq)]
pub struct Question {
    pub question: String,
    #[serde(default)]
    pub options: Vec<String>,
    #[serde(default)]
    pub recommended: usize,
}

/// Parses the `<questions>` JSON. Accepts a bare array or `{"questions": [...]}`.
pub fn parse_questions(body: &str) -> Result<Vec<Question>, String> {
    #[derive(Deserialize)]
    #[serde(untagged)]
    enum Shape {
        List(Vec<Question>),
        Wrapped { questions: Vec<Question> },
    }
    let qs = match serde_json::from_str::<Shape>(body.trim()).map_err(|e| e.to_string())? {
        Shape::List(q) | Shape::Wrapped { questions: q } => q,
    };
    let qs: Vec<Question> = qs
        .into_iter()
        .filter(|q| !q.question.trim().is_empty())
        .take(MAX_QUESTIONS)
        .map(|mut q| {
            q.options.retain(|o| !o.trim().is_empty());
            q.recommended = q.recommended.min(q.options.len().saturating_sub(1));
            q
        })
        .collect();
    if qs.is_empty() {
        return Err("no questions".into());
    }
    Ok(qs)
}

/// One line per question for the planner: "question -> answer".
fn answers_message(qs: &[Question], answers: &[String]) -> String {
    let mut s = String::from("My answers:\n");
    for (q, a) in qs.iter().zip(answers) {
        s.push_str(&format!("- {} -> {a}\n", q.question.trim()));
    }
    s
}

/// Asks each question in turn; `first` is how many were asked before.
/// Without a terminal, or with `--yes`, the recommended options are taken.
fn ask_all(qs: &[Question], interactive: bool, first: usize) -> Vec<String> {
    qs.iter()
        .enumerate()
        .map(|(i, q)| {
            let n = first + i + 1;
            if q.options.is_empty() {
                if !interactive {
                    return "No preference, use your judgement.".to_string();
                }
                println!("\n? Question {n}: {}", q.question);
                let a = read_line("Your answer (Enter: no preference): ");
                return if a.is_empty() {
                    "No preference, use your judgement.".to_string()
                } else {
                    a
                };
            }
            if !interactive {
                return q.options[q.recommended].clone();
            }
            let title = format!("Question {n}: {}", q.question);
            ask::choose(&title, &q.options, q.recommended, Some(OTHER)).text(&q.options)
        })
        .collect()
}

fn ids(plan: &Plan) -> BTreeSet<String> {
    plan.entries.iter().map(|e| e.id.clone()).collect()
}

fn list(v: &[&String]) -> String {
    if v.is_empty() {
        "none".into()
    } else {
        v.iter().map(|s| s.as_str()).collect::<Vec<_>>().join(", ")
    }
}

enum Decision {
    Approve,
    Edit,
    Quit,
    Revise(String),
}

fn decide() -> Decision {
    let options = [
        "Approve it".to_string(),
        "Ask the planner for changes".to_string(),
        "Edit curriculum.md myself".to_string(),
        "Quit and keep the draft".to_string(),
    ];
    // Typing the changes directly at the prompt also works (same as b).
    match ask::choose_or_type("What do you want to do with this curriculum?", &options, 0) {
        Answer::Pick(0) => Decision::Approve,
        Answer::Pick(1) => {
            let c = read_line("Changes you want: ");
            if c.is_empty() {
                decide()
            } else {
                Decision::Revise(c)
            }
        }
        Answer::Pick(2) => Decision::Edit,
        Answer::Pick(_) => Decision::Quit,
        Answer::Other(c) => Decision::Revise(c),
    }
}

pub fn cmd_new(raw: &[String]) -> Result<bool, String> {
    let mut valued = vec!["course", "from"];
    valued.extend(LLM_OPTS);
    let a = Args::parse(raw, &valued, &["yes"])?;
    let course = a.course();
    let mut intake = a.positional.join(" ");
    for f in a.all("from") {
        let body = std::fs::read_to_string(&f).map_err(|e| format!("{f}: {e}"))?;
        intake.push_str(&format!("\n\nMaterial from {f}:\n{body}"));
    }
    if intake.trim().is_empty() {
        return Err(
            "say what you want to learn: coursecrafter new \"learn C pointers\" [--from syllabus.txt]"
                .into(),
        );
    }
    let interactive = std::io::stdin().is_terminal() && !a.has("yes");
    let llm = a.llm()?;
    eprintln!("model {} at {}", llm.model, llm.base_url);
    converse(&course, &llm, intake, interactive, None, Mode::New)
}

/// `coursecrafter revise` : the planner rewrites the current curriculum.md.
pub fn cmd_revise(raw: &[String]) -> Result<bool, String> {
    let mut valued = vec!["course"];
    valued.extend(LLM_OPTS);
    let a = Args::parse(raw, &valued, &["yes"])?;
    let changes = a.positional.join(" ");
    if changes.trim().is_empty() {
        return Err(
            "say what to change: coursecrafter revise \"split p01.4 into two stages\"".into(),
        );
    }
    let course = a.course();
    let file = course.join("curriculum.md");
    let text = std::fs::read_to_string(&file).map_err(|_| {
        format!(
            "no curriculum at {}: run `coursecrafter new` or `coursecrafter import` first",
            file.display()
        )
    })?;
    let plan = curriculum::parse(&text).map_err(|p| {
        format!(
            "curriculum.md does not parse; fix it by hand or with `coursecrafter import curriculum.md`:\n{}",
            curriculum::report(&p)
        )
    })?;
    // The draft replaces curriculum.md while you review it: keep the old one.
    let backup = course
        .join(".proctor")
        .join("coursecrafter")
        .join("curriculum.previous.md");
    if let Some(d) = backup.parent() {
        std::fs::create_dir_all(d).map_err(|e| e.to_string())?;
    }
    std::fs::write(&backup, &text).map_err(|e| format!("{}: {e}", backup.display()))?;
    let interactive = std::io::stdin().is_terminal() && !a.has("yes");
    let llm = a.llm()?;
    eprintln!("model {} at {}", llm.model, llm.base_url);
    eprintln!("previous version saved to {}", backup.display());
    converse(
        &course,
        &llm,
        prompts::revise_request(&text, &changes),
        interactive,
        Some(&plan),
        Mode::Revise,
    )
}

/// `coursecrafter convert`: a curriculum written elsewhere (any outline,
/// syllabus or plan) rewritten in the curriculum format, keeping its content.
pub fn cmd_convert(raw: &[String]) -> Result<bool, String> {
    let mut valued = vec!["course"];
    valued.extend(LLM_OPTS);
    let a = Args::parse(raw, &valued, &["yes"])?;
    if a.positional.is_empty() {
        return Err("give the file(s) holding your curriculum: coursecrafter convert my-plan.md [notes.txt]...".into());
    }
    let mut source = String::new();
    for f in &a.positional {
        let body = std::fs::read_to_string(f).map_err(|e| format!("{f}: {e}"))?;
        source.push_str(&format!(
            "<file name=\"{f}\">\n{}\n</file>\n",
            body.trim_end()
        ));
    }
    let course = a.course();
    let file = course.join("curriculum.md");
    if let Ok(old) = std::fs::read_to_string(&file) {
        let backup = course
            .join(".proctor")
            .join("coursecrafter")
            .join("curriculum.previous.md");
        if let Some(d) = backup.parent() {
            std::fs::create_dir_all(d).map_err(|e| e.to_string())?;
        }
        std::fs::write(&backup, old).map_err(|e| format!("{}: {e}", backup.display()))?;
        eprintln!(
            "the existing curriculum.md is saved to {}",
            backup.display()
        );
    }
    let interactive = std::io::stdin().is_terminal() && !a.has("yes");
    let llm = a.llm()?;
    eprintln!("model {} at {}", llm.model, llm.base_url);
    converse(
        &course,
        &llm,
        prompts::convert_request(&source),
        interactive,
        None,
        Mode::Convert,
    )
}

#[derive(Clone, Copy, PartialEq, Eq)]
enum Mode {
    /// Plan from a wish: questions, then one self-review of the first draft.
    New,
    /// Change the current curriculum.md; the request includes the review.
    Revise,
    /// Rewrite an outside curriculum in the format, no restructuring review.
    Convert,
}

impl Mode {
    fn name(self) -> &'static str {
        match self {
            Mode::New => "new",
            Mode::Revise => "revise",
            Mode::Convert => "convert",
        }
    }
}

/// The planning loop shared by `new`, `revise` and `convert`. With `current`
/// (a revision), its ids are the baseline of the change list. Only `new` gets
/// the separate self-review: a revision request includes it, and a
/// conversion must keep the author's plan rather than redesign it.
fn converse(
    course: &Path,
    llm: &Llm,
    intake: String,
    interactive: bool,
    current: Option<&Plan>,
    mode: Mode,
) -> Result<bool, String> {
    eprintln!("scanning this machine...");
    let system = format!(
        "{}\n\n{}\n## Example\n\n{}\n\n{}\n{}",
        prompts::GUIDED_SYSTEM,
        prompts::PLAN_FORMAT,
        prompts::PLAN_EXAMPLE,
        crate::env::detect(None).prompt_block(),
        crate::linux::detect().planning_block()
    );
    let mut messages = vec![Msg::system(system), Msg::user(intake)];
    let mut asked = 0;
    let mut fixes = 0;
    let mut bad_questions = false;
    let mut reviewed = mode != Mode::New;
    // The self-review's list of changes, kept until a draft is shown: the
    // reviewed draft may still need validator fixes first.
    let mut review_notes: Option<String> = None;
    let mut previous: Option<BTreeSet<String>> = current.map(ids);
    loop {
        eprintln!("thinking...");
        let reply = llm.chat(&messages)?;
        if let (Some(q), None) = (
            llm::block(&reply, "questions"),
            llm::block(&reply, "curriculum"),
        ) {
            messages.push(Msg::assistant(reply));
            if asked >= MAX_QUESTIONS {
                messages.push(Msg::user(
                    "Use your recommended options and write the curriculum now.",
                ));
                continue;
            }
            let qs = match parse_questions(&q) {
                Ok(mut qs) => {
                    qs.truncate(MAX_QUESTIONS - asked);
                    qs
                }
                Err(e) if !bad_questions => {
                    bad_questions = true;
                    messages.push(Msg::user(format!(
                        "The <questions> block is not valid JSON in the required shape ({e}). \
                         Send it again as a JSON array of {{\"question\", \"options\", \"recommended\"}}."
                    )));
                    continue;
                }
                Err(_) => {
                    messages.push(Msg::user(
                        "Use your recommended options and write the curriculum now.",
                    ));
                    continue;
                }
            };
            if interactive && asked == 0 {
                println!(
                    "\nA few questions first. Enter keeps the recommended option; type a letter, or your own answer."
                );
            }
            let answers = ask_all(&qs, interactive, asked);
            asked += qs.len();
            messages.push(Msg::user(answers_message(&qs, &answers)));
            continue;
        }
        if let Some(changes) = llm::block(&reply, "changes")
            && !changes.trim().is_empty()
        {
            review_notes = Some(changes.trim().to_string());
        }
        let (text, fixed) = curriculum::autofix(&crate::unwrap_curriculum(&reply));
        for f in &fixed {
            eprintln!("fixed: {f}");
        }
        // The planner continues from the corrected file, so later edits use the new ids.
        let reply = if fixed.is_empty() {
            reply
        } else {
            format!("<curriculum>\n{text}\n</curriculum>")
        };
        let problems = curriculum::problems_of(&text);
        let source = mode.name();
        curriculum::log_validation(course, source, &fixed, &problems);
        if problems.iter().any(|p| !p.warning) {
            if fixes >= MAX_AUTOFIX {
                std::fs::create_dir_all(course).map_err(|e| e.to_string())?;
                std::fs::write(course.join("curriculum.md"), &text).map_err(|e| e.to_string())?;
                println!("{}", curriculum::report(&problems));
                return Err("the planner could not produce a valid curriculum; the last draft is in curriculum.md".into());
            }
            fixes += 1;
            let errors: Vec<curriculum::Problem> =
                problems.iter().filter(|p| !p.warning).cloned().collect();
            eprintln!(
                "draft rejected by the validator, asking the planner to fix:\n{}",
                curriculum::report(&errors).trim_end()
            );
            messages.push(Msg::assistant(reply));
            messages.push(Msg::user(curriculum::paste_block(&problems)));
            continue;
        }
        fixes = 0;
        if !reviewed {
            // One self-review per conversation, on the first valid draft.
            reviewed = true;
            eprintln!("reviewing the draft against the planning checklist...");
            messages.push(Msg::assistant(reply));
            messages.push(Msg::user(prompts::review_request()));
            continue;
        }
        if let Some(notes) = review_notes.take() {
            let heading = if mode == Mode::Convert {
                "What the conversion added or changed"
            } else {
                "Planner review"
            };
            println!("\n{heading}:\n{notes}");
        }
        if let (Some(before), Ok(plan)) = (&previous, curriculum::parse(&text)) {
            let after = ids(&plan);
            let added: Vec<&String> = after.difference(before).collect();
            let removed: Vec<&String> = before.difference(&after).collect();
            if !added.is_empty() || !removed.is_empty() {
                println!(
                    "\nChanges: added {}; removed {}",
                    list(&added),
                    list(&removed)
                );
            }
        }
        if !interactive {
            let ok = crate::review(course, text.clone(), true)?.is_some();
            eprintln!("{}", llm.usage_line());
            if ok && let Ok(plan) = curriculum::parse(&text) {
                crate::linux::offer(&plan, false);
            }
            return Ok(ok);
        }
        std::fs::create_dir_all(course).map_err(|e| e.to_string())?;
        std::fs::write(course.join("curriculum.md"), &text).map_err(|e| e.to_string())?;
        // Already validated above; this prints the environment warnings.
        let Some(plan) = crate::check_plan(&text, false) else {
            return Err(
                "internal: a validated curriculum failed to load; the draft is in curriculum.md"
                    .into(),
            );
        };
        println!("\n{}\n{}", plan.meta.title, curriculum::table(&plan));
        previous = Some(ids(&plan));
        match decide() {
            Decision::Approve => {
                crate::approve(course, &text)?;
                println!("Approved: {}.", course.join("curriculum.md").display());
                eprintln!("{}", llm.usage_line());
                crate::linux::offer(&plan, true);
                println!("Next: coursecrafter build");
                return Ok(true);
            }
            Decision::Edit => {
                let approved = crate::review(course, text, false)?;
                eprintln!("{}", llm.usage_line());
                if let Some(plan) = approved.as_deref().and_then(|t| curriculum::parse(t).ok()) {
                    crate::linux::offer(&plan, true);
                }
                return Ok(approved.is_some());
            }
            Decision::Quit => {
                println!(
                    "Draft kept in {}. Resume with: coursecrafter import curriculum.md",
                    course.join("curriculum.md").display()
                );
                return Ok(false);
            }
            Decision::Revise(c) => {
                messages.push(Msg::assistant(reply));
                messages.push(Msg::user(format!(
                    "Revise the curriculum: {c}\nCheck the result against the review list, then send the complete file again."
                )));
            }
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn parses_question_arrays() {
        let qs = parse_questions(
            r#"[{"question": "Final project?", "options": ["A shell", "An HTTP server"], "recommended": 1},
                {"question": "Weeks?", "options": ["4", "6"]}]"#,
        )
        .unwrap();
        assert_eq!(qs.len(), 2);
        assert_eq!(qs[0].recommended, 1);
        assert_eq!(qs[1].recommended, 0);
    }

    #[test]
    fn accepts_wrapped_questions_and_clamps() {
        let qs = parse_questions(
            r#"{"questions": [{"question": "Q", "options": ["a", "", "b"], "recommended": 9}]}"#,
        )
        .unwrap();
        assert_eq!(qs[0].options, vec!["a", "b"]);
        assert_eq!(qs[0].recommended, 1);
    }

    #[test]
    fn caps_the_number_of_questions() {
        let one = r#"{"question": "Q", "options": ["x", "y"]}"#;
        let body = format!("[{}]", [one; 7].join(","));
        assert_eq!(parse_questions(&body).unwrap().len(), MAX_QUESTIONS);
    }

    #[test]
    fn rejects_prose_and_empty_lists() {
        assert!(parse_questions("1. What language?").is_err());
        assert!(parse_questions("[]").is_err());
    }

    #[test]
    fn defaults_are_taken_without_a_terminal() {
        let qs = parse_questions(
            r#"[{"question": "Q1", "options": ["x", "y"], "recommended": 1}, {"question": "Q2"}]"#,
        )
        .unwrap();
        let a = ask_all(&qs, false, 0);
        assert_eq!(a[0], "y");
        assert!(a[1].contains("No preference"));
        assert!(answers_message(&qs, &a).contains("- Q1 -> y"));
    }
}
