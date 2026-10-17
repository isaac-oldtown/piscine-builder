//! The curriculum: one markdown file, written by a browser chat, by
//! `taskmaster new`, or by hand. It is the contract between planning and
//! generation, so it is parsed strictly and every problem is reported in a
//! form that can be pasted back to whoever wrote it.
//!
//! ```text
//! ---
//! title: C from scratch
//! language: c
//! ---
//! ## d01-hello: Hello
//! goal: Compile and run a first program.
//! difficulty: 1
//! introduces: [compiling, printf]
//!
//! Free text: a brief for the generator.
//! ```

use regex::Regex;
use serde::Deserialize;
use std::collections::{BTreeMap, BTreeSet};

#[derive(Deserialize, Debug, Clone, Default)]
#[serde(deny_unknown_fields)]
pub struct Meta {
    pub title: String,
    #[serde(default)]
    pub language: Option<String>,
    #[serde(default)]
    pub level: Option<String>,
    #[serde(default)]
    pub hours_per_week: Option<f64>,
    /// Course length; with `hours_per_week` it sets the hour budget.
    #[serde(default)]
    pub weeks: Option<f64>,
    #[serde(default)]
    pub description: Option<String>,
    /// Where students will follow the course, when it differs from the
    /// machine building it (OS, compilers, tools installed).
    #[serde(default)]
    pub environment: Option<String>,
}

#[derive(Deserialize, Debug, Clone, Default)]
#[serde(deny_unknown_fields)]
pub struct Fields {
    #[serde(default)]
    pub kind: Option<String>,
    #[serde(default)]
    pub language: Option<String>,
    #[serde(default)]
    pub difficulty: Option<u8>,
    #[serde(default)]
    pub goal: Option<String>,
    #[serde(default)]
    pub introduces: Vec<String>,
    #[serde(default)]
    pub assumes: Vec<String>,
    #[serde(default)]
    pub target: Option<String>,
    #[serde(default)]
    pub scaffold: Option<String>,
    #[serde(default)]
    pub allows: Vec<String>,
    #[serde(default)]
    pub gradable: Option<bool>,
    #[serde(default)]
    pub teaches: Vec<String>,
    #[serde(default)]
    pub with: Option<String>,
    /// Planning estimate of implementation and debugging time. Never shown
    /// in the instructions.
    #[serde(default)]
    pub hours: Option<f64>,
    /// Optional unit, skippable by the student.
    #[serde(default)]
    pub stretch: bool,
    /// One line: the observable behaviour that means the unit is done.
    #[serde(default)]
    pub done_when: Option<String>,
}

#[derive(Clone, Copy, PartialEq, Eq, Debug)]
pub enum Kind {
    Drill,
    Project,
    Stage,
    Note,
}

impl Kind {
    pub fn name(self) -> &'static str {
        match self {
            Kind::Drill => "drill",
            Kind::Project => "project",
            Kind::Stage => "stage",
            Kind::Note => "note",
        }
    }
}

#[derive(Debug, Clone)]
pub struct Entry {
    pub id: String,
    pub title: String,
    pub kind: Kind,
    pub f: Fields,
    pub brief: String,
    pub parent: Option<String>,
    pub line: usize,
}

pub struct Plan {
    pub meta: Meta,
    pub entries: Vec<Entry>,
}

#[derive(Debug, Clone)]
pub struct Problem {
    pub line: usize,
    pub id: Option<String>,
    pub msg: String,
    pub warning: bool,
}

impl Problem {
    fn err(line: usize, id: Option<&str>, msg: impl Into<String>) -> Problem {
        Problem {
            line,
            id: id.map(str::to_string),
            msg: msg.into(),
            warning: false,
        }
    }
    fn warn(line: usize, id: Option<&str>, msg: impl Into<String>) -> Problem {
        Problem {
            line,
            id: id.map(str::to_string),
            msg: msg.into(),
            warning: true,
        }
    }
}

