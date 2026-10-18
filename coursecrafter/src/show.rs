//! `coursecrafter show`: the curriculum as a readable overview, without the
//! truncation of the approval table. Read-only: no LLM, no file written.

use crate::curriculum::{self, Entry, Kind, Plan};
use crate::{Args, approval_path, text_hash};
use proctor::report::Style;

/// Terminal width from `COLUMNS`, else 100.
fn width() -> usize {
    std::env::var("COLUMNS")
        .ok()
        .and_then(|c| c.parse().ok())
        .unwrap_or(100usize)
        .clamp(60, 160)
}

/// Word-wraps `text` to `width`: the first line after `first`, the others after `rest`.
fn wrap(text: &str, first: &str, rest: &str, width: usize) -> String {
    let mut out = String::new();
    let mut line = first.to_string();
    let mut empty = true;
    for word in text.split_whitespace() {
        if !empty && line.chars().count() + 1 + word.chars().count() > width {
            out.push_str(line.trim_end());
            out.push('\n');
            line = rest.to_string();
            empty = true;
        }
        if !empty {
            line.push(' ');
        }
        line.push_str(word);
        empty = false;
    }
    out.push_str(line.trim_end());
    out.push('\n');
    out
}

fn hours(h: Option<f64>) -> String {
    h.map(|h| format!("{h} h")).unwrap_or_else(|| "? h".into())
}

fn level(d: Option<u8>) -> String {
    d.map(|d| d.to_string()).unwrap_or_else(|| "?".into())
}

fn list(v: &[String]) -> String {
    if v.is_empty() {
        "none".into()
    } else {
        v.join(", ")
    }
}

/// The field lines under a unit or project heading.
fn field(s: &mut String, st: &Style, label: &str, value: &str, indent: &str, w: usize) {
    let head = format!("{indent}{} ", st.dim(&format!("{label:<11}")));
    // `head` holds colour codes: wrap on the visible width.
    let visible = indent.len() + 12;
    let wrapped = wrap(value, &" ".repeat(visible), &" ".repeat(visible), w);
    s.push_str(&head);
    s.push_str(wrapped.trim_start());
}

fn unit(s: &mut String, st: &Style, plan: &Plan, e: &Entry, full: bool, w: usize) {
    let indent = if e.kind == Kind::Stage { "    " } else { "  " };
    let code = e.id.split('-').next().unwrap_or(&e.id);
    let tags = [
        hours(e.f.hours),
        format!("concept {}/5", level(e.f.concept_difficulty)),
        format!("impl {}/5", level(e.f.difficulty)),
        e.f.target.clone().unwrap_or_else(|| "auto".into()),
    ]
    .join(", ");
    let stretch = if e.f.stretch {
        st.yellow(" [stretch goal]")
    } else {
        String::new()
    };
    s.push_str(&format!(
        "\n{}{}  {}{stretch}  {}\n",
        &indent[..indent.len() - 2],
        st.bold(code),
        st.bold(&e.title),
        st.dim(&format!("({tags})"))
    ));
    if let Some(g) = &e.f.goal {
        s.push_str(&wrap(g, indent, indent, w));
    }
    if let Some(d) = &e.f.done_when {
        field(s, st, "done when", d, indent, w);
    }
    field(s, st, "introduces", &list(&e.f.introduces), indent, w);
    field(s, st, "assumes", &list(&e.f.assumes), indent, w);
    let notes: Vec<String> = plan
        .notes_for(&e.id)
        .iter()
        .map(|n| format!("{} ({})", n.title, n.f.teaches.join(", ")))
        .collect();
    if !notes.is_empty() {
        field(s, st, "notes", &notes.join("; "), indent, w);
    }
    if full {
        if !e.f.allows.is_empty() {
            field(s, st, "allows", &e.f.allows.join(", "), indent, w);
        }
        if let Some(sc) = &e.f.scaffold {
            field(s, st, "scaffold", sc, indent, w);
        }
        if e.f.gradable == Some(false) {
            field(
                s,
                st,
                "gradable",
                "no: only files, structure and the build are checked",
                indent,
                w,
            );
        }
        if !e.brief.trim().is_empty() {
            field(s, st, "brief", e.brief.trim(), indent, w);
        }
    }
}

