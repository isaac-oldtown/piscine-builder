use skillcheck::course::{CourseDir, State};
use skillcheck::engine::{self, Against, Engine, Kind, Options};
use skillcheck::gens;
use skillcheck::journal::{self, Event};
use skillcheck::report::{self, Style, Term};
use skillcheck::unit::Unit;
use skillcheck::{ask, progress, scaffold, selftest};
use std::path::Path;
use std::process::ExitCode;
use std::time::{SystemTime, UNIX_EPOCH};

const USAGE: &str = "\
skillcheck: levelup's grader. Works offline, never calls an LLM.

  skillcheck check [unit] [--seed HEX] [--verbose] [--no-prompt]
                               check your work (default: the current unit)
  skillcheck next              reveal the next unit once the current one passes
  skillcheck skip              skip the current unit if it is a stretch goal (optional)
  skillcheck status            where you are in the course
  skillcheck progress          regenerate and summarize PROGRESS.md
  skillcheck explain [unit]    show exactly what the checks run (generators, rounds)

  Course authors: skillcheck validate | skillcheck selftest [unit...]

Exit codes: 0 OK, 1 KO, 2 usage or course error. NO_COLOR and SKILLCHECK_ASCII are honoured.";

fn main() -> ExitCode {
    let args: Vec<String> = std::env::args().skip(1).collect();
    let res = match args.first().map(String::as_str) {
        None | Some("-h" | "--help" | "help") => {
            println!("{USAGE}");
            Ok(ExitCode::SUCCESS)
        }
        Some("check") => cmd_check(&args[1..]),
        Some("next" | "start") => cmd_next(&args[1..]),
        Some("skip") => cmd_skip(),
        Some("status") => cmd_status(),
        Some("progress") => cmd_progress(),
        Some("explain") => cmd_explain(&args[1..]),
        Some("validate") => cmd_validate(),
        Some("selftest") => cmd_selftest(&args[1..]),
        Some(other) => Err(format!("unknown command `{other}`\n\n{USAGE}")),
    };
    match res {
        Ok(code) => code,
        Err(e) => {
            eprintln!("error: {e}");
            ExitCode::from(2)
        }
    }
}

fn course() -> Result<CourseDir, String> {
    let cwd = std::env::current_dir().map_err(|e| e.to_string())?;
    CourseDir::discover(&cwd)
}

fn random_seed() -> u64 {
    let nanos = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|d| d.as_nanos() as u64)
        .unwrap_or(0);
    gens::Rng::new(nanos ^ ((std::process::id() as u64) << 32)).next_u64()
}

/// The unit to check: explicit id, else the workspace the cwd is in, else the current unit.
fn pick<'a>(c: &'a CourseDir, state: &State, explicit: Option<&str>) -> Result<&'a Unit, String> {
    if let Some(id) = explicit {
        let u = c
            .units
            .iter()
            .find(|u| u.id == id || u.id.split('-').next() == Some(id))
            .ok_or_else(|| format!("no unit `{id}` in this course"))?;
        if !state.unlocked.contains(&u.id) {
            return Err(format!("`{}` is still locked", u.id));
        }
        return Ok(u);
    }
    let cwd = std::env::current_dir().map_err(|e| e.to_string())?;
    let cwd = skillcheck::course::strip_verbatim(&cwd.canonicalize().unwrap_or(cwd));
    let in_ws = c
        .units
        .iter()
        .rev()
        .filter(|u| state.unlocked.contains(&u.id))
        .find(|u| {
            let ws = c.workspace(u);
            let ws = skillcheck::course::strip_verbatim(&ws.canonicalize().unwrap_or(ws));
            cwd.starts_with(&ws)
        });
    if let Some(u) = in_ws {
        // Inside a project workspace, prefer the first stage not passed yet.
        let wsn = c.workspace_name(u);
        let pending = c.units.iter().find(|x| {
            c.workspace_name(x) == wsn && state.unlocked.contains(&x.id) && !state.done(&x.id)
        });
        return Ok(pending.unwrap_or(u));
    }
    match &state.current {
        Some(id) => c
            .unit(id)
            .ok_or_else(|| format!("unknown current unit `{id}`")),
        None => Err("nothing unlocked yet: run `skillcheck next` to start".into()),
    }
}

fn record(c: &CourseDir, ev: &str, unit: &str) -> Result<(), String> {
    journal::append(
        &c.root,
        &Event {
            t: journal::now(),
            ev: ev.into(),
            unit: unit.into(),
            ..Default::default()
        },
    )
}