fn kind_of(id: &str) -> Option<Kind> {
    let stage = Regex::new(r"^p\d{2,}\.\d+-[a-z0-9]+(-[a-z0-9]+)*$").expect("regex");
    let project = Regex::new(r"^p\d{2,}-[a-z0-9]+(-[a-z0-9]+)*$").expect("regex");
    let drill = Regex::new(r"^d\d{2,}-[a-z0-9]+(-[a-z0-9]+)*$").expect("regex");
    let note = Regex::new(r"^note-[a-z0-9]+(-[a-z0-9]+)*$").expect("regex");
    if stage.is_match(id) {
        Some(Kind::Stage)
    } else if project.is_match(id) {
        Some(Kind::Project)
    } else if drill.is_match(id) {
        Some(Kind::Drill)
    } else if note.is_match(id) {
        Some(Kind::Note)
    } else {
        None
    }
}

pub fn parse(text: &str) -> Result<Plan, Vec<Problem>> {
    let text = text.replace("\r\n", "\n");
    let lines: Vec<&str> = text.lines().collect();
    let mut problems = Vec::new();
    let mut i = 0;
    while i < lines.len() && lines[i].trim().is_empty() {
        i += 1;
    }
    if lines.get(i).map(|l| l.trim()) != Some("---") {
        return Err(vec![Problem::err(
            i + 1,
            None,
            "the file must start with a `---` front matter block (title, language)",
        )]);
    }
    let fm_start = i + 1;
    let fm_end = (fm_start..lines.len())
        .find(|&j| lines[j].trim() == "---")
        .ok_or_else(|| {
            vec![Problem::err(
                fm_start,
                None,
                "the front matter is not closed by `---`",
            )]
        })?;
    let meta: Meta = yaml_serde::from_str(&lines[fm_start..fm_end].join("\n"))
        .map_err(|e| vec![Problem::err(fm_start, None, format!("front matter: {e}"))])?;
    let heading = Regex::new(r"^(#{2,3})\s+(\S+?)\s*:\s*(.+?)\s*$").expect("regex");
    let mut entries: Vec<Entry> = Vec::new();
    let mut last_project: Option<String> = None;
    let mut j = fm_end + 1;
    while j < lines.len() {
        let line = lines[j];
        let level = line.chars().take_while(|c| *c == '#').count();
        if !(level == 2 || level == 3) || !line[level..].starts_with(' ') {
            j += 1;
            continue;
        }
        let Some(c) = heading.captures(line) else {
            problems.push(Problem::err(
                j + 1,
                None,
                format!("headings must read `## id: Title`, got `{}`", line.trim()),
            ));
            j += 1;
            continue;
        };
        let (hashes, id, title) = (c[1].len(), c[2].to_string(), c[3].to_string());
        let start = j + 1;
        let mut k = start;
        while k < lines.len() && !lines[k].trim().is_empty() && !lines[k].starts_with('#') {
            k += 1;
        }
        let yaml = lines[start..k].join("\n");
        let mut b = k;
        while b < lines.len() && !(lines[b].starts_with("## ") || lines[b].starts_with("### ")) {
            b += 1;
        }
        let brief = lines[k..b].join("\n").trim().to_string();
        let f: Fields = if yaml.trim().is_empty() {
            Fields::default()
        } else {
            match yaml_serde::from_str(&yaml) {
                Ok(f) => f,
                Err(e) => {
                    problems.push(Problem::err(start, Some(&id), format!("{e}")));
                    j = b;
                    continue;
                }
            }
        };
        let kind = match (f.kind.as_deref(), kind_of(&id)) {
            (Some(k), detected) => {
                let k = match k {
                    "drill" => Kind::Drill,
                    "project" => Kind::Project,
                    "stage" => Kind::Stage,
                    "note" => Kind::Note,
                    other => {
                        problems.push(Problem::err(
                            start,
                            Some(&id),
                            format!("unknown kind `{other}` (drill, project, stage, note)"),
                        ));
                        j = b;
                        continue;
                    }
                };
                if detected.is_some_and(|d| d != k) {
                    problems.push(Problem::err(
                        j + 1,
                        Some(&id),
                        format!("id `{id}` does not look like a {}", k.name()),
                    ));
                }
                k
            }
            (None, Some(k)) => k,
            (None, None) => {
                problems.push(Problem::err(j + 1, Some(&id), format!("id `{id}` must be dNN-slug, pNN-slug, pNN.k-slug or note-slug (lowercase, hyphens)")));
                j = b;
                continue;
            }
        };
        let parent = match kind {
            Kind::Stage => {
                if hashes != 3 {
                    problems.push(Problem::err(
                        j + 1,
                        Some(&id),
                        "a stage heading uses `###`, under its project",
                    ));
                }
                if last_project.is_none() {
                    problems.push(Problem::err(
                        j + 1,
                        Some(&id),
                        "a stage must follow its project's `##` heading",
                    ));
                }
                last_project.clone()
            }
            Kind::Project => {
                last_project = Some(id.clone());
                None
            }
            _ => {
                if hashes == 3 {
                    problems.push(Problem::err(
                        j + 1,
                        Some(&id),
                        "only project stages use `###` headings",
                    ));
                }
                last_project = None;
                None
            }
        };
        entries.push(Entry {
            id,
            title,
            kind,
            f,
            brief,
            parent,
            line: j + 1,
        });
        j = b;
    }
    if !problems.is_empty() {
        return Err(problems);
    }
    Ok(Plan { meta, entries })
}

