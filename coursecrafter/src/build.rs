//! `coursecrafter build`: curriculum -> course.
//!   pass 1: one contract per unit (and notes, and settings for unknown languages)
//!   pass 2: tests, generators, reference solution, scaffold; gated by proctor selftest
//! Each LLM reply is cached on disk by a hash of its request, so a re-run only
//! pays for what changed. Nothing that fails the gate is kept.

use crate::curriculum::{Entry, Kind, Plan};
use crate::llm::{self, Llm, Msg};
use crate::prompts;
use proctor::course::CourseDir;
use proctor::unit::{LangConfig, Target, Unit};
use proctor::{engine, lang, selftest, sig};
use std::collections::{BTreeMap, BTreeSet};
use std::fs;
use std::path::{Path, PathBuf};
use yaml_serde::{Mapping, Value as Yaml};

pub struct Options {
    pub retries: u32,
    pub tests: Option<bool>,
    pub only: Vec<String>,
    pub yes: bool,
}

pub struct Builder<'a> {
    pub root: PathBuf,
    pub plan: &'a Plan,
    pub llm: &'a Llm,
    pub opts: Options,
    cache_hits: u32,
    /// Set when the LLM could not be reached: the build stops instead of
    /// failing every remaining unit the same way.
    provider_down: bool,
    /// Generation rules plus the student environment: one stable system
    /// prompt for the whole run, so providers can cache it.
    system: String,
}

fn hidden(root: &Path) -> PathBuf {
    root.join(proctor::course::HIDDEN)
}

fn fnv(parts: &[&str]) -> String {
    let mut h: u64 = 0xcbf2_9ce4_8422_2325;
    for p in parts {
        for b in p.bytes().chain([0u8]) {
            h ^= b as u64;
            h = h.wrapping_mul(0x0100_0000_01b3);
        }
    }
    format!("{h:016x}")
}

fn write(path: &Path, body: &str) -> Result<(), String> {
    if let Some(d) = path.parent() {
        fs::create_dir_all(d).map_err(|e| format!("{}: {e}", d.display()))?;
    }
    fs::write(path, body).map_err(|e| format!("{}: {e}", path.display()))
}

fn s(v: &str) -> Yaml {
    Yaml::String(v.to_string())
}

fn seq(v: &[String]) -> Yaml {
    Yaml::Sequence(v.iter().map(|x| s(x)).collect())
}

pub fn has_preset(lang: &str) -> bool {
    matches!(lang, "c" | "python" | "rust" | "bash" | "sh" | "shell")
}

fn note_slug(id: &str) -> String {
    id.trim_start_matches("note-").to_string()
}

fn status(msg: &str) {
    eprintln!("{msg}");
}