/// After a pass or a skip: offers the next unit. Stretch goals can be skipped
/// on the spot, in which case the one after is offered.
fn offer_next(root: &Path, after: Option<&str>, prompt: bool) -> Result<(), String> {
    loop {
        let c = CourseDir::load(root)?;
        let state = c.state();
        let style = Style::detect();
        let Some(next) = state.next_locked(&c) else {
            println!("{}", style.green("Course complete. Well played."));
            return Ok(());
        };
        let same_project = next.is_stage()
            && after
                .and_then(|a| c.unit(a))
                .is_some_and(|u| u.project == next.project);
        let label = if same_project { "stage" } else { "exercise" };
        if !prompt || !ask::interactive() {
            let hint = if next.stretch {
                " (a stretch goal: `skillcheck skip` skips it)"
            } else {
                ""
            };
            println!(
                "Run `skillcheck next` when you are ready for {}{hint}.",
                next.id
            );
            return Ok(());
        }
        if next.stretch {
            let options = vec![
                format!("Start {}", next.id),
                "Skip it: it is an optional stretch goal".to_string(),
                "Not now".to_string(),
            ];
            match ask::choose(
                &format!(
                    "The next {label}, {}, is a stretch goal. What now?",
                    next.id
                ),
                &options,
                0,
                None,
            ) {
                ask::Answer::Pick(0) => {
                    print_reveal(&c, next, &scaffold::unlock(&c, next)?);
                    return Ok(());
                }
                ask::Answer::Pick(1) => {
                    record(&c, "unlock", &next.id)?;
                    record(&c, "skip", &next.id)?;
                    println!("Skipped {}.", next.id);
                    progress::write(&CourseDir::load(root)?)?;
                    continue;
                }
                _ => {
                    println!("Run `skillcheck next` when you are ready for {}.", next.id);
                    return Ok(());
                }
            }
        }
        if ask::yes_no(
            &format!("Go to the next {label} ({})?", next.id),
            "Yes, unlock it now",
            "Not now",
            false,
        ) {
            print_reveal(&c, next, &scaffold::unlock(&c, next)?);
        } else {
            println!("Run `skillcheck next` when you are ready for {}.", next.id);
        }
        return Ok(());
    }
}

/// At the start of a new week, writes the review of the week that ended.
fn review_reminder(c: &CourseDir) {
    if let Ok(Some(path)) = progress::weekly_review(c, journal::now()) {
        let style = Style::detect();
        println!(
            "{} take five minutes for {path}: what you can do on your own, what is still unclear, whether the schedule holds.",
            style.bold("A week has ended:")
        );
    }
}

fn cmd_skip() -> Result<ExitCode, String> {
    let c = course()?;
    let state = c.state();
    let target = match state.current.as_ref().and_then(|id| c.unit(id)) {
        Some(u) if !state.done(&u.id) => u,
        // Current unit done: skipping applies to the next one, still locked.
        _ => state.next_locked(&c).ok_or("nothing left to skip")?,
    };
    if !target.stretch {
        return Err(format!(
            "`{}` is required; only units marked as stretch goals can be skipped",
            target.id
        ));
    }
    if !state.unlocked.contains(&target.id) {
        record(&c, "unlock", &target.id)?;
    }
    record(&c, "skip", &target.id)?;
    println!(
        "Skipped {} (stretch goal). You can still come back to it with `skillcheck check {}`.",
        target.id, target.id
    );
    progress::write(&CourseDir::load(&c.root)?)?;
    review_reminder(&c);
    offer_next(&c.root, Some(&target.id), true)?;
    Ok(ExitCode::SUCCESS)
}
fn cmd_check(args: &[String]) -> Result<ExitCode, String> {
    let mut seed = None;
    let mut verbose = false;
    let mut prompt = true;
    let mut explicit = None;
    let mut it = args.iter();
    while let Some(a) = it.next() {
        match a.as_str() {
            "--seed" => {
                let v = it.next().ok_or("--seed needs a value")?;
                seed = Some(
                    u64::from_str_radix(v.trim_start_matches("0x"), 16)
                        .map_err(|_| "--seed takes a hex number")?,
                );
            }
            "--verbose" | "-v" => verbose = true,
            "--no-prompt" => prompt = false,
            s if s.starts_with('-') => return Err(format!("unknown option {s}")),
            s => explicit = Some(s.to_string()),
        }
    }
    let c = course()?;
    let state = c.state();
    let u = pick(&c, &state, explicit.as_deref())?;
    let seed = seed.unwrap_or_else(random_seed);
    let opts = Options {
        seed,
        against: Against::Student,
        tests_only: false,
    };
    let mut term = Term {
        style: Style::detect(),
        verbose,
        quiet: false,
    };
    let started = std::time::Instant::now();
    let v = Engine::new(&c, u, &opts).check(&mut term)?;
    println!("{}", report::verdict_line(&v, seed, &term.style));
    let (cat, item, msg) = match &v.first_fail {
        Some((cat, i)) => (
            Some(cat.clone()),
            Some(i.name.clone()),
            Some(report::truncate(&i.detail, 300)),
        ),
        None => (None, None, None),
    };
    journal::append(
        &c.root,
        &Event {
            t: journal::now(),
            ev: "check".into(),
            unit: u.id.clone(),
            ok: Some(v.ok),
            passed: Some(v.passed),
            total: Some(v.total),
            cat,
            item,
            msg,
            seed: Some(format!("{seed:016x}")),
            ms: Some(started.elapsed().as_millis() as u64),
            warnings: Some(v.warnings),
        },
    )?;
    let newly_passed = v.ok && !state.passed.contains(&u.id);
    if newly_passed {
        journal::append(
            &c.root,
            &Event {
                t: journal::now(),
                ev: "pass".into(),
                unit: u.id.clone(),
                ..Default::default()
            },
        )?;
    }
    let c = CourseDir::load(&c.root)?;
    progress::write(&c)?;
    review_reminder(&c);
    if newly_passed {
        offer_next(&c.root, Some(&u.id), prompt)?;
    }
    Ok(if v.ok {
        ExitCode::SUCCESS
    } else {
        ExitCode::from(1)
    })
}