pub fn norm(concept: &str) -> String {
    concept.trim().to_lowercase()
}

impl Plan {
    /// Drills and stages, in teaching order: the units skillcheck will grade.
    pub fn units(&self) -> Vec<&Entry> {
        self.entries
            .iter()
            .filter(|e| matches!(e.kind, Kind::Drill | Kind::Stage))
            .collect()
    }

    pub fn entry(&self, id: &str) -> Option<&Entry> {
        self.entries.iter().find(|e| e.id == id)
    }

    pub fn language(&self, e: &Entry) -> String {
        e.f.language
            .clone()
            .or_else(|| {
                e.parent
                    .as_deref()
                    .and_then(|p| self.entry(p))
                    .and_then(|p| p.f.language.clone())
            })
            .or_else(|| self.meta.language.clone())
            .unwrap_or_default()
            .to_lowercase()
    }

    pub fn notes_for(&self, unit: &str) -> Vec<&Entry> {
        self.entries
            .iter()
            .filter(|e| e.kind == Kind::Note && e.f.with.as_deref() == Some(unit))
            .collect()
    }

    pub fn stages(&self, project: &str) -> Vec<&Entry> {
        self.entries
            .iter()
            .filter(|e| e.parent.as_deref() == Some(project))
            .collect()
    }

    /// Earlier units whose `introduces` cover what `e` assumes.
    pub fn prereqs(&self, e: &Entry) -> Vec<String> {
        let wanted: BTreeSet<String> = e.f.assumes.iter().map(|a| norm(a)).collect();
        let mut out = Vec::new();
        for u in self.units() {
            if u.id == e.id {
                break;
            }
            if u.parent.is_some() && u.parent == e.parent {
                continue;
            }
            if u.f.introduces.iter().any(|c| wanted.contains(&norm(c))) {
                let id = u.parent.clone().unwrap_or_else(|| u.id.clone());
                if !out.contains(&id) {
                    out.push(id);
                }
            }
        }
        out
    }

