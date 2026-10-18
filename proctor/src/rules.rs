//! `RULES.md`: what every unit of a course expects without saying it. It is
//! written from a template and the course's data (never by an LLM), so it
//! states exactly what proctor checks.

use crate::course::CourseDir;
use crate::render::display_language;
use std::collections::BTreeSet;

pub const FILE: &str = "RULES.md";

/// The course's rigor level: `relaxed`, `standard` or `strict` (the default).
pub fn rigor(c: &CourseDir) -> &str {
    match c.course.rigor.as_deref().map(str::trim) {
        Some("relaxed") => "relaxed",
        Some("standard") => "standard",
        _ => "strict",
    }
}

const RELAXED: &str = "Hidden tests check the behaviour the units state and the cases their examples show. Random inputs stay within the ranges the units state.";

const STANDARD: &str = "Beyond what a unit states, hidden tests cover:

- **the usual edge cases**: empty input, a single element, zero, a missing trailing newline;
- **invalid input** the unit describes, handled without crashing;
- **random input** compared with a hidden reference, a few hundred cases per function or program.";

const STRICT: &str = "You have unlimited attempts, the lessons and the documentation: the work is expected to be complete, not merely to pass the examples. Beyond what a unit states, hidden tests cover:

- **every edge case**: empty input, a single element, zero, the limits of the types involved, text outside ASCII, a missing trailing newline;
- **adversarial input**: very long text or large counts, malformed input; a program never crashes, panics or hangs;
- **random input** compared with a hidden reference, a few hundred cases per function or program;
- **performance**: a reasonable algorithm is expected; one that is needlessly slow on large input fails on the time limit.";

pub fn rules_md(c: &CourseDir) -> String {
    let level = rigor(c);
    let mut s = format!(
        "# Rules of {}\n\nThese rules hold for every unit. A unit's instructions say what is specific to it; when they are silent, this page applies.\n",
        c.course.title.trim()
    );
    s.push_str(&format!(
        "\n## Rigor: {level}\n\n{}\n",
        match level {
            "relaxed" => RELAXED,
            "standard" => STANDARD,
            _ => STRICT,
        }
    ));
    s.push_str(
        "\nEvery hidden test follows from a unit's instructions, its examples or this page: nothing is graded that you could not have known. Each unit's **Think about** list points at the cases worth deciding about, without the answers.\n",
    );
    s.push_str(
        "\n## When the instructions are silent\n\n- Do what the obvious reading, or the standard tool of the same name, would do.\n- Never crash, panic or hang, whatever the input.\n- Report errors on standard error and, for a program, exit with a non-zero status.\n",
    );
    s.push_str(
        "\n## Output\n\n- Output is compared **exactly**: every character, space and newline counts.\n- Print nothing that was not asked for, on standard output or standard error.\n- A line of output ends with a newline, the last one included.\n",
    );

    // Code quality, per language actually used.
    let mut langs: BTreeSet<String> = BTreeSet::new();
    let mut quality = Vec::new();
    for u in &c.units {
        let lang = c.language(u);
        if !langs.insert(lang.clone()) {
            continue;
        }
        let mut parts = Vec::new();
        if let Some(flags) = c.lang_config(u).flags.filter(|f| !f.is_empty()) {
            let werror = flags.iter().any(|f| f == "-Werror");
            parts.push(format!(
                "compiled with `{}`{}",
                flags.join(" "),
                if werror {
                    ", so a warning fails the build"
                } else {
                    ""
                }
            ));
        }
        let statics: Vec<String> = c
            .static_checks(u)
            .iter()
            .map(|x| format!("`{}`", x.cmd))
            .collect();
        if !statics.is_empty() {
            parts.push(format!("{} must pass", statics.join(" and ")));
        }
        if !parts.is_empty() {
            quality.push(format!(
                "- **{}**: {}.",
                display_language(c, &lang),
                parts.join("; ")
            ));
        }
    }
    if !quality.is_empty() {
        s.push_str("\n## Code quality\n\n");
        s.push_str(&quality.join("\n"));
        s.push_str(
            "\n\nRun them yourself before `proctor check`: their warnings are errors here.\n",
        );
    }

    let secs = crate::engine::DEFAULT_TIMEOUT_MS as f64 / 1000.0;
    s.push_str(&format!(
        "\n## Limits\n\n- Each call or run is stopped after {secs:.0} s unless a unit says otherwise; a hang or a crash fails the test.\n- **Allowed** lists what your code may use besides your own code and the language's built-ins. An entry covers everything inside it; **Forbidden even so** removes items from it. Your own tests in `tests/` may use anything.\n- Randomized tests use a new seed on every run. A failure prints the seed, and `proctor check --seed <seed>` replays it.\n"
    ));
    if !c.course.projects.is_empty() {
        s.push_str(
            "\n## Projects\n\nA project's stages share one workspace. Each stage's check re-runs the tests of every earlier stage, so earlier behaviour must keep working.\n",
        );
    }
    s.push_str(
        "\n## When a check fails\n\n`proctor check` stops at the first failing step and prints what it expected and what it got. `proctor check --verbose` shows every test, and `proctor explain` shows what the tests generate and how many rounds they run.\n",
    );
    if let Some(conv) = c
        .course
        .conventions
        .as_deref()
        .filter(|t| !t.trim().is_empty())
    {
        s.push_str(&format!(
            "\n## Conventions of this course\n\n{}\n",
            conv.trim()
        ));
    }
    s.push_str(
        "\n## Fair play\n\nDocumentation, books, forums and search engines are fine. AI assistants are not: the point is that you can do it.\n",
    );
    s
}