fn print_reveal(c: &CourseDir, u: &Unit, r: &scaffold::Revealed) {
    let style = Style::detect();
    println!("{} {}: {}", style.bold("Unlocked"), u.id, u.title);
    println!(
        "  instructions: {}",
        c.subject_rel(u).to_string_lossy().replace('\\', "/")
    );
    for f in &r.created {
        println!("  created {f}");
    }
    for (path, body) in &r.kept {
        println!(
            "  kept your {path}; add these if they are missing:\n{}",
            body.lines()
                .map(|l| format!("    {l}"))
                .collect::<Vec<_>>()
                .join("\n")
        );
    }
    if u.scaffold == skillcheck::unit::Scaffold::None {
        println!(
            "  no workspace was created: setting it up is part of the exercise ({}/)",
            c.workspace_name(u)
        );
    }
}

fn cmd_next(args: &[String]) -> Result<ExitCode, String> {
    let force = args.iter().any(|a| a == "--force");
    let c = course()?;
    let state = c.state();
    if let Some(cur) = &state.current
        && !state.done(cur)
        && !force
    {
        let stretch = c.unit(cur).is_some_and(|u| u.stretch);
        return Err(format!(
            "finish `{cur}` first: `skillcheck check` must print OK{}",
            if stretch {
                " (it is a stretch goal: `skillcheck skip` skips it)"
            } else {
                ""
            }
        ));
    }
    match state.next_locked(&c) {
        None => {
            println!("Nothing left to unlock: the course is complete.");
            Ok(ExitCode::SUCCESS)
        }
        Some(next) => {
            let r = scaffold::unlock(&c, next)?;
            print_reveal(&c, next, &r);
            let c = CourseDir::load(&c.root)?;
            progress::write(&c)?;
            review_reminder(&c);
            Ok(ExitCode::SUCCESS)
        }
    }
}

fn cmd_status() -> Result<ExitCode, String> {
    let c = course()?;
    let state = c.state();
    let style = Style::detect();
    println!(
        "{}  ({} / {} units passed)",
        style.bold(&c.course.title),
        state.passed.len(),
        c.units.len()
    );
    for u in &c.units {
        let mark = if state.passed.contains(&u.id) {
            style.green("passed  ")
        } else if state.skipped.contains(&u.id) {
            style.dim("skipped ")
        } else if state.unlocked.contains(&u.id) {
            style.yellow("current ")
        } else {
            style.dim("locked  ")
        };
        let shown = if state.unlocked.contains(&u.id) {
            format!(
                "{}: {}{}",
                u.id,
                u.title,
                if u.stretch { " (stretch)" } else { "" }
            )
        } else {
            style.dim("?")
        };
        println!("  {mark} {shown}");
    }
    let current = state.current.as_ref().and_then(|id| c.unit(id));
    if let Some(cur) = current.filter(|u| state.done(&u.id)) {
        match state.next_locked(&c) {
            Some(next) => println!(
                "\n{} is done. Run `skillcheck next` to unlock {}.",
                cur.id, next.id
            ),
            None => println!("\nCourse complete."),
        }
    } else if let Some(cur) = current {
        println!(
            "\nCurrent: {}  workspace {}/  instructions {}",
            cur.id,
            c.workspace_name(cur),
            c.subject_rel(cur).to_string_lossy().replace('\\', "/")
        );
    } else {
        println!("\nRun `skillcheck next` to start.");
    }
    Ok(ExitCode::SUCCESS)
}