    pub fn validate(&self) -> Vec<Problem> {
        let mut p = Vec::new();
        if self.meta.title.trim().is_empty() {
            p.push(Problem::err(2, None, "front matter needs a `title`"));
        }
        let mut seen = BTreeSet::new();
        for e in &self.entries {
            if !seen.insert(e.id.clone()) {
                p.push(Problem::err(e.line, Some(&e.id), "duplicate id"));
            }
        }
        // Numbering grows in teaching order.
        let num = |id: &str| -> u32 {
            id[1..]
                .chars()
                .take_while(char::is_ascii_digit)
                .collect::<String>()
                .parse()
                .unwrap_or(0)
        };
        let (mut last_d, mut last_p) = (0, 0);
        for e in &self.entries {
            match e.kind {
                Kind::Drill => {
                    let n = num(&e.id);
                    if n <= last_d {
                        p.push(Problem::err(
                            e.line,
                            Some(&e.id),
                            format!("drill numbers must grow in order (after d{last_d:02})"),
                        ));
                    }
                    last_d = n;
                }
                Kind::Project => {
                    let n = num(&e.id);
                    if n <= last_p {
                        p.push(Problem::err(
                            e.line,
                            Some(&e.id),
                            format!("project numbers must grow in order (after p{last_p:02})"),
                        ));
                    }
                    last_p = n;
                }
                _ => {}
            }
        }
        for proj in self.entries.iter().filter(|e| e.kind == Kind::Project) {
            let stages = self.stages(&proj.id);
            if stages.len() < 2 {
                p.push(Problem::err(
                    proj.line,
                    Some(&proj.id),
                    "a project needs at least two stages, the last one being its finale",
                ));
            }
            let code = proj.id.split('-').next().unwrap_or_default();
            for (k, s) in stages.iter().enumerate() {
                let want = format!("{code}.{}-", k + 1);
                if !s.id.starts_with(&want) {
                    p.push(Problem::err(
                        s.line,
                        Some(&s.id),
                        format!(
                            "stage {} of {} must be numbered `{want}...`",
                            k + 1,
                            proj.id
                        ),
                    ));
                }
            }
            if proj.f.goal.as_deref().is_none_or(|g| g.trim().is_empty()) {
                p.push(Problem::err(
                    proj.line,
                    Some(&proj.id),
                    "a project needs a one-sentence `goal`",
                ));
            }
        }
        let unit_ids: BTreeSet<&str> = self.units().iter().map(|u| u.id.as_str()).collect();
        for e in &self.entries {
            let id = Some(e.id.as_str());
            match e.kind {
                Kind::Drill | Kind::Stage => {
                    if e.f.goal.as_deref().is_none_or(|g| g.trim().is_empty()) {
                        p.push(Problem::err(e.line, id, "needs a one-sentence `goal`"));
                    }
                    match e.f.difficulty {
                        None => p.push(Problem::err(e.line, id, "needs `difficulty` (1 to 5)")),
                        Some(d) if !(1..=5).contains(&d) => {
                            p.push(Problem::err(e.line, id, "`difficulty` must be 1 to 5"))
                        }
                        _ => {}
                    }
                    if let Some(t) = &e.f.target
                        && t != "function"
                        && t != "program"
                    {
                        p.push(Problem::err(
                            e.line,
                            id,
                            "`target` must be function or program",
                        ));
                    }
                    if let Some(s) = &e.f.scaffold
                        && !matches!(s.as_str(), "none" | "minimal" | "full")
                    {
                        p.push(Problem::err(
                            e.line,
                            id,
                            "`scaffold` must be none, minimal or full",
                        ));
                    }
                    let lang = self.language(e);
                    if lang.is_empty() {
                        p.push(Problem::err(
                            e.line,
                            id,
                            "no language: set `language` here or in the front matter",
                        ));
                    }
                    if e.f.target.as_deref() == Some("function")
                        && !lang.is_empty()
                        && !skillcheck::lang::has_harness(&lang)
                    {
                        p.push(Problem::err(e.line, id, format!("`target: function` is available for c, python and rust; use `target: program` for {lang}")));
                    }
                    if e.f.gradable == Some(false) {
                        p.push(Problem::warn(e.line, id, "gradable: false, so skillcheck will only check files, structure and the build"));
                    }
                    if !e.f.teaches.is_empty() || e.f.with.is_some() {
                        p.push(Problem::err(
                            e.line,
                            id,
                            "`teaches` and `with` are for notes",
                        ));
                    }
                }
                Kind::Note => {
                    if e.f.teaches.is_empty() {
                        p.push(Problem::err(e.line, id, "a note needs `teaches: [...]`"));
                    }
                    match &e.f.with {
                        None => p.push(Problem::err(
                            e.line,
                            id,
                            "a note needs `with: <unit id>` (revealed together with that unit)",
                        )),
                        Some(w) if !unit_ids.contains(w.as_str()) => p.push(Problem::err(
                            e.line,
                            id,
                            format!("`with: {w}` is not a drill or stage of this curriculum"),
                        )),
                        _ => {}
                    }
                }
                Kind::Project => {}
            }
        }
        // Teach before use.
        let mut known: BTreeSet<String> = BTreeSet::new();
        let mut project_checked: BTreeSet<String> = BTreeSet::new();
        for u in self.units() {
            for n in self.notes_for(&u.id) {
                known.extend(n.f.teaches.iter().map(|c| norm(c)));
            }
            let mut needs: Vec<(String, &Entry)> =
                u.f.assumes.iter().map(|a| (a.clone(), u)).collect();
            if let Some(pid) = &u.parent
                && project_checked.insert(pid.clone())
                && let Some(proj) = self.entry(pid)
            {
                needs.extend(proj.f.assumes.iter().map(|a| (a.clone(), proj)));
            }
            for (a, owner) in needs {
                if !known.contains(&norm(&a)) {
                    p.push(Problem::err(
                        owner.line,
                        Some(&owner.id),
                        format!("assumes `{a}`, which no earlier unit introduces and no note teaches; add a drill, a stage or a note for it, or move this unit later"),
                    ));
                }
            }
            known.extend(u.f.introduces.iter().map(|c| norm(c)));
        }
        if self.units().is_empty() {
            p.push(Problem::err(
                1,
                None,
                "the curriculum has no drill or stage",
            ));
        }
        p.extend(self.quality());
        p
    }

