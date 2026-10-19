//! Course self-test, used by coursecrafter's quality gate and by course
//! authors: the reference solution must pass every check, and every
//! function test must fail against empty stubs (otherwise it proves nothing).

use crate::course::CourseDir;
use crate::engine::{self, Against, Engine, Item, Options, Phase, Reporter, Status};
use crate::expr;
use crate::lang;
use crate::sig::Proto;
use crate::unit::{Target, Unit};
use std::collections::BTreeMap;
use std::fs;
use std::path::{Path, PathBuf};

pub struct Silent {
    pub log: String,
}

impl Reporter for Silent {
    fn start(&mut self, _: &Unit, _: u64, _: &str) {}
    fn phase_begin(&mut self, _: &str, _: bool) {}
    fn item(&mut self, _: &Item) {}
    fn phase_end(&mut self, _: &Phase) {}
    fn log(&mut self, text: &str) {
        self.log.push_str(text);
    }
}

pub struct UnitReport {
    pub unit: String,
    pub reference: Result<(usize, usize), String>,
    /// Weak spots: functions no test catches on an empty stub, tests that test nothing.
    pub vacuous: Vec<String>,
    pub vacuity_checked: usize,
    pub notes: Vec<String>,
}

impl UnitReport {
    pub fn ok(&self) -> bool {
        self.reference.is_ok() && self.vacuous.is_empty()
    }

    /// One JSON line, how the Linux container reports back.
    pub fn to_json(&self) -> serde_json::Value {
        let (passed, error) = match &self.reference {
            Ok((p, t)) => (Some([p, t]), None),
            Err(e) => (None, Some(e)),
        };
        serde_json::json!({
            "unit": self.unit,
            "passed": passed,
            "error": error,
            "vacuous": self.vacuous,
            "vacuity_checked": self.vacuity_checked,
            "notes": self.notes,
        })
    }

    pub fn from_json(v: &serde_json::Value) -> Option<UnitReport> {
        let strings = |k: &str| -> Vec<String> {
            v[k].as_array()
                .map(|a| {
                    a.iter()
                        .filter_map(|x| x.as_str().map(String::from))
                        .collect()
                })
                .unwrap_or_default()
        };
        let reference = match (v["passed"].as_array(), v["error"].as_str()) {
            (Some(pt), _) => Ok((
                pt.first()?.as_u64()? as usize,
                pt.get(1)?.as_u64()? as usize,
            )),
            (None, Some(e)) => Err(e.to_string()),
            (None, None) => return None,
        };
        Some(UnitReport {
            unit: v["unit"].as_str()?.to_string(),
            reference,
            vacuous: strings("vacuous"),
            vacuity_checked: v["vacuity_checked"].as_u64().unwrap_or(0) as usize,
            notes: strings("notes"),
        })
    }
}

/// Writes a workspace holding only mechanical stubs for `protos`. `module` is
/// the unit's `module:`, the Python module of undotted function names.
pub fn stub_workspace(
    lang: &str,
    protos: &[Proto],
    module: Option<&str>,
    dir: &Path,
    reference: &Path,
) -> Result<(), String> {
    let _ = fs::remove_dir_all(dir);
    fs::create_dir_all(dir).map_err(|e| e.to_string())?;
    let w = |rel: &str, body: &str| -> Result<(), String> {
        let p = dir.join(rel);
        if let Some(d) = p.parent() {
            fs::create_dir_all(d).map_err(|e| e.to_string())?;
        }
        fs::write(&p, body).map_err(|e| format!("{}: {e}", p.display()))
    };
    match lang {
        "c" => {
            let body = lang::stub_source(lang, protos).unwrap_or_default();
            w(
                "stubs.c",
                &format!(
                    "#include <stddef.h>\n#include <stdbool.h>\n#include <stdint.h>\n\n{body}"
                ),
            )?;
        }
        "python" => {
            let mut by_module: BTreeMap<String, Vec<Proto>> = BTreeMap::new();
            for p in protos {
                let module = p
                    .name
                    .rsplit_once('.')
                    .map(|(m, _)| m.to_string())
                    .or_else(|| module.map(str::to_string))
                    .unwrap_or_default();
                by_module.entry(module).or_default().push(p.clone());
            }
            for (module, ps) in by_module {
                let body = lang::stub_source(lang, &ps).unwrap_or_default();
                if module.is_empty() {
                    return Err("python stubs need dotted names or a `module:`".into());
                }
                let rel = format!("{}/__init__.py", module.replace('.', "/"));
                w(&rel, &body)?;
            }
        }
        "rust" => {
            let pkg = lang::cargo_package(reference).map_err(|e| e.msg)?;
            w(
                "Cargo.toml",
                &format!(
                    "[package]\nname = \"{pkg}\"\nversion = \"0.0.0\"\nedition = \"2024\"\n\n[workspace]\n"
                ),
            )?;
            w(
                "src/lib.rs",
                &lang::stub_source(lang, protos).unwrap_or_default(),
            )?;
        }
        other => return Err(format!("no stub generator for {other}")),
    }
    Ok(())
}