impl<'a> Builder<'a> {
    pub fn new(
        root: PathBuf,
        plan: &'a Plan,
        llm: &'a Llm,
        opts: Options,
        env_block: &str,
    ) -> Builder<'a> {
        let langs: BTreeSet<String> = plan.units().iter().map(|u| plan.language(u)).collect();
        let langs: Vec<String> = langs.into_iter().collect();
        let system = format!("{}\n\n{env_block}", prompts::gen_system(&langs));
        Builder {
            root,
            plan,
            llm,
            opts,
            cache_hits: 0,
            provider_down: false,
            system,
        }
    }

    /// Named by `--only`: redone even if already built.
    fn explicit(&self, e: &Entry) -> bool {
        !self.opts.only.is_empty() && self.selected(e)
    }

    /// A unit whose correction suite already passed the gate: its spec holds
    /// tests and its reference exists (a failed unit is restored to neither).
    fn has_suite(&self, id: &str) -> bool {
        let tested = self
            .read_unit_map(id)
            .is_some_and(|m| m.contains_key(s("cases")) || m.contains_key(s("random")));
        tested && hidden(&self.root).join("refs").join(id).is_dir()
    }

    fn selected(&self, e: &Entry) -> bool {
        self.opts.only.is_empty()
            || self
                .opts
                .only
                .iter()
                .any(|o| o == &e.id || e.parent.as_deref() == Some(o))
    }

    /// Chat with on-disk caching keyed by the full request.
    fn chat(&mut self, label: &str, messages: &[Msg]) -> Result<String, String> {
        let mut parts: Vec<&str> = vec![&self.llm.model];
        parts.extend(messages.iter().map(|m| m.content.as_str()));
        let key = fnv(&parts);
        let path = hidden(&self.root)
            .join("coursecrafter")
            .join("cache")
            .join(format!("{label}-{key}.txt"));
        if let Ok(text) = fs::read_to_string(&path) {
            self.cache_hits += 1;
            return Ok(text);
        }
        let reply = self.llm.chat(messages).map_err(|e| {
            self.provider_down = true;
            format!(
                "{e}\nThe provider did not answer (stopped, quota or rate limit). Everything finished so far is saved: run the same command again to resume; finished replies are reused at no cost."
            )
        })?;
        write(&path, &reply)?;
        Ok(reply)
    }

    fn unit_path(&self, id: &str) -> PathBuf {
        hidden(&self.root).join("units").join(format!("{id}.yaml"))
    }

    fn read_unit_map(&self, id: &str) -> Option<Mapping> {
        let text = fs::read_to_string(self.unit_path(id)).ok()?;
        match yaml_serde::from_str(&text).ok()? {
            Yaml::Mapping(m) => Some(m),
            _ => None,
        }
    }

    fn existing_languages(&self) -> Mapping {
        let p = hidden(&self.root).join("course.yaml");
        let Ok(text) = fs::read_to_string(p) else {
            return Mapping::new();
        };
        match yaml_serde::from_str::<Yaml>(&text) {
            Ok(Yaml::Mapping(m)) => match m.get(s("languages")) {
                Some(Yaml::Mapping(l)) => l.clone(),
                _ => Mapping::new(),
            },
            _ => Mapping::new(),
        }
    }

    /// Writes course.yaml listing `units` (those whose files exist so far).
    fn write_course(&self, units: &[&Entry], languages: &Mapping) -> Result<(), String> {
        let meta = &self.plan.meta;
        let mut m = Mapping::new();
        m.insert(s("title"), s(&meta.title));
        if let Some(l) = &meta.language {
            m.insert(s("language"), s(&l.to_lowercase()));
        }
        if let Some(h) = meta.hours_per_week {
            m.insert(s("hours_per_week"), Yaml::Number(h.into()));
        }
        if let Some(w) = meta.weeks {
            m.insert(s("weeks"), Yaml::Number(w.into()));
        }
        m.insert(
            s("units"),
            seq(&units.iter().map(|u| u.id.clone()).collect::<Vec<_>>()),
        );
        let mut projects = Mapping::new();
        for p in self.plan.entries.iter().filter(|e| e.kind == Kind::Project) {
            let mut pm = Mapping::new();
            pm.insert(s("title"), s(&p.title));
            pm.insert(s("goal"), s(p.f.goal.as_deref().unwrap_or_default()));
            if let Some(min) = p.f.minimum.as_deref() {
                pm.insert(s("minimum"), s(min));
            }
            pm.insert(s("language"), s(&self.plan.language(p)));
            if let Some(d) = p.f.difficulty {
                pm.insert(s("difficulty"), Yaml::Number((d as u64).into()));
            }
            pm.insert(s("prereqs"), seq(&self.plan.prereqs(p)));
            let mut concepts: Vec<String> = Vec::new();
            for st in self.plan.stages(&p.id) {
                for c in &st.f.introduces {
                    if !concepts.contains(c) {
                        concepts.push(c.clone());
                    }
                }
            }
            pm.insert(s("concepts"), seq(&concepts));
            projects.insert(s(&p.id), Yaml::Mapping(pm));
        }
        m.insert(s("projects"), Yaml::Mapping(projects));
        if !languages.is_empty() {
            m.insert(s("languages"), Yaml::Mapping(languages.clone()));
        }
        let text = yaml_serde::to_string(&Yaml::Mapping(m)).map_err(|e| e.to_string())?;
        write(&hidden(&self.root).join("course.yaml"), &text)
    }

    fn language_info(&self, lang: &str, languages: &Mapping) -> String {
        match lang {
            "c" => "c (preset): compiled with cc -std=c11 -Wall -Wextra -Werror; function target available.".into(),
            "python" => "python (preset): Python 3; function target available; program target needs run: e.g. \"python -m pkg\" or \"python main.py\".".into(),
            "rust" => "rust (preset): cargo build, cargo fmt --check, cargo clippy -D warnings; function target available (library crate). Program target runs the built binary.".into(),
            "bash" | "sh" | "shell" => "bash (preset): bash -n syntax check; program target only; set run: e.g. \"bash script.sh\".".into(),
            other => {
                let block = languages
                    .get(s(other))
                    .and_then(|v| yaml_serde::to_string(v).ok())
                    .unwrap_or_default();
                format!("{other} (course settings, program target only):\n```yaml\n{block}```")
            }
        }
    }

    pub fn run(&mut self) -> Result<bool, String> {
        let h = hidden(&self.root);
        for d in ["units", "refs", "notes", "scaffold", "coursecrafter/cache"] {
            fs::create_dir_all(h.join(d)).map_err(|e| e.to_string())?;
        }
        let mut languages = self.existing_languages();
        let mut needed: BTreeSet<String> = BTreeSet::new();
        for u in self.plan.units() {
            needed.insert(self.plan.language(u));
        }
        for lang_name in needed {
            if has_preset(&lang_name) || languages.contains_key(s(&lang_name)) {
                continue;
            }
            status(&format!("[language] {lang_name}: writing course settings"));
            let block = self.language_settings(&lang_name)?;
            languages.insert(s(&lang_name), block);
        }

        // Pass 1: contracts.
        let units: Vec<&Entry> = self.plan.units();
        for (i, u) in units.iter().enumerate() {
            // Existing contracts are kept (rewriting one would drop its tests);
            // `--only` redoes the named units.
            if self.unit_path(&u.id).is_file() && !self.explicit(u) {
                self.sync_contract(u)?;
                continue;
            }
            status(&format!("[contract {}/{}] {}", i + 1, units.len(), u.id));
            let m = self.contract(u, &units[..i], &languages)?;
            let text = yaml_serde::to_string(&Yaml::Mapping(m)).map_err(|e| e.to_string())?;
            write(&self.unit_path(&u.id), &text)?;
            for n in self.plan.notes_for(&u.id) {
                let path = h.join("notes").join(format!("{}.md", note_slug(&n.id)));
                if path.is_file() && !self.explicit(u) {
                    continue;
                }
                status(&format!("[note] {}", n.id));
                let body = self.note(n)?;
                write(&path, &body)?;
            }
        }
        self.write_course(&units, &languages)?;
        status(&format!(
            "contracts written for {} unit(s). {}",
            units.len(),
            self.llm.usage_line()
        ));

        let want_tests = match self.opts.tests {
            Some(t) => t,
            None if self.opts.yes => true,
            None => crate::ask_yes(
                "Generate the correction suite (tests and hidden reference solutions)?",
            ),
        };
        if !want_tests {
            status(
                "Skipped the correction suite: proctor will check structure, prototypes, allowed and the build only. Run `coursecrafter build --tests` later to add it.",
            );
            self.refresh_unlocked();
            return Ok(true);
        }

        // Pass 2: tests + reference, unit by unit, each gated.
        let linux_pending = !cfg!(target_os = "linux")
            && units.iter().any(|u| {
                self.selected(u)
                    && self.plan.platform(u).is_some()
                    && (!self.has_suite(&u.id) || self.explicit(u))
            });
        if linux_pending {
            // Linux units are gated in a container: fail early, not as rejected attempts.
            proctor::docker::image()
                .map_err(|e| format!("{e}\n(the Linux units are tested in a Docker container)"))?;
        }
        let mut failed: Vec<String> = Vec::new();
        let mut waiting: Vec<String> = Vec::new();
        let mut done_before = 0;
        for (i, u) in units.iter().enumerate() {
            if !self.selected(u) {
                continue;
            }
            if u.f.gradable == Some(false) {
                status(&format!(
                    "[tests {}/{}] {}: not gradable, skipped",
                    i + 1,
                    units.len(),
                    u.id
                ));
                continue;
            }
            if self.has_suite(&u.id) && !self.explicit(u) {
                done_before += 1;
                continue;
            }
            // A stage builds on the previous stage's reference and must pass its
            // tests: without them it would be written blind and fail for nothing.
            if let Some(prev) = self.previous_stage(u)
                && prev.f.gradable != Some(false)
                && !self.has_suite(&prev.id)
            {
                status(&format!(
                    "[tests {}/{}] {}: waits for {}, which has no correction suite yet",
                    i + 1,
                    units.len(),
                    u.id,
                    prev.id
                ));
                waiting.push(u.id.clone());
                continue;
            }
            status(&format!("[tests {}/{}] {}", i + 1, units.len(), u.id));
            if let Err(e) = self.tests(u, &units[..=i], &languages) {
                if self.provider_down {
                    self.write_course(&units, &languages)?;
                    return Err(e);
                }
                status(&format!("  FAILED {}: {e}", u.id));
                failed.push(u.id.clone());
            }
        }
        if done_before > 0 {
            status(&format!(
                "{done_before} unit(s) already had a correction suite and were kept (`--only <id>` redoes one)"
            ));
        }
        self.write_course(&units, &languages)?;
        status(&format!(
            "{} ({} cached repl{})",
            self.llm.usage_line(),
            self.cache_hits,
            if self.cache_hits == 1 { "y" } else { "ies" }
        ));
        if !failed.is_empty() {
            status(&format!(
                "{} unit(s) have no correction suite because they never passed the gate: {}. Run `coursecrafter build --only <id>` to retry.",
                failed.len(),
                failed.join(", ")
            ));
        }
        if !waiting.is_empty() {
            status(&format!(
                "{} stage(s) were not built because an earlier stage of their project has no suite: {}. They are built once it passes.",
                waiting.len(),
                waiting.join(", ")
            ));
        }
        self.refresh_unlocked();
        Ok(failed.is_empty() && waiting.is_empty())
    }

    /// Units the student already unlocked get their instructions rendered
    /// again from the new specs; workspaces and notes are never overwritten.
    fn refresh_unlocked(&self) {
        let Ok(c) = CourseDir::load(&self.root) else {
            return;
        };
        let state = c.state();
        let mut n = 0;
        for u in c.units.iter().filter(|u| state.unlocked.contains(&u.id)) {
            if proctor::scaffold::reveal(&c, u).is_ok() {
                n += 1;
            }
        }
        if n > 0 {
            status(&format!(
                "refreshed the instructions of {n} unlocked unit(s) in subjects/"
            ));
        }
    }

    fn previous_stage(&self, u: &Entry) -> Option<&'a Entry> {
        let stages = self.plan.stages(u.parent.as_deref()?);
        let pos = stages.iter().position(|x| x.id == u.id)?;
        pos.checked_sub(1).map(|p| stages[p])
    }

    fn language_settings(&mut self, lang_name: &str) -> Result<Yaml, String> {
        let mut messages = vec![
            Msg::system(self.system.clone()),
            Msg::user(prompts::language_request(lang_name)),
        ];
        for _ in 0..=self.opts.retries {
            let reply = self.chat(&format!("lang-{lang_name}"), &messages)?;
            let problem = match llm::yaml_block(&reply, "language") {
                None => "missing <language> block".to_string(),
                Some(y) => match yaml_serde::from_str::<LangConfig>(&y) {
                    Err(e) => format!("not valid settings: {e}"),
                    Ok(cfg) if cfg.run.is_none() => "settings need `run`".into(),
                    Ok(cfg) => {
                        let bad = cfg
                            .imports
                            .iter()
                            .flatten()
                            .find(|p| regex::Regex::new(p).is_err());
                        match bad {
                            Some(p) => format!("bad regex `{p}`"),
                            None => return yaml_serde::from_str(&y).map_err(|e| e.to_string()),
                        }
                    }
                },
            };
            messages.push(Msg::assistant(reply));
            messages.push(Msg::user(prompts::feedback(
                &problem,
                "the <language> block",
            )));
        }
        Err(format!("could not get usable settings for {lang_name}"))
    }

    fn note(&mut self, n: &Entry) -> Result<String, String> {
        let messages = vec![
            Msg::system(self.system.clone()),
            Msg::user(prompts::note_request(&n.title, &n.f.teaches, &n.brief)),
        ];
        let reply = self.chat(&format!("note-{}", n.id), &messages)?;
        let body = llm::block(&reply, "note").unwrap_or(reply);
        Ok(body.replace(['\u{2014}', '\u{2013}'], ":") + "\n")
    }

    fn context(&self, earlier: &[&Entry]) -> String {
        let mut s = format!("Course: {}", self.plan.meta.title);
        if let Some(l) = &self.plan.meta.level {
            s.push_str(&format!(" (level: {l})"));
        }
        s.push('\n');
        for e in earlier {
            let funcs = self
                .read_unit_map(&e.id)
                .and_then(|m| m.get(s_key("functions")).cloned())
                .and_then(|f| match f {
                    Yaml::Sequence(items) => Some(
                        items
                            .iter()
                            .filter_map(|i| i.get("sig").and_then(Yaml::as_str).map(str::to_string))
                            .collect::<Vec<_>>()
                            .join("; "),
                    ),
                    _ => None,
                })
                .unwrap_or_default();
            s.push_str(&format!(
                "- {}: {}. introduces: {}{}\n",
                e.id,
                e.title,
                e.f.introduces.join(", "),
                if funcs.is_empty() {
                    String::new()
                } else {
                    format!(". functions: {funcs}")
                }
            ));
        }
        s
    }

    fn entry_text(&self, u: &Entry) -> String {
        let f = &u.f;
        let mut s = format!(
            "id: {}\ntitle: {}\ngoal: {}\ndifficulty: {}\n",
            u.id,
            u.title,
            f.goal.as_deref().unwrap_or_default(),
            f.difficulty.unwrap_or(1)
        );
        s.push_str(&format!("language: {}\n", self.plan.language(u)));
        for (k, v) in [
            ("introduces", &f.introduces),
            ("assumes", &f.assumes),
            ("allows", &f.allows),
        ] {
            if !v.is_empty() {
                s.push_str(&format!("{k}: [{}]\n", v.join(", ")));
            }
        }
        if let Some(t) = &f.target {
            s.push_str(&format!("target: {t} (fixed)\n"));
        }
        if let Some(d) = f.done_when.as_deref().filter(|d| !d.trim().is_empty()) {
            s.push_str(&format!("done_when: {d}\n"));
        }
        if f.stretch {
            s.push_str("stretch: true (optional unit; the student may skip it)\n");
        }
        if self.plan.platform(u).is_some() {
            s.push_str("platform: linux (built, run and tested on Linux: Ubuntu 24.04, /bin/sh is dash, GNU coreutils)\n");
        }
        if f.guided {
            s.push_str(&format!(
                "guided: true. The student has never used {lang}. Also send a <lesson>...</lesson> block after the <unit> block (plain markdown, NOT inside the YAML): a \
                 walkthrough (headings ###, no ## headings, at most about 150 lines) that takes them by the hand, in \
                 numbered steps with the exact commands and the expected output: check the toolchain is installed (and \
                 where to get it if not), create the workspace and the files the contract expects, write a first program \
                 and explain every line of it, build and run it, read a compiler error on purpose, then a short tour of \
                 the syntax this unit introduces with small examples, then run `proctor check`. The walkthrough may \
                 show complete code for the first program; for the rest of the requirements it explains the syntax with \
                 different examples and leaves the solution to the student.\n",
                lang = self.plan.language(u)
            ));
        } else if !f.introduces.is_empty() {
            s.push_str(&format!(
                "This unit introduces: {}. The student has not met these yet, so also send a <lesson>...</lesson> block \
                 after the <unit> block (plain markdown, NOT inside the YAML; headings ###, no ## headings, about 40 to 100 \
                 lines) that teaches them before the exercise: for each concept, what it is and why it matters, the exact \
                 syntax with a small runnable example that is NOT part of the solution, the output of that example, and \
                 the usual pitfall. Explain every piece of syntax the requirements rely on (a format spec such as {{:>4}}, \
                 a type, a macro, an operator rule such as integer division) unless an earlier unit introduced it. End \
                 with a pointer to the official documentation for each concept (a chapter of the language's book or its \
                 standard library docs).\n",
                f.introduces.join(", ")
            ));
        }
        let notes: Vec<String> = self
            .plan
            .notes_for(&u.id)
            .iter()
            .map(|n| format!("{} (teaches {})", n.title, n.f.teaches.join(", ")))
            .collect();
        if !notes.is_empty() {
            s.push_str(&format!("revealed with notes: {}\n", notes.join("; ")));
        }
        if !u.brief.is_empty() {
            s.push_str(&format!("brief: {}\n", u.brief));
        }
        s
    }

    fn project_text(&self, u: &Entry) -> String {
        let Some(pid) = &u.parent else {
            return String::new();
        };
        let Some(p) = self.plan.entry(pid) else {
            return String::new();
        };
        let mut s = format!(
            "# Project {}: {}\nGoal: {}\nMinimum viable result (the required stages deliver exactly this): {}\nStages:\n",
            p.id,
            p.title,
            p.f.goal.as_deref().unwrap_or_default(),
            p.f.minimum.as_deref().unwrap_or("not stated")
        );
        for st in self.plan.stages(pid) {
            let mark = if st.id == u.id { "  <- this one" } else { "" };
            s.push_str(&format!(
                "- {}: {}: {}{mark}\n",
                st.id,
                st.title,
                st.f.goal.as_deref().unwrap_or_default()
            ));
        }
        s.push_str("All stages share one workspace; each stage builds on the previous one.\n\n");
        s
    }

    /// A kept contract takes the plan's fixed fields (platform, hours,
    /// stretch...) again, so editing those in the plan needs no regeneration.
    fn sync_contract(&self, u: &Entry) -> Result<(), String> {
        let Some(old) = self.read_unit_map(&u.id) else {
            return Ok(());
        };
        let mut m = self.merge_contract(u, &old);
        for k in ["cases", "random"] {
            if let Some(v) = old.get(s(k)) {
                m.insert(s(k), v.clone());
            }
        }
        if m != old {
            let text = yaml_serde::to_string(&Yaml::Mapping(m)).map_err(|e| e.to_string())?;
            write(&self.unit_path(&u.id), &text)?;
        }
        Ok(())
    }

    /// Fixed fields first, then the model's contract.
    fn merge_contract(&self, u: &Entry, model: &Mapping) -> Mapping {
        let mut m = Mapping::new();
        m.insert(s("id"), s(&u.id));
        m.insert(s("title"), s(&u.title));
        if let Some(p) = &u.parent {
            m.insert(s("project"), s(p));
        }
        m.insert(s("language"), s(&self.plan.language(u)));
        if let Some(d) = u.f.difficulty {
            m.insert(s("difficulty"), Yaml::Number((d as u64).into()));
        }
        if let Some(d) = u.f.concept_difficulty {
            m.insert(s("concept_difficulty"), Yaml::Number((d as u64).into()));
        }
        if let Some(h) = u.f.hours {
            m.insert(s("hours"), Yaml::Number(h.into()));
        }
        if u.parent.is_none() {
            m.insert(s("prereqs"), seq(&self.plan.prereqs(u)));
        }
        m.insert(s("concepts"), seq(&u.f.introduces));
        m.insert(s("introduces"), seq(&u.f.introduces));
        m.insert(s("assumes"), seq(&u.f.assumes));
        let notes: Vec<String> = self
            .plan
            .notes_for(&u.id)
            .iter()
            .map(|n| note_slug(&n.id))
            .collect();
        if !notes.is_empty() {
            m.insert(s("notes"), seq(&notes));
        }
        m.insert(
            s("scaffold"),
            s(u.f.scaffold.as_deref().unwrap_or("minimal")),
        );
        if u.f.stretch {
            m.insert(s("stretch"), Yaml::Bool(true));
        }
        if let Some(p) = self.plan.platform(u) {
            m.insert(s("platform"), s(&p));
        }
        if u.f.guided {
            m.insert(s("guided"), Yaml::Bool(true));
        }
        // Plan keys the model may echo back; they are not part of a unit spec.
        let plan_only = [
            "done_when",
            "guided",
            "kind",
            "gradable",
            "allows",
            "teaches",
            "with",
            "minimum",
        ];
        let fixed = [
            "platform",
            "id",
            "title",
            "project",
            "language",
            "difficulty",
            "prereqs",
            "concepts",
            "introduces",
            "assumes",
            "notes",
            "scaffold",
            "stretch",
            "concept_difficulty",
            "hours",
            "cases",
            "random",
        ];
        if let Some(t) = &u.f.target {
            m.insert(s("target"), s(t));
        }
        for (k, v) in model {
            let key = k.as_str().unwrap_or_default();
            if fixed.contains(&key)
                || plan_only.contains(&key)
                || (key == "target" && u.f.target.is_some())
            {
                continue;
            }
            m.insert(k.clone(), v.clone());
        }
        // How the program runs is decided here, not by the model: C and Rust
        // run what they built, and a project keeps its first stage's command.
        if matches!(self.plan.language(u).as_str(), "c" | "rust") {
            m.remove(s("run"));
            m.remove(s("build"));
        } else if let Some(first) = u
            .parent
            .as_deref()
            .and_then(|p| self.plan.stages(p).first().map(|f| f.id.clone()))
            .filter(|f| *f != u.id)
            && let Some(run) = self
                .read_unit_map(&first)
                .and_then(|f| f.get(s("run")).cloned())
        {
            m.insert(s("run"), run);
        }
        m
    }

    /// Mechanical checks on a contract; problems go back to the model.
    fn check_contract(&self, u: &Entry, m: &Mapping, languages: &Mapping) -> Vec<String> {
        let text = match yaml_serde::to_string(&Yaml::Mapping(m.clone())) {
            Ok(t) => t,
            Err(e) => return vec![e.to_string()],
        };
        let unit: Unit = match yaml_serde::from_str(&text) {
            Ok(u) => u,
            Err(e) => return vec![format!("the <unit> YAML does not match the schema: {e}")],
        };
        let lang_name = self.plan.language(u);
        let mut p = Vec::new();
        if unit.goal.trim().is_empty() {
            p.push("`goal` is empty".into());
        }
        if unit.target == Target::Function && !lang::has_harness(&lang_name) {
            p.push(format!(
                "target: function is not available for {lang_name}; use target: program"
            ));
        }
        for f in &unit.functions {
            match sig::parse(&lang_name, &f.sig, &f.name) {
                Err(e) => p.push(format!("function `{}`: {e}", f.name)),
                Ok(proto) if unit.target == Target::Function => {
                    if let Err(e) = lang::harness_supports(&lang_name, &proto) {
                        p.push(e);
                    }
                    if lang_name == "python" && !f.name.contains('.') && unit.module.is_none() {
                        p.push(format!("python function `{}` needs a dotted name (module.function) or a `module:`", f.name));
                    }
                }
                Ok(_) => {}
            }
        }
        if unit.target == Target::Function && !unit.functions.is_empty() && unit.stubs.is_none() {
            p.push(
                "a function target needs `stubs:` (where the empty functions are written)".into(),
            );
        }
        if let Some(st) = &unit.stubs {
            let fits = unit.files.is_empty()
                || unit.files.iter().any(|r| {
                    r.rule != proctor::unit::Rule::Forbidden
                        && proctor::structure::glob(&r.path, st)
                });
            if !fits {
                p.push(format!(
                    "stubs path `{st}` is not allowed by the files rules"
                ));
            }
        }
        let lang_run = languages
            .get(s(&lang_name))
            .and_then(|l| l.get("run"))
            .is_some();
        if unit.target == Target::Program
            && unit.run.is_none()
            && !lang_run
            && !matches!(lang_name.as_str(), "c" | "rust")
        {
            p.push(format!("a program target in {lang_name} needs `run:`"));
        }
        if unit.requirements.len() > 8 {
            p.push(
                "too many requirements (keep at most 6): split the unit or merge bullets".into(),
            );
        }
        let lesson_lines = unit
            .lesson
            .as_deref()
            .map_or(0, |l| l.lines().filter(|x| !x.trim().is_empty()).count());
        if !u.f.guided && !u.f.introduces.is_empty() && lesson_lines < 8 {
            p.push("this unit introduces new concepts, so it needs a <lesson> block teaching them (see the request)".into());
        }
        if unit.target == Target::Program
            && unit.example.as_deref().is_none_or(|e| e.trim().is_empty())
        {
            p.push("a program target needs `example`: the exact output of one typical run, as a block string (with the command and its input when it takes some)".into());
        }
        if u.f.guided
            && unit
                .lesson
                .as_deref()
                .is_none_or(|l| l.lines().filter(|x| !x.trim().is_empty()).count() < 20)
        {
            p.push("a guided unit needs `lesson`: a step-by-step walkthrough in markdown (see the request)".into());
        }
        let all_text = text.to_lowercase();
        if text.contains('\u{2014}') || text.contains('\u{2013}') {
            p.push("never use long dashes; use a colon or a new sentence".into());
        }
        // The student's commands may be named; the hidden parts may not.
        if all_text.contains("harness") || all_text.contains("reference solution") {
            p.push("the contract must not reveal the hidden tests, the reference solution or how the checks work".into());
        }
        if let Some(r) = &unit.run
            && (r.contains("target/") || r.contains("target\\"))
        {
            p.push(format!(
                "`run: {r}` points into a build folder, which proctor places elsewhere: remove `run` (a Rust or C program runs as built) or run it through its interpreter"
            ));
        }
        p
    }

    fn contract(
        &mut self,
        u: &Entry,
        earlier: &[&Entry],
        languages: &Mapping,
    ) -> Result<Mapping, String> {
        let req = prompts::contract_request(
            &self.context(earlier),
            &self.entry_text(u),
            &self.project_text(u),
            &self.language_info(&self.plan.language(u), languages),
        );
        let mut messages = vec![Msg::system(self.system.clone()), Msg::user(req)];
        let mut last = String::new();
        for _ in 0..=self.opts.retries {
            let reply = self.chat(&format!("contract-{}", u.id), &messages)?;
            let problems: Vec<String> = match llm::yaml_block(&reply, "unit") {
                None => vec!["missing <unit> block".into()],
                Some(y) => match llm::parse_yaml(&drop_fixed_keys(&y)) {
                    Ok(Yaml::Mapping(mut model)) => {
                        // Markdown with code blocks does not survive inside YAML: it comes apart.
                        if let Some(l) =
                            llm::block(&reply, "lesson").filter(|l| !l.trim().is_empty())
                        {
                            model.insert(s("lesson"), s(l.trim()));
                        }
                        let m = self.merge_contract(u, &model);
                        let p = self.check_contract(u, &m, languages);
                        if p.is_empty() {
                            return Ok(m);
                        }
                        p
                    }
                    Ok(_) => vec!["<unit> must be a YAML mapping".into()],
                    Err(e) => vec![format!("<unit> is not valid YAML: {e}")],
                },
            };
            last = problems.join("\n");
            status(&format!("  rejected: {}", llm::cut(&problems[0], 160)));
            messages.push(Msg::assistant(reply));
            messages.push(Msg::user(prompts::feedback(&last, "the <unit> block")));
        }
        Err(format!(
            "{}: contract rejected after {} attempt(s): {last}",
            u.id,
            self.opts.retries + 1
        ))
    }

    fn ref_text(&self, id: &str) -> String {
        let dir = hidden(&self.root).join("refs").join(id);
        let mut out = String::new();
        for rel in proctor::scan::walk(&dir) {
            if rel.ends_with("Cargo.lock") {
                continue;
            }
            if let Ok(body) = fs::read_to_string(dir.join(&rel)) {
                out.push_str(&format!("<file path=\"{rel}\">\n{body}</file>\n"));
            }
        }
        out
    }

    /// `cases` and `random` of each earlier stage of `u`'s project.
    fn earlier_tests(&self, u: &Entry) -> Vec<(String, String)> {
        let Some(p) = &u.parent else {
            return Vec::new();
        };
        let mut out = Vec::new();
        for st in self.plan.stages(p) {
            if st.id == u.id {
                break;
            }
            let Some(m) = self.read_unit_map(&st.id) else {
                continue;
            };
            let mut t = Mapping::new();
            for k in ["cases", "random"] {
                if let Some(v) = m.get(s(k)) {
                    t.insert(s(k), v.clone());
                }
            }
            if !t.is_empty()
                && let Ok(y) = yaml_serde::to_string(&Yaml::Mapping(t))
            {
                out.push((st.id.clone(), y));
            }
        }
        out
    }

    /// Allowed entries of `u` and the earlier stages of its project (as the
    /// grader merges them), or `None` when nothing is restricted.
    fn allowed_so_far(&self, u: &Entry, contract: &Mapping) -> Option<Vec<String>> {
        let list = |m: &Mapping| -> Option<Vec<String>> {
            match m.get(s("allowed"))? {
                Yaml::Sequence(v) => Some(
                    v.iter()
                        .filter_map(|x| x.as_str().map(String::from))
                        .collect(),
                ),
                _ => None,
            }
        };
        let mut all = list(contract)?;
        if let Some(p) = &u.parent {
            for st in self.plan.stages(p) {
                if st.id == u.id {
                    break;
                }
                for a in self
                    .read_unit_map(&st.id)
                    .and_then(|m| list(&m))
                    .unwrap_or_default()
                {
                    if !all.contains(&a) {
                        all.push(a);
                    }
                }
            }
        }
        Some(all)
    }

    fn tests(&mut self, u: &Entry, upto: &[&Entry], languages: &Mapping) -> Result<(), String> {
        let h = hidden(&self.root);
        let contract = self
            .read_unit_map(&u.id)
            .ok_or("contract missing; run pass 1 first")?;
        let mut contract = contract;
        contract.remove(s("cases"));
        contract.remove(s("random"));
        let contract_text =
            yaml_serde::to_string(&Yaml::Mapping(contract.clone())).map_err(|e| e.to_string())?;
        let previous = self
            .previous_stage(u)
            .map(|p| self.ref_text(&p.id))
            .unwrap_or_default();
        let lib_path = h.join("lib.yaml");
        let lib_text = fs::read_to_string(&lib_path).unwrap_or_default();
        let later: Vec<String> = match &u.parent {
            Some(p) => self
                .plan
                .stages(p)
                .iter()
                .skip_while(|x| x.id != u.id)
                .skip(1)
                .map(|x| format!("{}: {}", x.id, x.f.goal.as_deref().unwrap_or(&x.title)))
                .collect(),
            None => Vec::new(),
        };
        let earlier = self.earlier_tests(u);
        let allowed = self.allowed_so_far(u, &contract);
        let forbidden: Vec<String> = match contract.get(s("forbidden")) {
            Some(Yaml::Sequence(v)) => v
                .iter()
                .filter_map(|x| x.as_str().map(String::from))
                .collect(),
            _ => Vec::new(),
        };
        let language = self.language_info(&self.plan.language(u), languages);
        let req = prompts::tests_request(
            &contract_text,
            &prompts::TestsContext {
                previous_ref: &previous,
                lib: &lib_text,
                language: &language,
                later_stages: &later,
                earlier_tests: &earlier,
                allowed: allowed.as_deref(),
                forbidden: &forbidden,
            },
        );
        let mut messages = vec![Msg::system(self.system.clone()), Msg::user(req)];
        let mut draft = Draft::default();
        let mut last = String::new();
        for attempt in 0..=self.opts.retries {
            let reply = match self.chat(&format!("tests-{}", u.id), &messages) {
                Ok(r) => r,
                Err(e) => {
                    last = e;
                    break;
                }
            };
            // A retry sends only what changes: merge it into what came before.
            draft.merge(&reply);
            match self.try_tests(u, upto, &contract, &draft, &lib_text, languages) {
                Ok(()) => return Ok(()),
                Err(problem) => {
                    last = problem;
                    // Test-language rejections feed the parser's corpus of idioms.
                    if last.contains("cases line") || last.contains("law check") {
                        crate::curriculum::log_validation(
                            &self.root,
                            "tests",
                            &[],
                            &[crate::curriculum::Problem {
                                line: 0,
                                id: Some(u.id.clone()),
                                msg: last.clone(),
                                warning: false,
                            }],
                        );
                    }
                    status(&format!(
                        "  attempt {} rejected: {}",
                        attempt + 1,
                        llm::cut(last.lines().next().unwrap_or(""), 180)
                    ));
                    messages.push(Msg::assistant(reply));
                    messages.push(Msg::user(prompts::tests_retry(&last)));
                }
            }
        }
        // Never keep a broken suite: restore the bare contract and the old lib.
        let text = yaml_serde::to_string(&Yaml::Mapping(contract)).map_err(|e| e.to_string())?;
        write(&self.unit_path(&u.id), &text)?;
        write(&lib_path, &lib_text)?;
        let _ = fs::remove_dir_all(h.join("refs").join(&u.id));
        Err(llm::cut(&last, 400))
    }

    fn try_tests(
        &mut self,
        u: &Entry,
        upto: &[&Entry],
        contract: &Mapping,
        draft: &Draft,
        lib_text: &str,
        languages: &Mapping,
    ) -> Result<(), String> {
        let h = hidden(&self.root);
        let tests_yaml = draft.tests.clone().ok_or("missing <tests> block")?;
        let tests: Mapping = match llm::parse_yaml(&tests_yaml) {
            Ok(Yaml::Mapping(m)) => m,
            Ok(_) => {
                return Err("<tests> must be a YAML mapping with `cases` and/or `random`".into());
            }
            Err(e) => return Err(format!("<tests> is not valid YAML: {e}")),
        };
        const TEST_KEYS: [&str; 5] = ["cases", "random", "helpers", "extra_flags", "wrap"];
        let mut unit = contract.clone();
        for key in TEST_KEYS {
            if let Some(v) = tests.get(s(key)) {
                unit.insert(s(key), v.clone());
            }
        }
        if let Some(extra) = tests
            .keys()
            .find(|k| !k.as_str().is_some_and(|k| TEST_KEYS.contains(&k)))
        {
            return Err(format!(
                "unexpected key `{}` in <tests>; allowed: {}",
                extra.as_str().unwrap_or("?"),
                TEST_KEYS.join(", ")
            ));
        }
        // Support files for helpers live in the hidden folder, per unit.
        let support_dir = h.join("support").join(&u.id);
        let _ = fs::remove_dir_all(&support_dir);
        if draft.support.is_empty() {
            unit.remove(s("support"));
        } else {
            let mut list = Vec::new();
            for (path, body) in &draft.support {
                if path.contains("..") || path.starts_with('/') {
                    return Err(format!(
                        "support path `{path}` must be a plain relative name"
                    ));
                }
                write(&support_dir.join(path), body)?;
                list.push(format!("support/{}/{path}", u.id));
            }
            unit.insert(s("support"), seq(&list));
        }
        let refs = &draft.files;
        if refs.is_empty() {
            return Err("missing the reference solution (<file path=\"...\"> blocks)".into());
        }
        for path in refs.keys() {
            if path.starts_with('/') || path.contains("..") || path.contains(':') {
                return Err(format!(
                    "reference path `{path}` must be relative to the workspace"
                ));
            }
        }
        // lib.yaml: keep existing names, add new ones.
        let mut lib: Mapping = match yaml_serde::from_str::<Yaml>(lib_text) {
            Ok(Yaml::Mapping(m)) => m,
            _ => Mapping::new(),
        };
        if let Some(l) = &draft.lib
            && !l.trim().is_empty()
        {
            match yaml_serde::from_str::<Yaml>(l) {
                Ok(Yaml::Mapping(new)) => {
                    for (k, v) in new {
                        if !lib.contains_key(&k) {
                            lib.insert(k, v);
                        }
                    }
                }
                Ok(Yaml::Null) => {}
                _ => return Err("<lib> must be a YAML mapping of named generators".into()),
            }
        }
        let lib_out = if lib.is_empty() {
            String::new()
        } else {
            yaml_serde::to_string(&Yaml::Mapping(lib)).map_err(|e| e.to_string())?
        };
        write(&h.join("lib.yaml"), &lib_out)?;
        write(
            &self.unit_path(&u.id),
            &yaml_serde::to_string(&Yaml::Mapping(unit)).map_err(|e| e.to_string())?,
        )?;
        let ref_dir = h.join("refs").join(&u.id);
        let _ = fs::remove_dir_all(&ref_dir);
        for (path, body) in refs {
            write(&ref_dir.join(path), body)?;
        }
        if self.plan.language(u) == "rust" {
            // Formatting is mechanical: done here rather than asked of the model.
            let mut fmt = std::process::Command::new(lang::resolve("cargo"));
            fmt.arg("fmt").current_dir(&ref_dir);
            let _ = proctor::run::run(&mut fmt, None, std::time::Duration::from_secs(60));
        }
        let scaffold_dir = h.join("scaffold").join(&u.id);
        let _ = fs::remove_dir_all(&scaffold_dir);
        for (path, body) in &draft.scaffold {
            write(&scaffold_dir.join(path), body)?;
        }
        // Gate: the course up to this unit must load, parse and self-test.
        self.write_course(upto, languages)?;
        let c = CourseDir::load(&self.root)?;
        let problems: Vec<String> = engine::validate(&c)
            .into_iter()
            .filter(|p| p.starts_with(&format!("{}:", u.id)) || p.contains(&u.id))
            .collect();
        if !problems.is_empty() {
            return Err(problems.join("\n"));
        }
        let unit = c.unit(&u.id).ok_or("unit missing after write")?;
        let seed = proctor::gens::Rng::new(proctor::journal::now()).next_u64();
        let r = if proctor::docker::needed(unit) {
            proctor::docker::selftest(&c, &[unit], seed)?
                .pop()
                .ok_or("no report from the Linux container")?
        } else {
            selftest::unit(&c, unit, seed)
        };
        let mut issues = Vec::new();
        if let Err(e) = &r.reference {
            issues.push(format!("your reference solution fails proctor: {e}"));
        }
        for v in &r.vacuous {
            issues.push(format!(
                "weak tests: {v}; add a check that fails when the function does nothing"
            ));
        }
        for n in &r.notes {
            if n.starts_with("stubs") {
                issues.push(n.clone());
            }
        }
        if issues.is_empty() {
            let (p, t) = r.reference.unwrap_or((0, 0));
            status(&format!(
                "  gate OK: reference {p}/{t}{}",
                if r.vacuity_checked > 0 {
                    ", every function caught on stubs"
                } else {
                    ""
                }
            ));
            Ok(())
        } else {
            Err(issues.join("\n"))
        }
    }
}