    /// Hours of the required units (stretch goals excluded).
    pub fn required_hours(&self) -> f64 {
        self.units()
            .iter()
            .filter(|u| !u.f.stretch)
            .filter_map(|u| u.f.hours)
            .sum()
    }

    pub fn stretch_hours(&self) -> f64 {
        self.units()
            .iter()
            .filter(|u| u.f.stretch)
            .filter_map(|u| u.f.hours)
            .sum()
    }

    pub fn budget(&self) -> Option<f64> {
        Some(self.meta.hours_per_week? * self.meta.weeks?)
    }

    /// Planning checks beyond the format: workload, stretch goals, units that
    /// nothing uses, difficulty spread, wording.
    fn quality(&self) -> Vec<Problem> {
        let mut p = Vec::new();
        let units = self.units();
        for u in &units {
            let id = Some(u.id.as_str());
            match u.f.hours {
                None => p.push(Problem::err(u.line, id, "needs `hours`: an honest estimate of implementation and debugging time")),
                Some(h) if h <= 0.0 => p.push(Problem::err(u.line, id, "`hours` must be positive")),
                Some(h) if h > 16.0 => p.push(Problem::warn(u.line, id, format!("{h} hours is more than two days of work: split it into smaller units"))),
                _ => {}
            }
            if u.f.done_when.as_deref().is_none_or(|d| d.trim().is_empty()) {
                p.push(Problem::warn(u.line, id, "no `done_when`: say in one line what behaviour means the unit is done"));
            }
        }
        // Budget.
        if let Some(budget) = self.budget() {
            let req = self.required_hours();
            if req > budget * 1.2 {
                p.push(Problem::err(1, None, format!("required units total {req:.0} h for a budget of {budget:.0} h (hours_per_week x weeks): cut units, shrink them, or mark some `stretch: true`")));
            } else if req > budget {
                p.push(Problem::warn(1, None, format!("required units total {req:.0} h, over the {budget:.0} h budget: leave some slack or mark more units as stretch goals")));
            }
        }
        // Stretch goals: optional means nothing required may depend on them.
        let mut stretch_only: BTreeMap<String, String> = BTreeMap::new();
        let mut required_known: BTreeSet<String> = BTreeSet::new();
        for u in &units {
            for n in self.notes_for(&u.id) {
                required_known.extend(n.f.teaches.iter().map(|c| norm(c)));
            }
            if !u.f.stretch {
                for a in &u.f.assumes {
                    let a = norm(a);
                    if !required_known.contains(&a)
                        && let Some(by) = stretch_only.get(&a)
                    {
                        p.push(Problem::err(u.line, Some(&u.id), format!("assumes `{a}`, which only the stretch goal {by} introduces; a skipped stretch goal would leave a gap")));
                    }
                }
            }
            for c in &u.f.introduces {
                if u.f.stretch {
                    stretch_only.entry(norm(c)).or_insert_with(|| u.id.clone());
                } else {
                    required_known.insert(norm(c));
                }
            }
        }
        for proj in self.entries.iter().filter(|e| e.kind == Kind::Project) {
            let stages = self.stages(&proj.id);
            if stages.first().is_some_and(|s| s.f.stretch) {
                p.push(Problem::err(stages[0].line, Some(&stages[0].id), "the first stage of a project cannot be a stretch goal"));
            }
        }
        // Units nothing uses: a drill must serve something later.
        for (i, u) in units.iter().enumerate() {
            if u.kind != Kind::Drill || u.f.stretch || u.f.introduces.is_empty() {
                continue;
            }
            let later = &units[i + 1..];
            if later.is_empty() {
                continue;
            }
            let used = u.f.introduces.iter().any(|c| {
                let c = norm(c);
                later.iter().any(|l| l.f.assumes.iter().any(|a| norm(a) == c))
            }) || self.entries.iter().any(|e| e.kind == Kind::Project && e.line > u.line && e.f.assumes.iter().any(|a| u.f.introduces.iter().any(|c| norm(c) == norm(a))));
            if !used {
                p.push(Problem::warn(u.line, Some(&u.id), "nothing later assumes what this drill introduces: tie it to a later unit, fold it into another one, or mark it stretch"));
            }
        }
        // Difficulty spread: 5 stays rare, and one level must not dominate.
        let diffs: Vec<u8> = units.iter().filter_map(|u| u.f.difficulty).collect();
        if !diffs.is_empty() {
            let fives = diffs.iter().filter(|d| **d == 5).count();
            if fives > (diffs.len() / 10).max(1) {
                p.push(Problem::warn(1, None, format!("{fives} units at difficulty 5: keep 5 for the one or two hardest units of the course")));
            }
            if diffs.len() >= 8 {
                for level in 1..=5u8 {
                    let n = diffs.iter().filter(|d| **d == level).count();
                    if n * 100 > diffs.len() * 55 {
                        p.push(Problem::warn(1, None, format!("{n} of {} units are at difficulty {level}: spread the scale so it carries information", diffs.len())));
                    }
                }
            }
        }
        // Wording.
        let banned = Regex::new(r"(?i)\bno-school-names\b").expect("regex");
        let mut texts: Vec<(usize, Option<&str>, String)> = vec![(1, None, format!("{} {}", self.meta.title, self.meta.description.clone().unwrap_or_default()))];
        for e in &self.entries {
            texts.push((e.line, Some(e.id.as_str()), format!("{} {} {} {}", e.title, e.f.goal.clone().unwrap_or_default(), e.f.done_when.clone().unwrap_or_default(), e.brief)));
        }
        for (line, id, t) in texts {
            if let Some(m) = banned.find(&t) {
                p.push(Problem::err(line, id, format!("do not refer to other schools or their programs (found `{}`)", m.as_str())));
            }
        }
        p
    }