pub fn unit(c: &CourseDir, u: &Unit, seed: u64) -> UnitReport {
    let mut r = UnitReport {
        unit: u.id.clone(),
        reference: Err(String::new()),
        vacuous: Vec::new(),
        vacuity_checked: 0,
        notes: Vec::new(),
    };
    let ref_dir = c.ref_dir(u);
    r.reference = if !ref_dir.is_dir() {
        if u.has_tests() || !u.functions.is_empty() {
            Err(format!("no reference solution in .proctor/refs/{}/", u.id))
        } else {
            Ok((0, 0))
        }
    } else {
        let opts = Options {
            seed,
            against: Against::Reference,
            tests_only: false,
        };
        let mut silent = Silent { log: String::new() };
        match Engine::new(c, u, &opts).check(&mut silent) {
            Ok(v) if v.ok => Ok((v.passed, v.total)),
            Ok(v) => Err(match v.first_fail {
                Some((cat, item)) => format!("{cat} {}: {}", item.name, item.detail),
                None => "failed".into(),
            }),
            Err(e) => Err(e),
        }
    };
    let lang = c.language(u);
    if u.target != Target::Function || !u.has_tests() {
        return r;
    }
    if !lang::has_harness(&lang) {
        r.notes.push(format!("no stub check for {lang}"));
        return r;
    }
    let chain = c.chain(u);
    let protos = match engine::protos_for(c, &chain) {
        Ok(p) => p,
        Err(e) => {
            r.notes.push(e);
            return r;
        }
    };
    let dir: PathBuf = c
        .hidden()
        .join("cache")
        .join(format!("stubs-{}", u.id))
        .join("tree");
    if let Err(e) = stub_workspace(&lang, &protos, u.module.as_deref(), &dir, &ref_dir) {
        r.notes.push(format!("stubs: {e}"));
        return r;
    }
    // Tests may also call test-only helpers; the stubs cover only the student's functions.
    let mut callable = protos.clone();
    callable.extend(engine::helpers_for(c, &chain).unwrap_or_default());
    let tests = match engine::tests_of(c, u, &callable) {
        Ok(t) => t,
        Err(e) => {
            r.notes.push(e);
            return r;
        }
    };
    let known: Vec<&str> = protos.iter().map(|p| p.name.as_str()).collect();
    let called = |t: &engine::Test| -> Vec<String> {
        let mut names = Vec::new();
        match &t.kind {
            engine::Kind::Check { expr } => expr::calls(expr, &mut names),
            engine::Kind::Fuzz { func, .. } => names.push(func.clone()),
            engine::Kind::Law { checks, .. } => {
                checks.iter().for_each(|(_, e)| expr::calls(e, &mut names))
            }
            engine::Kind::Prog(_) | engine::Kind::FuzzProg { .. } => {}
        }
        names.retain(|n| known.contains(&n.as_str()));
        names
    };
    let opts = Options {
        seed,
        against: Against::Dir(dir),
        tests_only: true,
    };
    let mut silent = Silent { log: String::new() };
    match Engine::new(c, u, &opts).check(&mut silent) {
        Ok(v) => {
            let failed_on_stub: Vec<&str> = v
                .phases
                .iter()
                .filter(|p| p.name == "tests" && p.status != Status::Skip)
                .flat_map(|p| p.items.iter())
                .filter(|i| !i.ok)
                .map(|i| i.name.as_str())
                .collect();
            for t in &tests {
                let names = called(t);
                if names.is_empty()
                    && matches!(
                        t.kind,
                        engine::Kind::Check { .. } | engine::Kind::Law { .. }
                    )
                {
                    r.vacuous
                        .push(format!("`{}` calls no student function", t.name));
                }
            }
            for f in &u.functions {
                let calling: Vec<&engine::Test> = tests
                    .iter()
                    .filter(|t| called(t).contains(&f.name))
                    .collect();
                r.vacuity_checked += 1;
                if calling.is_empty() {
                    r.vacuous.push(format!("`{}` is never tested", f.name));
                } else if !calling
                    .iter()
                    .any(|t| failed_on_stub.contains(&t.name.as_str()))
                {
                    r.vacuous
                        .push(format!("no test of `{}` fails on an empty stub", f.name));
                }
            }
            if let Some(p) = v
                .phases
                .iter()
                .find(|p| p.name == "build" && p.status == Status::Fail)
            {
                r.notes.push(format!("stubs do not build: {}", p.summary));
            }
        }
        Err(e) => r.notes.push(e),
    }
    r
}