/// One-line keys coursecrafter sets itself: a model that repeats them can
/// break the YAML (`concepts: [formatting with {}]`) for values that are
/// replaced anyway, so they are removed before parsing.
fn drop_fixed_keys(yaml: &str) -> String {
    const KEYS: &[&str] = &[
        "id",
        "title",
        "project",
        "language",
        "difficulty",
        "concept_difficulty",
        "hours",
        "concepts",
        "introduces",
        "assumes",
        "prereqs",
        "notes",
        "platform",
        "stretch",
        "guided",
        "done_when",
    ];
    yaml.lines()
        .filter(|l| {
            let Some((k, v)) = l.split_once(':') else {
                return true;
            };
            !(KEYS.contains(&k) && !v.trim().is_empty())
        })
        .collect::<Vec<_>>()
        .join("\n")
}

fn s_key(k: &str) -> Yaml {
    Yaml::String(k.to_string())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn repeated_fixed_keys_cannot_break_the_contract() {
        let y = "id: d01\nconcepts: [integer types, formatting with {} and {:?}]\ntarget: program\nrequirements:\n  - \"id: kept, it is inside a list\"\n";
        assert!(yaml_serde::from_str::<Yaml>(y).is_err());
        let fixed = drop_fixed_keys(y);
        let m: Yaml = yaml_serde::from_str(&fixed).unwrap();
        assert!(m.get("concepts").is_none() && m.get("id").is_none());
        assert_eq!(m.get("target").and_then(Yaml::as_str), Some("program"));
        assert!(fixed.contains("id: kept"));
    }

    #[test]
    fn a_retry_replaces_only_what_it_sends() {
        let mut d = Draft::default();
        d.merge("<tests>\ncases: |\n  f(1) == 1\n</tests>\n<file path=\"src/main.rs\">\nfn main() {}\n</file>\n<file path=\"Cargo.toml\">\n[package]\n</file>\n");
        d.merge("<file path=\"src/main.rs\">\nfn main() { println!(); }\n</file>\n<file path=\"old.rs\">\n</file>\n");
        assert!(d.tests.as_deref().is_some_and(|t| t.contains("f(1) == 1")));
        assert!(d.files["src/main.rs"].contains("println"));
        assert!(d.files.contains_key("Cargo.toml"));
        assert!(!d.files.contains_key("old.rs"));
    }
}

/// One unit's suite as the model has sent it so far. The first reply holds
/// everything; a retry sends only what changes, merged in here, so fixing one
/// error cannot break what already worked.
#[derive(Default)]
struct Draft {
    tests: Option<String>,
    lib: Option<String>,
    files: BTreeMap<String, String>,
    support: BTreeMap<String, String>,
    scaffold: BTreeMap<String, String>,
}

impl Draft {
    fn merge(&mut self, reply: &str) {
        if let Some(t) = llm::yaml_block(reply, "tests") {
            self.tests = Some(t);
        }
        if let Some(l) = llm::block(reply, "lib") {
            self.lib = Some(l);
        }
        for (tag, map) in [
            ("file", &mut self.files),
            ("support", &mut self.support),
            ("scaffold", &mut self.scaffold),
        ] {
            for (path, body) in llm::files(reply, tag) {
                // An empty block deletes the file.
                if body.trim().is_empty() {
                    map.remove(&path);
                } else {
                    map.insert(path, body);
                }
            }
        }
    }
}