fn project(s: &mut String, st: &Style, plan: &Plan, p: &Entry, full: bool, w: usize) {
    let stages = plan.stages(&p.id);
    let req: f64 = stages
        .iter()
        .filter(|x| !x.f.stretch)
        .filter_map(|x| x.f.hours)
        .sum();
    let extra: f64 = stages
        .iter()
        .filter(|x| x.f.stretch)
        .filter_map(|x| x.f.hours)
        .sum();
    let mut size = format!(
        "project, {} stages, about {} h",
        stages.len(),
        curriculum::round5(req)
    );
    if extra > 0.0 {
        size.push_str(&format!(" + {} h stretch", curriculum::round5(extra)));
    }
    let code = p.id.split('-').next().unwrap_or(&p.id);
    s.push_str(&format!(
        "\n{}  {}  {}\n",
        st.bold(code),
        st.bold(&p.title),
        st.dim(&format!("({size})"))
    ));
    if let Some(g) = &p.f.goal {
        s.push_str(&wrap(g, "  ", "  ", w));
    }
    if let Some(m) = &p.f.minimum {
        field(s, st, "minimum", m, "  ", w);
    }
    if !p.f.assumes.is_empty() {
        field(s, st, "assumes", &list(&p.f.assumes), "  ", w);
    }
    if full && !p.brief.trim().is_empty() {
        field(s, st, "scope", p.brief.trim(), "  ", w);
    }
    for stage in stages {
        unit(s, st, plan, stage, full, w);
    }
}

pub fn render(plan: &Plan, approval: &str, full: bool, st: &Style, w: usize) -> String {
    let m = &plan.meta;
    let mut s = format!("{}\n", st.bold(&m.title));
    let mut facts = Vec::new();
    if let Some(l) = &m.language {
        facts.push(l.clone());
    }
    if let Some(l) = &m.level {
        facts.push(l.clone());
    }
    if let (Some(h), Some(wk)) = (m.hours_per_week, m.weeks) {
        let plural = if wk == 1.0 { "" } else { "s" };
        facts.push(format!("{h} h/week for {wk} week{plural}"));
    }
    facts.push(approval.to_string());
    s.push_str(&st.dim(&facts.join(" · ")));
    s.push('\n');
    if let Some(e) = &m.environment {
        s.push_str(&wrap(&format!("environment: {e}"), "", "  ", w));
    }
    s.push_str(
        &curriculum::summary(plan)
            .trim_start_matches('\n')
            .replace(" (c/i column: concept / implementation)", ""),
    );
    let units = plan.units();
    let drills = units.iter().filter(|u| u.kind == Kind::Drill).count();
    let projects = plan
        .entries
        .iter()
        .filter(|e| e.kind == Kind::Project)
        .count();
    let stretch = units.iter().filter(|u| u.f.stretch).count();
    s.push_str(&st.dim(&format!(
        "{drills} drill(s), {projects} project(s), {} unit(s) in total, {stretch} stretch goal(s)\n",
        units.len()
    )));
    for e in &plan.entries {
        match e.kind {
            Kind::Drill => unit(&mut s, st, plan, e, full, w),
            Kind::Project => project(&mut s, st, plan, e, full, w),
            Kind::Stage | Kind::Note => {}
        }
    }
    s
}

pub fn cmd_show(raw: &[String]) -> Result<bool, String> {
    let a = Args::parse(raw, &["course"], &["full"])?;
    let course = a.course();
    let file = match a.positional.as_slice() {
        [f] => std::path::PathBuf::from(f),
        [] => course.join("curriculum.md"),
        _ => {
            return Err("usage: coursecrafter show [curriculum.md] [--course DIR] [--full]".into());
        }
    };
    let text = std::fs::read_to_string(&file).map_err(|e| format!("{}: {e}", file.display()))?;
    let plan = match curriculum::parse(&text) {
        Ok(p) => p,
        Err(problems) => {
            println!(
                "{} does not parse:\n{}",
                file.display(),
                curriculum::report(&problems)
            );
            return Ok(false);
        }
    };
    let st = Style::detect();
    let approved = std::fs::read_to_string(approval_path(&course)).unwrap_or_default();
    let approval = if approved.trim().is_empty() {
        st.yellow("not approved")
    } else if approved.trim() == text_hash(&text) {
        st.green("approved")
    } else {
        st.yellow("changed since approval")
    };
    print!("{}", render(&plan, &approval, a.has("full"), &st, width()));
    let problems = plan.validate();
    let errors = problems.iter().filter(|p| !p.warning).count();
    println!();
    if problems.is_empty() {
        println!("{}", st.green("Validator: no problem."));
    } else {
        let line = format!(
            "Validator: {errors} error(s), {} warning(s).",
            problems.len() - errors
        );
        println!(
            "{}",
            if errors > 0 {
                st.red(&line)
            } else {
                st.yellow(&line)
            }
        );
        print!("{}", curriculum::report(&problems));
    }
    if !a.has("full") {
        println!(
            "{}",
            st.dim("Briefs, Allowed lists and scaffolds: coursecrafter show --full")
        );
    }
    Ok(errors == 0)
}
