//! A course on disk: `<root>/.proctor/` holds everything hidden; the
//! student sees `subjects/`, `notes/`, `PROGRESS.md` and their workspaces.

use crate::gens::Lib;
use crate::journal::{self, Event};
use crate::unit::{Course, LangConfig, Project, StaticCheck, Unit};
use std::collections::BTreeMap;
use std::collections::BTreeSet;
use std::fs;
use std::path::{Path, PathBuf};

pub const HIDDEN: &str = ".proctor";

/// The folder's names before the grader was renamed, newest first.
const OLD_HIDDEN: &[&str] = &[".skillchecker", ".skillcheck"];

/// Renames an old hidden folder in `root` to `.proctor/`, and its
/// `taskmaster/` folder to `coursecrafter/`, once.
pub fn migrate(root: &Path) -> Result<(), String> {
    let new = root.join(HIDDEN);
    let rename = |old: &Path, new: &Path| {
        fs::rename(old, new).map_err(|e| {
            format!(
                "renaming {} to {}: {e}",
                strip_verbatim(old).display(),
                strip_verbatim(new).display()
            )
        })
    };
    if let Some(old) = OLD_HIDDEN
        .iter()
        .map(|o| root.join(o))
        .find(|o| o.join("course.yaml").is_file())
        && !new.exists()
    {
        rename(&old, &new)?;
        eprintln!(
            "Renamed {} to {} (the grader is now proctor).",
            strip_verbatim(&old).display(),
            strip_verbatim(&new).display()
        );
    }
    let (old_tm, new_tm) = (new.join("taskmaster"), new.join("coursecrafter"));
    if old_tm.is_dir() && !new_tm.exists() {
        rename(&old_tm, &new_tm)?;
    }
    Ok(())
}

pub struct CourseDir {
    pub root: PathBuf,
    pub course: Course,
    pub units: Vec<Unit>,
    pub lib: Lib,
}

fn read_yaml<T: serde::de::DeserializeOwned>(path: &Path) -> Result<T, String> {
    let text = fs::read_to_string(path).map_err(|e| format!("{}: {e}", path.display()))?;
    yaml_serde::from_str(&text).map_err(|e| format!("{}: {e}", path.display()))
}

impl CourseDir {
    /// Nearest ancestor of `start` (inclusive) holding `.proctor/course.yaml`.
    pub fn discover(start: &Path) -> Result<CourseDir, String> {
        let start = start
            .canonicalize()
            .map_err(|e| format!("{}: {e}", start.display()))?;
        let root = start
            .ancestors()
            .find(|d| {
                d.join(HIDDEN).join("course.yaml").is_file()
                    || OLD_HIDDEN
                        .iter()
                        .any(|o| d.join(o).join("course.yaml").is_file())
            })
            .ok_or(
                "not inside a PiscineBuilder course (no .proctor/course.yaml above this directory)",
            )?;
        CourseDir::load(root)
    }

    pub fn load(root: &Path) -> Result<CourseDir, String> {
        migrate(root)?;
        let hidden = root.join(HIDDEN);
        let course: Course = read_yaml(&hidden.join("course.yaml"))?;
        let mut units = Vec::new();
        let mut seen = BTreeSet::new();
        for id in &course.units {
            if !seen.insert(id.clone()) {
                return Err(format!("course.yaml lists `{id}` twice"));
            }
            let path = hidden.join("units").join(format!("{id}.yaml"));
            let unit: Unit = read_yaml(&path)?;
            if &unit.id != id {
                return Err(format!(
                    "{}: id is `{}`, expected `{id}`",
                    path.display(),
                    unit.id
                ));
            }
            if let Some(p) = &unit.project
                && !course.projects.contains_key(p)
            {
                return Err(format!("unit `{id}` belongs to unknown project `{p}`"));
            }
            units.push(unit);
        }
        let lib_path = hidden.join("lib.yaml");
        let lib = if lib_path.is_file() {
            read_yaml(&lib_path)?
        } else {
            Lib::new()
        };
        Ok(CourseDir {
            root: strip_verbatim(root),
            course,
            units,
            lib,
        })
    }

    pub fn hidden(&self) -> PathBuf {
        self.root.join(HIDDEN)
    }

    pub fn unit(&self, id: &str) -> Option<&Unit> {
        self.units.iter().find(|u| u.id == id)
    }

    pub fn index(&self, id: &str) -> Option<usize> {
        self.units.iter().position(|u| u.id == id)
    }

    pub fn project(&self, u: &Unit) -> Option<(&String, &Project)> {
        let p = u.project.as_ref()?;
        self.course.projects.get_key_value(p)
    }

    pub fn language(&self, u: &Unit) -> String {
        u.language
            .clone()
            .or_else(|| self.project(u).and_then(|(_, p)| p.language.clone()))
            .or_else(|| self.course.language.clone())
            .unwrap_or_else(|| "generic".into())
            .to_lowercase()
    }

    /// Built-in preset for the unit's language, overridden by the course's
    /// `languages:` entry.
    pub fn lang_config(&self, u: &Unit) -> LangConfig {
        let lang = self.language(u);
        let base = crate::defaults::preset(&lang);
        match self
            .course
            .languages
            .get(&lang)
            .or_else(|| self.course.languages.get(crate::defaults::canonical(&lang)))
        {
            Some(over) => base.with(over),
            None => base,
        }
    }

    /// Program command: the latest stage that sets `run:`, else the language's.
    pub fn run_cmd(&self, u: &Unit) -> Option<String> {
        self.chain(u)
            .iter()
            .rev()
            .find_map(|x| x.run.clone())
            .or_else(|| self.lang_config(u).run)
    }

