//! End to end on the demo course: its specs validate, every unit passes the
//! check coursecrafter's build gate runs (the reference passes its own tests,
//! and the function tests fail on empty stubs), and runaway student work is
//! stopped. Units whose toolchain is not installed are skipped.

use proctor::course::CourseDir;
use proctor::engine::{Against, Engine, Item, Options};
use proctor::selftest::Silent;
use proctor::unit::Unit;
use proctor::{docker, engine, lang, run, selftest};
use std::fs;
use std::path::{Path, PathBuf};
use std::time::Duration;

fn demo() -> PathBuf {
    Path::new(env!("CARGO_MANIFEST_DIR")).join("../examples/demo-course")
}

/// A scratch copy of the demo course, removed when dropped.
struct Scratch(PathBuf);

impl Drop for Scratch {
    fn drop(&mut self) {
        let _ = fs::remove_dir_all(&self.0);
    }
}

/// A fresh copy of the demo course in the temp folder.
fn scratch(name: &str) -> Scratch {
    let s = Scratch(std::env::temp_dir().join(format!("proctor-{name}-{}", std::process::id())));
    let _ = fs::remove_dir_all(&s.0);
    copy_course(&demo(), &s.0);
    s
}

/// Copies the course, without a cache left by running it in place.
fn copy_course(from: &Path, to: &Path) {
    fs::create_dir_all(to).unwrap();
    for e in fs::read_dir(from).unwrap().flatten() {
        let (src, dst) = (e.path(), to.join(e.file_name()));
        if src.is_dir() {
            if e.file_name() != "cache" {
                copy_course(&src, &dst);
            }
        } else {
            fs::copy(&src, &dst).unwrap();
        }
    }
}

/// Why `u` cannot be checked on this machine, if it cannot.
fn missing_tool(c: &CourseDir, u: &Unit) -> Option<String> {
    let lang = c.language(u);
    let program = match lang.as_str() {
        "c" => return lang::cc().is_none().then(|| "no C compiler".into()),
        "python" => return lang::python().is_none().then(|| "no Python 3".into()),
        "rust" => "cargo".to_string(),
        "bash" | "sh" | "shell" => "bash".to_string(),
        _ => c
            .lang_config(u)
            .run
            .and_then(|r| run::split_words(&r).into_iter().next())
            .unwrap_or(lang),
    };
    (!run::exists(&lang::resolve(&program))).then(|| format!("no `{program}`"))
}

/// The tests phase of unit `id` checked against `files`, on its fixed cases
/// only, with time limit `limit`; `None` when its toolchain is missing.
fn fixed_cases(id: &str, limit: Duration, files: &[(&str, String)]) -> Option<Vec<Item>> {
    let s = scratch(id);
    let spec = s.0.join(".proctor/units").join(format!("{id}.yaml"));
    let yaml = fs::read_to_string(&spec).unwrap();
    let fixed = yaml.split("\nrandom:").next().unwrap();
    let limit = limit.as_millis();
    fs::write(&spec, format!("{fixed}\ntimeout_ms: {limit}\n")).unwrap();
    let c = CourseDir::load(&s.0).unwrap();
    let u = c.unit(id).unwrap();
    if let Some(why) = missing_tool(&c, u) {
        eprintln!("skipped {id}: {why}");
        return None;
    }
    let work = s.0.join("work");
    for (path, body) in files {
        let p = work.join(path);
        fs::create_dir_all(p.parent().unwrap()).unwrap();
        fs::write(p, body).unwrap();
    }
    let opts = Options {
        seed: 1,
        against: Against::Dir(work),
        tests_only: true,
    };
    let v = Engine::new(&c, u, &opts)
        .check(&mut Silent { log: String::new() })
        .unwrap();
    let tests = v.phases.into_iter().find(|p| p.name == "tests");
    let items = tests.map(|p| p.items).unwrap_or_default();
    assert!(!items.is_empty(), "{id}: no tests ran: {:?}", v.first_fail);
    Some(items)
}

#[test]
fn demo_course_passes_the_build_gate() {
    let scratch = scratch("demo");
    let c = CourseDir::load(&scratch.0).unwrap();
    assert_eq!(engine::validate(&c), Vec::<String>::new());
    let mut failures = Vec::new();
    let mut checked = 0;
    for u in &c.units {
        let skip = if docker::needed(u) {
            Some("needs the Linux container".to_string())
        } else {
            missing_tool(&c, u)
        };
        if let Some(why) = skip {
            eprintln!("skipped {}: {why}", u.id);
            continue;
        }
        checked += 1;
        let r = selftest::unit(&c, u, 1);
        if !r.ok() || r.notes.iter().any(|n| n.starts_with("stubs")) {
            failures.push(format!(
                "{}: reference {:?}, weak {:?}, notes {:?}",
                u.id, r.reference, r.vacuous, r.notes
            ));
        }
    }
    assert!(failures.is_empty(), "{}", failures.join("\n"));
    assert!(checked > 0, "no unit could be checked on this machine");
}

#[test]
fn a_program_flooding_its_output_is_stopped() {
    // Builtins only: a 640 KB line, printed forever.
    let flood = "s=xxxxxxxxxx\nfor i in 1 2 3 4 5 6 7 8 9 10 11 12 13 14 15 16; do s=$s$s; done\nwhile :; do printf '%s\\n' \"$s\"; done\n";
    let files = [("wc.sh", flood.to_string())];
    let Some(items) = fixed_cases("d02-word-count", Duration::from_secs(30), &files) else {
        return;
    };
    for i in items {
        assert!(!i.ok && i.detail.contains("MB of output"), "{}", i.detail);
    }
}

#[test]
fn a_function_printing_forever_times_out() {
    let cargo = demo().join(".proctor/refs/d03-longest-word/Cargo.toml");
    let lib = "pub fn longest_word(text: &str) -> Option<&str> {\n    loop {\n        println!(\"{text}\");\n    }\n}\n\npub fn word_count(text: &str) -> usize {\n    loop {\n        println!(\"{text}\");\n    }\n}\n";
    let files = [
        ("Cargo.toml", fs::read_to_string(cargo).unwrap()),
        ("src/lib.rs", lib.to_string()),
    ];
    let Some(items) = fixed_cases("d03-longest-word", Duration::from_millis(500), &files) else {
        return;
    };
    for i in items {
        assert!(!i.ok && i.detail.contains("timed out"), "{}", i.detail);
    }
}
