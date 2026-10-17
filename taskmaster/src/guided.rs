//! `taskmaster new`: a short planning conversation in the terminal. It ends
//! with the same curriculum file the browser path produces, so both go
//! through one validator and one approval step.
//!
//! The planner asks its questions as multiple choice, shown one at a time.
//! Its first valid draft gets one self-review against the planning checklist
//! before the student sees it.

use crate::curriculum::{self, Plan};
use crate::llm::{self, Msg};
use crate::prompts;
use crate::{Args, LLM_OPTS, read_line};
use serde::Deserialize;
use skillcheck::ask::{self, Answer};
use std::collections::BTreeSet;
use std::io::IsTerminal;

const MAX_QUESTION_ROUNDS: usize = 2;
const MAX_AUTOFIX: usize = 3;
const MAX_QUESTIONS: usize = 4;
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

/// Asks each question in turn. Without a terminal, or with `--yes`, the
/// recommended options are taken.
fn ask_all(qs: &[Question], interactive: bool) -> Vec<String> {
    let total = qs.len();
    qs.iter()
        .enumerate()
        .map(|(i, q)| {
            if q.options.is_empty() {
                if !interactive {
                    return "No preference, use your judgement.".to_string();
                }
                println!("\n? ({}/{total}) {}", i + 1, q.question);
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
            let title = format!("({}/{total}) {}", i + 1, q.question);
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
    match ask::choose(
        "What do you want to do with this curriculum?",
        &options,
        0,
        Some("Something else: type the changes you want"),
    ) {
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
            "say what you want to learn: taskmaster new \"learn C pointers\" [--from syllabus.txt]"
                .into(),
        );
    }
    let interactive = std::io::stdin().is_terminal() && !a.has("yes");
    let llm = a.llm()?;
    eprintln!("model {} at {}", llm.model, llm.base_url);
    let system = format!(
        "{}\n\n{}\n## Example\n\n{}\n\n{}",
        prompts::GUIDED_SYSTEM,
        prompts::PLAN_FORMAT,
        prompts::PLAN_EXAMPLE,
        crate::env::detect(None).prompt_block()
    );
    let mut messages = vec![Msg::system(system), Msg::user(intake)];
    let mut rounds = 0;
    let mut fixes = 0;
    let mut bad_questions = false;
    let mut reviewed = false;
    let mut previous: Option<BTreeSet<String>> = None;
    loop {
        eprintln!("thinking...");
        let reply = llm.chat(&messages)?;
        if let (Some(q), None) = (
            llm::block(&reply, "questions"),
            llm::block(&reply, "curriculum"),
        ) {
            messages.push(Msg::assistant(reply));
            if rounds >= MAX_QUESTION_ROUNDS {
                messages.push(Msg::user(
                    "Use your recommended options and write the curriculum now.",
                ));
                continue;
            }
            let qs = match parse_questions(&q) {
                Ok(qs) => qs,
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
            rounds += 1;
            if interactive {
                println!(
                    "\nA few questions first. Enter keeps the recommended option; type a letter, or your own answer."
                );
            }
            let answers = ask_all(&qs, interactive);
            messages.push(Msg::user(answers_message(&qs, &answers)));
            continue;
        }
        let text = crate::unwrap_curriculum(&reply);
        let problems = match curriculum::parse(&text) {
            Ok(plan) => plan.validate(),
            Err(p) => p,
        };
        if problems.iter().any(|p| !p.warning) {
            if fixes >= MAX_AUTOFIX {
                std::fs::create_dir_all(&course).map_err(|e| e.to_string())?;
                std::fs::write(course.join("curriculum.md"), &text).map_err(|e| e.to_string())?;
                println!("{}", curriculum::report(&problems));
                return Err("the planner could not produce a valid curriculum; the last draft is in curriculum.md".into());
            }
            fixes += 1;
            eprintln!(
                "draft rejected by the validator ({} problem(s)), asking for a fix",
                problems.iter().filter(|p| !p.warning).count()
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
        if let Some(changes) = llm::block(&reply, "changes")
            && !changes.trim().is_empty()
        {
            println!("\nPlanner review:\n{}", changes.trim());
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
            let ok = crate::review(&course, text, true)?.is_some();
            eprintln!("{}", llm.usage_line());
            return Ok(ok);
        }
        std::fs::create_dir_all(&course).map_err(|e| e.to_string())?;
        std::fs::write(course.join("curriculum.md"), &text).map_err(|e| e.to_string())?;
        let Some(plan) = crate::check_plan(&text, false) else {
            // Only the environment preflight can fail here: let the planner adapt.
            messages.push(Msg::assistant(reply));
            messages.push(Msg::user(
                "The student's environment cannot run part of this curriculum (see the environment section). \
                 Adapt it and send the complete file again."
                    .to_string(),
            ));
            fixes += 1;
            if fixes > MAX_AUTOFIX {
                return Err("the planner could not fit the curriculum to this environment; the last draft is in curriculum.md".into());
            }
            continue;
        };
        println!("\n{}\n{}", plan.meta.title, curriculum::table(&plan));
        previous = Some(ids(&plan));
        match decide() {
            Decision::Approve => {
                crate::approve(&course, &text)?;
                println!(
                    "Approved: {}. Next: taskmaster build",
                    course.join("curriculum.md").display()
                );
                eprintln!("{}", llm.usage_line());
                return Ok(true);
            }
            Decision::Edit => {
                let ok = crate::review(&course, text, false)?.is_some();
                eprintln!("{}", llm.usage_line());
                return Ok(ok);
            }
            Decision::Quit => {
                println!(
                    "Draft kept in {}. Resume with: taskmaster import curriculum.md",
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
        let a = ask_all(&qs, false);
        assert_eq!(a[0], "y");
        assert!(a[1].contains("No preference"));
        assert!(answers_message(&qs, &a).contains("- Q1 -> y"));
    }
}