    /// Languages used, with a warning when the toolchain is missing, and
    /// tools the units rely on that this machine does not have.
    pub fn preflight(&self, env: &crate::env::Env) -> Vec<Problem> {
        let mut out = Vec::new();
        for u in self.units() {
            let text: String =
                u.f.introduces
                    .iter()
                    .chain(&u.f.assumes)
                    .chain(&u.f.allows)
                    .map(|s| s.to_lowercase())
                    .collect::<Vec<_>>()
                    .join(" | ");
            for tool in crate::env::MENTIONABLE {
                let re = Regex::new(&format!(r"\b{}\b", regex::escape(tool))).expect("regex");
                if re.is_match(&text) && !env.has(tool) {
                    out.push(Problem::warn(u.line, Some(&u.id), format!("relies on `{tool}`, which is not installed on this machine; its tests cannot be verified here")));
                }
            }
        }
        let mut langs: BTreeMap<String, usize> = BTreeMap::new();
        for u in self.units() {
            langs.entry(self.language(u)).or_insert(u.line);
        }
        for (lang, line) in langs {
            let (tool, ok) = match lang.as_str() {
                "c" => (
                    "a C compiler (cc, gcc or clang)",
                    skillcheck::lang::cc().is_some(),
                ),
                "python" => ("Python 3", skillcheck::lang::python().is_some()),
                "rust" => ("cargo", skillcheck::run::exists("cargo")),
                "bash" | "sh" | "shell" => (
                    "bash",
                    skillcheck::run::exists(&skillcheck::lang::resolve("bash")),
                ),
                _ => continue,
            };
            if !ok {
                out.push(Problem::warn(line, None, format!("{lang}: {tool} is not installed on this machine; install it before building the tests")));
            }
        }
        out
    }
}