fn cmd_progress() -> Result<ExitCode, String> {
    let c = course()?;
    progress::write(&c)?;
    let text = progress::render(&c, &journal::read(&c.root));
    let dashboard: Vec<&str> = text
        .lines()
        .skip_while(|l| !l.starts_with("## Dashboard"))
        .skip(4)
        .take_while(|l| l.starts_with('|'))
        .collect();
    for l in dashboard {
        let cells: Vec<&str> = l.trim_matches('|').split(" | ").map(str::trim).collect();
        if let [k, v] = cells.as_slice() {
            println!("  {k:<24} {v}");
        }
    }
    println!("\nFull report: {}", Path::new("PROGRESS.md").display());
    Ok(ExitCode::SUCCESS)
}

fn cmd_explain(args: &[String]) -> Result<ExitCode, String> {
    let c = course()?;
    let state = c.state();
    let u = pick(&c, &state, args.first().map(String::as_str))?;
    let chain = c.chain(u);
    let protos = engine::protos_for(&c, &chain)?;
    println!("{} ({}, target: {:?})", u.id, c.language(u), u.target);
    for p in &protos {
        println!("  function {}   checked as `{}`", p.name, p.norm);
    }
    for x in &chain {
        let tests = engine::tests_of(&c, x, &protos)?;
        if tests.is_empty() {
            continue;
        }
        println!(
            "\n{}{}",
            if x.id == u.id {
                "tests of "
            } else {
                "regression from "
            },
            x.id
        );
        for t in tests {
            match &t.kind {
                Kind::Check { .. } | Kind::Prog(_) => println!("  {}", t.name),
                Kind::Fuzz {
                    func,
                    params,
                    rounds,
                } => {
                    println!("  fuzz {func}: {rounds} rounds, compared with the reference");
                    for (n, g) in params {
                        println!("      {n}: {}", gens::describe(g));
                    }
                }
                Kind::FuzzProg {
                    argv,
                    stdin,
                    rounds,
                } => {
                    println!(
                        "  {}: {rounds} rounds, stdout and exit code compared with the reference",
                        t.name
                    );
                    if let Some(g) = argv {
                        println!("      argv: {}", gens::describe(g));
                    }
                    if let Some(g) = stdin {
                        println!("      stdin: {}", gens::describe(g));
                    }
                }
                Kind::Law {
                    vars,
                    checks,
                    rounds,
                    time_ms,
                } => {
                    println!(
                        "  {}: {rounds} rounds{}",
                        t.name,
                        time_ms
                            .map(|ms| format!(", {ms} ms per round"))
                            .unwrap_or_default()
                    );
                    for (n, g) in vars {
                        println!("      {n}: {}", gens::describe(g));
                    }
                    for (src, _) in checks {
                        println!("      check {src}");
                    }
                }
            }
        }
    }
    Ok(ExitCode::SUCCESS)
}

fn cmd_validate() -> Result<ExitCode, String> {
    let c = course()?;
    let problems = engine::validate(&c);
    if problems.is_empty() {
        println!("OK {} unit(s) parse cleanly", c.units.len());
        Ok(ExitCode::SUCCESS)
    } else {
        for p in &problems {
            println!("KO {p}");
        }
        Ok(ExitCode::from(1))
    }
}

fn cmd_selftest(args: &[String]) -> Result<ExitCode, String> {
    let c = course()?;
    let problems = engine::validate(&c);
    for p in &problems {
        println!("KO spec: {p}");
    }
    let ids: Vec<&Unit> = if args.is_empty() {
        c.units.iter().collect()
    } else {
        args.iter()
            .map(|a| c.unit(a).ok_or_else(|| format!("no unit `{a}`")))
            .collect::<Result<_, _>>()?
    };
    let seed = random_seed();
    let mut all_ok = problems.is_empty();
    let style = Style::detect();
    for u in ids {
        let r = selftest::unit(&c, u, seed);
        let reference = match &r.reference {
            Ok((p, t)) => style.green(&format!("reference OK {p}/{t}")),
            Err(e) => style.red(&format!("reference KO {}", report::truncate(e, 160))),
        };
        let vac = if r.vacuity_checked == 0 {
            String::new()
        } else if r.vacuous.is_empty() {
            style.green(&format!(
                "  every function caught on stubs ({})",
                r.vacuity_checked
            ))
        } else {
            style.red(&format!("  weak: {}", r.vacuous.join("; ")))
        };
        println!("{:<28} {reference}{vac}", u.id);
        for n in &r.notes {
            println!("{:<28} {}", "", style.yellow(n));
        }
        all_ok &= r.ok();
    }
    println!("seed {seed:016x}");
    Ok(if all_ok {
        ExitCode::SUCCESS
    } else {
        ExitCode::from(1)
    })
}