    pub fn build_cmd(&self, u: &Unit) -> Option<String> {
        self.chain(u)
            .iter()
            .rev()
            .find_map(|x| x.build.clone())
            .or_else(|| self.lang_config(u).build)
    }

    /// Language env first, then each stage's own variables.
    pub fn env(&self, u: &Unit) -> BTreeMap<String, String> {
        let mut env = self.lang_config(u).env.unwrap_or_default();
        for x in self.chain(u) {
            env.extend(x.env.clone());
        }
        env
    }

    /// Language static checks, then the stages' (same name = replaced).
    pub fn static_checks(&self, u: &Unit) -> Vec<StaticCheck> {
        let mut out = self.lang_config(u).static_checks.unwrap_or_default();
        for x in self.chain(u) {
            for s in &x.static_checks {
                out.retain(|o| o.name != s.name);
                out.push(s.clone());
            }
        }
        out
    }

    pub fn import_patterns(&self, u: &Unit) -> Vec<String> {
        let mut out = self.lang_config(u).imports.unwrap_or_default();
        out.extend(self.chain(u).iter().flat_map(|x| x.imports.clone()));
        out
    }

    pub fn timeout_ms(&self, u: &Unit) -> Option<u64> {
        u.timeout_ms.or(self.lang_config(u).timeout_ms)
    }

    /// Run prefix: the latest stage's `wrap`, else the language's.
    pub fn wrap(&self, u: &Unit) -> Option<String> {
        self.chain(u)
            .iter()
            .rev()
            .find_map(|x| x.wrap.clone())
            .or_else(|| self.lang_config(u).wrap)
            .filter(|w| !w.trim().is_empty())
    }

    /// Support files (absolute), language ones then each stage's.
    pub fn support(&self, u: &Unit) -> Vec<PathBuf> {
        let mut rel = self.lang_config(u).support.unwrap_or_default();
        for x in self.chain(u) {
            rel.extend(x.support.clone());
        }
        let mut out: Vec<PathBuf> = Vec::new();
        for r in rel {
            let p = self.hidden().join(r);
            if !out.contains(&p) {
                out.push(p);
            }
        }
        out
    }
    pub fn workspace_name(&self, u: &Unit) -> String {
        u.project.clone().unwrap_or_else(|| u.id.clone())
    }

    pub fn workspace(&self, u: &Unit) -> PathBuf {
        self.root.join(self.workspace_name(u))
    }

    /// For a stage: every stage of its project up to and including it.
    /// For a drill: just the drill.
    pub fn chain<'a>(&'a self, u: &'a Unit) -> Vec<&'a Unit> {
        match &u.project {
            None => vec![u],
            Some(p) => {
                let mut out = Vec::new();
                for x in &self.units {
                    if x.project.as_ref() == Some(p) {
                        out.push(x);
                    }
                    if x.id == u.id {
                        break;
                    }
                }
                out
            }
        }
    }

    pub fn stage_number(&self, u: &Unit) -> usize {
        self.chain(u).len()
    }

    pub fn stages_of(&self, project: &str) -> Vec<&Unit> {
        self.units
            .iter()
            .filter(|u| u.project.as_deref() == Some(project))
            .collect()
    }

    pub fn ref_dir(&self, u: &Unit) -> PathBuf {
        self.hidden().join("refs").join(&u.id)
    }

    pub fn subject_rel(&self, u: &Unit) -> PathBuf {
        match &u.project {
            None => Path::new("subjects").join(format!("{}.md", u.id)),
            Some(p) => Path::new("subjects").join(p).join(format!("{}.md", u.id)),
        }
    }

    pub fn overview_rel(&self, project: &str) -> PathBuf {
        Path::new("subjects").join(project).join("00-overview.md")
    }

    pub fn state(&self) -> State {
        State::from_events(&journal::read(&self.root), self)
    }
}

/// `\\?\C:\x` -> `C:\x`, so paths print and pass to tools cleanly.
pub fn strip_verbatim(p: &Path) -> PathBuf {
    let s = p.to_string_lossy();
    match s.strip_prefix(r"\\?\") {
        Some(rest) if !rest.starts_with("UNC") => PathBuf::from(rest),
        _ => p.to_path_buf(),
    }
}

pub struct State {
    pub unlocked: BTreeSet<String>,
    pub passed: BTreeSet<String>,
    /// Stretch units the student chose to skip.
    pub skipped: BTreeSet<String>,
    /// First unlocked unit neither passed nor skipped, else the last unlocked.
    pub current: Option<String>,
}

impl State {
    pub fn from_events(events: &[Event], c: &CourseDir) -> State {
        let mut unlocked = BTreeSet::new();
        let mut passed = BTreeSet::new();
        let mut skipped = BTreeSet::new();
        for e in events {
            match e.ev.as_str() {
                "unlock" => {
                    unlocked.insert(e.unit.clone());
                }
                "pass" => {
                    passed.insert(e.unit.clone());
                }
                "skip" => {
                    skipped.insert(e.unit.clone());
                }
                _ => {}
            }
        }
        let current = c
            .units
            .iter()
            .find(|u| {
                unlocked.contains(&u.id) && !passed.contains(&u.id) && !skipped.contains(&u.id)
            })
            .or_else(|| c.units.iter().rev().find(|u| unlocked.contains(&u.id)))
            .map(|u| u.id.clone());
        State {
            unlocked,
            passed,
            skipped,
            current,
        }
    }

    /// Passed, or skipped as a stretch goal.
    pub fn done(&self, id: &str) -> bool {
        self.passed.contains(id) || self.skipped.contains(id)
    }

    /// The unit `next` would unlock, if the current one is done.
    pub fn next_locked<'a>(&self, c: &'a CourseDir) -> Option<&'a Unit> {
        c.units.iter().find(|u| !self.unlocked.contains(&u.id))
    }
}
