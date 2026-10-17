//! `taskmaster new`: a short planning conversation in the terminal. It ends
//! with the same curriculum file the browser path produces, so both go
//! through one validator and one approval step.

use crate::curriculum::{self, Plan};
use crate::llm::{self, Msg};
use crate::prompts;
use crate::{Args, LLM_OPTS, read_line};
use std::collections::BTreeSet;
use std::io::IsTerminal;

const MAX_QUESTION_ROUNDS: usize = 2;
const MAX_AUTOFIX: usize = 3;

fn ids(plan: &Plan) -> BTreeSet<String> {
    plan.entries.iter().map(|e| e.id.clone()).collect()
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
    let mut previous: Option<BTreeSet<String>> = None;
    loop {
        eprintln!("thinking...");
        let reply = llm.chat(&messages)?;
        if let (Some(q), None) = (
            llm::block(&reply, "questions"),
            llm::block(&reply, "curriculum"),
        ) {
            messages.push(Msg::assistant(reply));
            if interactive && rounds < MAX_QUESTION_ROUNDS {
                rounds += 1;
                println!("\n{q}\n");
                let answer =
                    read_line("Your answers (one line, `skip` keeps the suggested defaults): ");
                let answer = if answer.is_empty() || answer == "skip" {
                    "Use your suggested defaults.".to_string()
                } else {
                    answer
                };
                messages.push(Msg::user(answer));
            } else {
                messages.push(Msg::user(
                    "Use your suggested defaults and write the curriculum now.",
                ));
            }
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
        if let (Some(before), Ok(plan)) = (&previous, curriculum::parse(&text)) {
            let after = ids(&plan);
            let added: Vec<&String> = after.difference(before).collect();
            let removed: Vec<&String> = before.difference(&after).collect();
            if !added.is_empty() || !removed.is_empty() {
                println!(
                    "\nChanges: added {}; removed {}",
                    if added.is_empty() {
                        "none".into()
                    } else {
                        added
                            .iter()
                            .map(|s| s.as_str())
                            .collect::<Vec<_>>()
                            .join(", ")
                    },
                    if removed.is_empty() {
                        "none".into()
                    } else {
                        removed
                            .iter()
                            .map(|s| s.as_str())
                            .collect::<Vec<_>>()
                            .join(", ")
                    }
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
            continue;
        };
        println!("\n{}\n{}", plan.meta.title, curriculum::table(&plan));
        previous = Some(ids(&plan));
        let a = read_line("[Y] approve, [e]dit the file, [q]uit, or type the changes you want: ");
        match a.to_lowercase().as_str() {
            "" | "y" | "yes" | "ok" => {
                crate::approve(&course, &text)?;
                println!(
                    "Approved: {}. Next: taskmaster build",
                    course.join("curriculum.md").display()
                );
                eprintln!("{}", llm.usage_line());
                return Ok(true);
            }
            "e" | "edit" => {
                let ok = crate::review(&course, text, false)?.is_some();
                eprintln!("{}", llm.usage_line());
                return Ok(ok);
            }
            "q" | "quit" => {
                println!(
                    "Draft kept in {}. Resume with: taskmaster import curriculum.md",
                    course.join("curriculum.md").display()
                );
                return Ok(false);
            }
            _ => {
                messages.push(Msg::assistant(reply));
                messages.push(Msg::user(format!(
                    "Revise the curriculum: {a}\nSend the complete file again."
                )));
            }
        }
    }
}