pub fn report(problems: &[Problem]) -> String {
    let mut s = String::new();
    for p in problems {
        let tag = if p.warning { "warning" } else { "error" };
        let at = match &p.id {
            Some(id) => format!("line {} ({id})", p.line),
            None => format!("line {}", p.line),
        };
        s.push_str(&format!("- {tag}, {at}: {}\n", p.msg));
    }
    s
}

/// A block the user can paste back into the chat that wrote the curriculum.
pub fn paste_block(problems: &[Problem]) -> String {
    let errors: Vec<Problem> = problems.iter().filter(|p| !p.warning).cloned().collect();
    format!(
        "The levelup importer rejected the curriculum. Fix these problems and send the complete file again, in the same format:\n\n{}",
        report(&errors)
    )
}

/// Compact plan table for approval.
pub fn table(plan: &Plan) -> String {
    let mut rows: Vec<[String; 7]> = vec![[
        "unit".into(),
        "lang".into(),
        "diff".into(),
        "hours".into(),
        "target".into(),
        "introduces".into(),
        "assumes".into(),
    ]];
    for e in &plan.entries {
        let (id, lang, diff, target) = match e.kind {
            Kind::Project => (
                format!("{} (project)", e.id),
                plan.language(e),
                String::new(),
                String::new(),
            ),
            Kind::Note => (
                format!(
                    "  {} (note, with {})",
                    e.id,
                    e.f.with.clone().unwrap_or_default()
                ),
                String::new(),
                String::new(),
                String::new(),
            ),
            Kind::Stage => (
                format!("  {}{}", e.id, if e.f.stretch { " (stretch)" } else { "" }),
                plan.language(e),
                e.f.difficulty.map(|d| d.to_string()).unwrap_or_default(),
                e.f.target.clone().unwrap_or_else(|| "auto".into()),
            ),
            Kind::Drill => (
                format!("{}{}", e.id, if e.f.stretch { " (stretch)" } else { "" }),
                plan.language(e),
                e.f.difficulty.map(|d| d.to_string()).unwrap_or_default(),
                e.f.target.clone().unwrap_or_else(|| "auto".into()),
            ),
        };
        let intro = if e.kind == Kind::Note {
            e.f.teaches.join(", ")
        } else {
            e.f.introduces.join(", ")
        };
        let hours = match e.kind {
            Kind::Project => {
                let h: f64 = plan.stages(&e.id).iter().filter_map(|s| s.f.hours).sum();
                if h > 0.0 { format!("{h:.0}") } else { String::new() }
            }
            _ => e.f.hours.map(|h| format!("{h}")).unwrap_or_default(),
        };
        rows.push([id, lang, diff, hours, target, intro, e.f.assumes.join(", ")]);
    }
    let widths: Vec<usize> = (0..7)
        .map(|c| {
            rows.iter()
                .map(|r| r[c].chars().count())
                .max()
                .unwrap_or(0)
                .min(if c >= 5 { 40 } else { 50 })
        })
        .collect();
    let mut s = String::new();
    for (n, r) in rows.iter().enumerate() {
        let cells: Vec<String> = r
            .iter()
            .enumerate()
            .map(|(c, v)| {
                let v = crate::llm::cut(v, widths[c]);
                format!("{v:<w$}", w = widths[c])
            })
            .collect();
        s.push_str(cells.join("  ").trim_end());
        s.push('\n');
        if n == 0 {
            s.push_str(
                &widths
                    .iter()
                    .map(|w| "-".repeat(*w))
                    .collect::<Vec<_>>()
                    .join("  "),
            );
            s.push('\n');
        }
    }
    s.push_str(&summary(plan));
    s
}

/// Workload and difficulty in two lines, under the table.
pub fn summary(plan: &Plan) -> String {
    let req = plan.required_hours();
    let stretch = plan.stretch_hours();
    let mut s = format!("\nWorkload: {req:.0} h required");
    if stretch > 0.0 {
        s.push_str(&format!(" + {stretch:.0} h of stretch goals"));
    }
    if let Some(b) = plan.budget() {
        s.push_str(&format!(" for a budget of {b:.0} h ({:.0}%)", req * 100.0 / b.max(1.0)));
    }
    let mut counts = [0usize; 6];
    for u in plan.units() {
        if let Some(d) = u.f.difficulty {
            counts[(d as usize).min(5)] += 1;
        }
    }
    let spread: Vec<String> = (1..=5).filter(|d| counts[*d] > 0).map(|d| format!("{d}: {}", counts[d])).collect();
    s.push_str(&format!("\nDifficulty: {}\n", spread.join(", ")));
    s
}

#[cfg(test)]
mod tests {
    use super::*;

    const GOOD: &str = "---\ntitle: T\nlanguage: c\n---\n\n## d01-hello: Hello\ngoal: Print.\ndifficulty: 1\nhours: 2\nintroduces: [compiling]\n\nBrief text.\n\n## note-make: Makefiles\nteaches: [make]\nwith: p01.1-build\n\n## p01-tool: Tool\ngoal: A tool.\nassumes: [compiling]\n\n### p01.1-build: Build\ngoal: Build it.\ndifficulty: 2\nhours: 3\nassumes: [make]\nintroduces: [linking]\n\n### p01.2-finale: Finale\ngoal: Finish.\ndifficulty: 3\nhours: 4\nassumes: [linking]\n";

    #[test]
    fn parses_and_validates_a_good_plan() {
        let plan = parse(GOOD).unwrap_or_else(|p| panic!("{}", report(&p)));
        assert_eq!(plan.entries.len(), 5);
        assert_eq!(plan.entries[0].brief, "Brief text.");
        assert_eq!(plan.entries[3].parent.as_deref(), Some("p01-tool"));
        let problems = plan.validate();
        assert!(problems.iter().all(|p| p.warning), "{}", report(&problems));
        assert_eq!(plan.prereqs(&plan.entries[2]), vec!["d01-hello"]);
    }

    #[test]
    fn catches_teach_before_use_and_shape_errors() {
        let bad = GOOD
            .replace("with: p01.1-build", "with: p01.2-finale")
            .replace("difficulty: 3", "difficulty: 9");
        let plan = parse(&bad).unwrap();
        let text = report(&plan.validate());
        assert!(text.contains("assumes `make`"), "{text}");
        assert!(text.contains("difficulty"), "{text}");
        assert!(parse("no front matter").is_err());
        let wrong_id = GOOD.replace("## d01-hello: Hello", "## hello: Hello");
        assert!(parse(&wrong_id).is_err());
    }
}
