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
    /// How hard the ideas are to understand (`difficulty` is how hard they
    /// are to implement and debug).
    #[serde(default)]
    pub concept_difficulty: Option<u8>,
    /// Projects: what the project must do to count as a success.
    #[serde(default)]
    pub minimum: Option<String>,
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
                None => p.push(Problem::err(
                    u.line,
                    id,
                    "needs `hours`: an honest estimate of implementation and debugging time",
                )),
                Some(h) if h <= 0.0 => p.push(Problem::err(u.line, id, "`hours` must be positive")),
                Some(h) if h > 16.0 => p.push(Problem::warn(
                    u.line,
                    id,
                    format!("{h} hours is more than two days of work: split it into smaller units"),
                )),
                _ => {}
            }
            match u.f.concept_difficulty {
                None => p.push(Problem::err(u.line, id, "needs `concept_difficulty` (1 to 5): how hard the ideas are to understand, next to `difficulty` for implementation")),
                Some(d) if !(1..=5).contains(&d) => p.push(Problem::err(u.line, id, "`concept_difficulty` must be 1 to 5")),
                _ => {}
            }
            if u.f.done_when.as_deref().is_none_or(|d| d.trim().is_empty()) {
                p.push(Problem::warn(
                    u.line,
                    id,
                    "no `done_when`: say in one line what behaviour means the unit is done",
                ));
            }
        }
        // Budget.
        if let Some(budget) = self.budget() {
            let req = self.required_hours();
            // At least 12% of the budget stays free for debugging and revisiting concepts.
            if req > budget {
                p.push(Problem::err(1, None, format!("required units total {req:.0} h for a budget of {budget:.0} h (hours_per_week x weeks): cut scope, mark units `stretch: true`, or extend the weeks")));
            } else if req > budget * 0.88 {
                p.push(Problem::warn(1, None, format!("required units total {req:.0} h of a {budget:.0} h budget: keep 10 to 15% free for debugging and revisiting concepts")));
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
            if proj
                .f
                .minimum
                .as_deref()
                .is_none_or(|m| m.trim().is_empty())
            {
                p.push(Problem::err(proj.line, Some(&proj.id), "a project needs `minimum`: one line saying what it must do to count as a success"));
            }
            if let Some(first_stretch) = stages.iter().position(|s| s.f.stretch)
                && let Some(late) = stages[first_stretch..].iter().find(|s| !s.f.stretch)
            {
                p.push(Problem::err(late.line, Some(&late.id), "a required stage cannot follow a stretch stage: stretch stages come after the required finale"));
            }
            if stages.first().is_some_and(|s| s.f.stretch) {
                p.push(Problem::err(
                    stages[0].line,
                    Some(&stages[0].id),
                    "the first stage of a project cannot be a stretch goal",
                ));
            }
        }
        // Progression: hard stages do not stack new concepts, and rely on
        // concepts practised in a unit, not only read about in a note.
        let practised: BTreeSet<String> = units
            .iter()
            .flat_map(|u| u.f.introduces.iter().map(|c| norm(c)))
            .collect();
        for u in units
            .iter()
            .filter(|u| u.kind == Kind::Stage && u.f.difficulty.is_some_and(|d| d >= 4))
        {
            if u.f.introduces.len() >= 3 {
                p.push(Problem::warn(u.line, Some(&u.id), format!("a hard stage introducing {} new concepts: practise some of them in a small drill first", u.f.introduces.len())));
            }
            for a in &u.f.assumes {
                if !practised.contains(&norm(a)) {
                    p.push(Problem::warn(u.line, Some(&u.id), format!("relies on `{a}`, which only a note covers: practise it in a drill before this hard stage")));
                }
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
                later
                    .iter()
                    .any(|l| l.f.assumes.iter().any(|a| norm(a) == c))
            }) || self.entries.iter().any(|e| {
                e.kind == Kind::Project
                    && e.line > u.line
                    && e.f
                        .assumes
                        .iter()
                        .any(|a| u.f.introduces.iter().any(|c| norm(c) == norm(a)))
            });
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

/// Rough planning hours for a difficulty, used when `hours` is missing.
fn default_hours(difficulty: u8) -> f64 {
    match difficulty {
        0 | 1 => 1.5,
        2 => 3.0,
        3 => 5.0,
        4 => 8.0,
        _ => 12.0,
    }
}

/// Replaces whole ids only: `d02-a` must not match inside `d02-ab`.
fn replace_id(text: &str, old: &str, new: &str) -> String {
    let id_char = |c: char| c.is_ascii_alphanumeric() || c == '-' || c == '_';
    let mut out = String::with_capacity(text.len());
    let mut rest = text;
    while let Some(i) = rest.find(old) {
        let before = rest[..i].chars().next_back();
        let after = rest[i + old.len()..].chars().next();
        out.push_str(&rest[..i]);
        if before.is_some_and(|c| id_char(c) || c == '.') || after.is_some_and(id_char) {
            out.push_str(old);
        } else {
            out.push_str(new);
        }
        rest = &rest[i + old.len()..];
    }
    out.push_str(rest);
    out
}

fn quoted(v: &str) -> String {
    format!("\"{}\"", v.replace('\\', "\\\\").replace('"', "\\\""))
}

fn is_quoted(v: &str) -> bool {
    v.len() >= 2
        && ((v.starts_with('"') && v.ends_with('"')) || (v.starts_with('\'') && v.ends_with('\'')))
}

/// A plain scalar YAML would misread: `a: b` becomes a map, a leading
/// backtick or bracket is a syntax error, ` #` starts a comment.
fn needs_quotes(v: &str, in_list: bool) -> bool {
    if v.is_empty() || is_quoted(v) {
        return false;
    }
    let starts_badly = v.starts_with(['`', '[', '{', '&', '*', '!', '|', '>', '%', '@', '\'', '"']);
    let list_breakers = in_list && v.contains([',', '[', ']', '{', '}']);
    starts_badly || v.contains(": ") || v.ends_with(':') || v.contains(" #") || list_breakers
}

/// Splits the inside of a flow list at top-level commas; commas inside
/// `<...>`, `(...)`, `[...]` or backticks stay in their item (`HashMap<K, V>`).
fn split_items(inner: &str) -> Vec<String> {
    let mut items = Vec::new();
    let mut cur = String::new();
    let (mut depth, mut tick, mut quote) = (0i32, false, None::<char>);
    for c in inner.chars() {
        match c {
            '"' | '\'' if !tick && (quote.is_none() || quote == Some(c)) => {
                quote = if quote.is_some() { None } else { Some(c) };
            }
            '`' if quote.is_none() => tick = !tick,
            '<' | '(' | '[' | '{' if quote.is_none() && !tick => depth += 1,
            '>' | ')' | ']' | '}' if quote.is_none() && !tick => depth -= 1,
            ',' if quote.is_none() && !tick && depth <= 0 => {
                items.push(std::mem::take(&mut cur).trim().to_string());
                continue;
            }
            _ => {}
        }
        cur.push(c);
    }
    if !cur.trim().is_empty() {
        items.push(cur.trim().to_string());
    }
    items
}

/// Quotes values YAML would reject or misread, on the known keys: scalars
/// (`title: Rust: the basics`) and items of lists
/// (`introduces: [error policy: report and continue, HashMap<K, V>]`).
fn quote_yaml_values(text: &str) -> (String, Vec<String>) {
    let scalar =
        Regex::new(r"^(title|description|environment|goal|done_when|minimum):[ \t]+(.+?)[ \t]*$")
            .expect("regex");
    let list =
        Regex::new(r"^(introduces|assumes|allows|teaches):[ \t]+\[(.*)\][ \t]*$").expect("regex");
    let mut fixes = Vec::new();
    let lines: Vec<String> = text
        .split('\n')
        .enumerate()
        .map(|(i, l)| {
            if let Some(c) = scalar.captures(l)
                && needs_quotes(&c[2], false)
            {
                fixes.push(format!("line {}: quoted the `{}` value", i + 1, &c[1]));
                return format!("{}: {}", &c[1], quoted(&c[2]));
            }
            if let Some(c) = list.captures(l) {
                let items = split_items(&c[2]);
                if items.iter().any(|it| needs_quotes(it, true)) {
                    let fixed: Vec<String> = items
                        .iter()
                        .map(|it| {
                            if needs_quotes(it, true) {
                                quoted(it)
                            } else {
                                it.clone()
                            }
                        })
                        .collect();
                    fixes.push(format!(
                        "line {}: quoted items of the `{}` list",
                        i + 1,
                        &c[1]
                    ));
                    return format!("{}: [{}]", &c[1], fixed.join(", "));
                }
            }
            l.to_string()
        })
        .collect();
    (lines.join("\n"), fixes)
}
/// Mechanical corrections that are not worth an LLM call: drill and project
/// numbers that do not grow, stages numbered out of sequence (references in
/// `with:` and briefs follow the new ids), and missing `hours` (filled from
/// the difficulty). Returns the fixed text and one line per correction.
pub fn autofix(text: &str) -> (String, Vec<String>) {
    let (mut text, mut fixes) = quote_yaml_values(&text.replace("\r\n", "\n"));
    let Ok(plan) = parse(&text) else {
        return (text, fixes);
    };
    let num = |id: &str| -> u32 {
        id[1..]
            .chars()
            .take_while(char::is_ascii_digit)
            .collect::<String>()
            .parse()
            .unwrap_or(0)
    };
    let slug = |id: &str| {
        id.split_once('-')
            .map(|(_, s)| s.to_string())
            .unwrap_or_default()
    };
    let grows = |ids: &[&Entry]| ids.windows(2).all(|w| num(&w[1].id) > num(&w[0].id));
    let mut renames: Vec<(String, String)> = Vec::new();
    let drills: Vec<&Entry> = plan
        .entries
        .iter()
        .filter(|e| e.kind == Kind::Drill)
        .collect();
    if !grows(&drills) {
        for (i, d) in drills.iter().enumerate() {
            renames.push((d.id.clone(), format!("d{:02}-{}", i + 1, slug(&d.id))));
        }
    }
    let projects: Vec<&Entry> = plan
        .entries
        .iter()
        .filter(|e| e.kind == Kind::Project)
        .collect();
    let renumber_projects = !grows(&projects);
    for (i, p) in projects.iter().enumerate() {
        let code = if renumber_projects {
            format!("p{:02}", i + 1)
        } else {
            p.id.split('-').next().unwrap_or_default().to_string()
        };
        renames.push((p.id.clone(), format!("{code}-{}", slug(&p.id))));
        for (k, s) in plan.stages(&p.id).iter().enumerate() {
            renames.push((s.id.clone(), format!("{code}.{}-{}", k + 1, slug(&s.id))));
        }
    }
    renames.retain(|(old, new)| old != new);
    // Two phases, so a chain like d03 -> d02 and d02 -> d01 cannot collide.
    for (i, (old, _)) in renames.iter().enumerate() {
        text = replace_id(&text, old, &format!("\u{1}{i}\u{1}"));
    }
    for (i, (old, new)) in renames.iter().enumerate() {
        text = text.replace(&format!("\u{1}{i}\u{1}"), new);
        fixes.push(format!("renumbered {old} to {new}"));
    }
    let Ok(plan) = parse(&text) else {
        return (text, fixes);
    };
    let trailing = text.ends_with('\n');
    let mut lines: Vec<String> = text.lines().map(str::to_string).collect();
    let missing: Vec<(usize, f64, String)> = plan
        .units()
        .iter()
        .filter(|u| u.f.hours.is_none())
        .filter_map(|u| {
            u.f.difficulty
                .map(|d| (u.line, default_hours(d), u.id.clone()))
        })
        .collect();
    // Bottom to top, so earlier line numbers stay valid.
    for (line, hours, id) in missing.into_iter().rev() {
        let mut k = line;
        while k < lines.len() && !lines[k].trim().is_empty() && !lines[k].starts_with('#') {
            k += 1;
        }
        lines.insert(k, format!("hours: {hours}"));
        fixes.push(format!("{id}: no `hours`, set {hours} from its difficulty"));
    }
    let mut out = lines.join("\n");
    if trailing {
        out.push('\n');
    }
    (out, fixes)
}

/// Parse problems or validation problems of a curriculum text.
pub fn problems_of(text: &str) -> Vec<Problem> {
    match parse(text) {
        Ok(plan) => plan.validate(),
        Err(p) => p,
    }
}

/// Appends fixes and problems to `.skillcheck/taskmaster/validator.log`
/// (JSON lines), to see which rules planners trip on most.
pub fn log_validation(
    course: &std::path::Path,
    source: &str,
    fixes: &[String],
    problems: &[Problem],
) {
    if fixes.is_empty() && problems.is_empty() {
        return;
    }
    let path = course
        .join(".skillcheck")
        .join("taskmaster")
        .join("validator.log");
    if let Some(d) = path.parent() {
        let _ = std::fs::create_dir_all(d);
    }
    let t = skillcheck::journal::now();
    let mut out = String::new();
    for f in fixes {
        out.push_str(
            &serde_json::json!({"t": t, "source": source, "level": "fixed", "msg": f}).to_string(),
        );
        out.push('\n');
    }
    for p in problems {
        let level = if p.warning { "warning" } else { "error" };
        out.push_str(
            &serde_json::json!({"t": t, "source": source, "level": level, "id": p.id, "msg": p.msg}).to_string(),
        );
        out.push('\n');
    }
    use std::io::Write;
    if let Ok(mut f) = std::fs::OpenOptions::new()
        .create(true)
        .append(true)
        .open(&path)
    {
        let _ = f.write_all(out.as_bytes());
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

/// `concept/implementation` difficulty, e.g. `2/4`.
fn difficulties(e: &Entry) -> String {
    let show = |d: Option<u8>| d.map(|d| d.to_string()).unwrap_or_else(|| "?".into());
    format!("{}/{}", show(e.f.concept_difficulty), show(e.f.difficulty))
}

/// Compact plan table for approval.
pub fn table(plan: &Plan) -> String {
    let mut rows: Vec<[String; 7]> = vec![[
        "unit".into(),
        "lang".into(),
        "c/i".into(),
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
                difficulties(e),
                e.f.target.clone().unwrap_or_else(|| "auto".into()),
            ),
            Kind::Drill => (
                format!("{}{}", e.id, if e.f.stretch { " (stretch)" } else { "" }),
                plan.language(e),
                difficulties(e),
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
                if h > 0.0 { round5(h) } else { String::new() }
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

/// Hours for display, to the nearest 5 (59 -> 60, 33 -> 35, 6 -> 5):
/// estimates are not precise enough for more.
pub fn round5(h: f64) -> String {
    let r = (h / 5.0).round() * 5.0;
    if h > 0.0 && r == 0.0 {
        "under 5".into()
    } else {
        format!("{r:.0}")
    }
}

/// Workload and difficulty in two lines, under the table.
pub fn summary(plan: &Plan) -> String {
    let req = plan.required_hours();
    let stretch = plan.stretch_hours();
    let mut s = format!("\nWorkload: about {} h required", round5(req));
    if stretch > 0.0 {
        s.push_str(&format!(" + {} h of stretch goals", round5(stretch)));
    }
    if let Some(b) = plan.budget() {
        s.push_str(&format!(
            " for a budget of {} h ({:.0}%)",
            round5(b),
            req * 100.0 / b.max(1.0)
        ));
    }
    let mut counts = [0usize; 6];
    for u in plan.units() {
        if let Some(d) = u.f.difficulty {
            counts[(d as usize).min(5)] += 1;
        }
    }
    let spread: Vec<String> = (1..=5)
        .filter(|d| counts[*d] > 0)
        .map(|d| format!("{d}: {}", counts[d]))
        .collect();
    s.push_str(&format!(
        "\nImplementation difficulty: {} (c/i column: concept / implementation)\n",
        spread.join(", ")
    ));
    s
}

#[cfg(test)]
mod tests {
    use super::*;

    const GOOD: &str = "---\ntitle: T\nlanguage: c\n---\n\n## d01-hello: Hello\ngoal: Print.\nconcept_difficulty: 2\ndifficulty: 1\nhours: 2\nintroduces: [compiling]\n\nBrief text.\n\n## note-make: Makefiles\nteaches: [make]\nwith: p01.1-build\n\n## p01-tool: Tool\ngoal: A tool.\nminimum: it builds and runs.\nassumes: [compiling]\n\n### p01.1-build: Build\ngoal: Build it.\nconcept_difficulty: 2\ndifficulty: 2\nhours: 3\nassumes: [make]\nintroduces: [linking]\n\n### p01.2-finale: Finale\ngoal: Finish.\nconcept_difficulty: 2\ndifficulty: 3\nhours: 4\nassumes: [linking]\n";

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
    fn autofix_renumbers_and_fills_hours() {
        // Stages numbered 2 and 3, a note pointing at the old id, no hours on d01.
        let messy = GOOD
            .replace("p01.1-build", "p01.2-build")
            .replace("p01.2-finale", "p01.3-finale")
            .replace("difficulty: 1\nhours: 2\n", "difficulty: 1\n");
        assert!(report(&problems_of(&messy)).contains("must be numbered"));
        let (fixed, notes) = autofix(&messy);
        assert!(fixed.contains("### p01.1-build: Build"), "{fixed}");
        assert!(fixed.contains("### p01.2-finale: Finale"), "{fixed}");
        assert!(
            fixed.contains("with: p01.1-build"),
            "the note follows the rename"
        );
        assert!(
            fixed.contains("introduces: [compiling]\nhours: 1.5\n\nBrief text."),
            "{fixed}"
        );
        assert_eq!(notes.len(), 3, "{notes:?}");
        assert!(
            problems_of(&fixed).iter().all(|p| p.warning),
            "{}",
            report(&problems_of(&fixed))
        );
        assert_eq!(
            autofix(&fixed).1.len(),
            0,
            "a fixed file needs no more fixes"
        );
    }

    #[test]
    fn drills_renumber_without_touching_longer_ids() {
        let text = "---\ntitle: T\nlanguage: c\n---\n\n## d03-a: A\ngoal: g.\ndifficulty: 1\nhours: 1\nintroduces: [x]\n\n## d02-ab: B\ngoal: g.\ndifficulty: 1\nhours: 1\nassumes: [x]\n\nSee d03-a first.\n";
        let (fixed, _) = autofix(text);
        assert!(fixed.contains("## d01-a: A"), "{fixed}");
        assert!(fixed.contains("## d02-ab: B"), "{fixed}");
        assert!(fixed.contains("See d01-a first."), "{fixed}");
    }

    #[test]
    fn planning_rules_minimum_buffer_and_stretch_order() {
        let errors = |t: &str| report(&problems_of(t));
        // No minimum viable result, no concept difficulty.
        let t = GOOD.replace("minimum: it builds and runs.\n", "").replacen(
            "concept_difficulty: 2\n",
            "",
            1,
        );
        let r = errors(&t);
        assert!(r.contains("needs `minimum`"), "{r}");
        assert!(r.contains("needs `concept_difficulty`"), "{r}");
        // 9 h required: over a 10 h budget's 88%, under the budget itself.
        let t = GOOD.replace(
            "language: c\n",
            "language: c\nhours_per_week: 5\nweeks: 2\n",
        );
        let r = errors(&t);
        assert!(r.contains("keep 10 to 15% free"), "{r}");
        assert!(!r.contains("error, line 1"), "{r}");
        // A stretch stage followed by a required one.
        let t = GOOD.replace("assumes: [make]\n", "assumes: [make]\nstretch: true\n");
        assert!(errors(&t).contains("cannot follow a stretch stage"));
    }

    #[test]
    fn autofix_quotes_values_starting_with_a_backtick() {
        let t = GOOD.replace("goal: Finish.", "goal: `make` runs \"all\".");
        assert!(parse(&t).is_err());
        let (fixed, notes) = autofix(&t);
        assert!(fixed.contains(r#"goal: "`make` runs \"all\".""#), "{fixed}");
        assert_eq!(notes.len(), 1, "{notes:?}");
        assert!(problems_of(&fixed).iter().all(|p| p.warning));
    }

    #[test]
    fn autofix_quotes_colons_and_list_items() {
        let t = GOOD
            .replace("title: T\n", "title: Rust: the basics\n")
            .replace(
                "introduces: [linking]",
                "introduces: [linking, error policy: report and continue, HashMap<K, V>, `?` operator]",
            )
            .replace("assumes: [linking]", "assumes: [linking, error policy: report and continue]");
        assert!(parse(&t).is_err());
        let (fixed, notes) = autofix(&t);
        assert!(fixed.contains("title: \"Rust: the basics\""), "{fixed}");
        assert!(
            fixed.contains(r#"introduces: [linking, "error policy: report and continue", "HashMap<K, V>", "`?` operator"]"#),
            "{fixed}"
        );
        assert_eq!(notes.len(), 3, "{notes:?}");
        let problems = problems_of(&fixed);
        assert!(problems.iter().all(|p| p.warning), "{}", report(&problems));
        assert_eq!(autofix(&fixed).1.len(), 0, "quoting is idempotent");
    }

    #[test]
    fn the_prompt_example_is_a_valid_curriculum() {
        let problems = problems_of(crate::prompts::PLAN_EXAMPLE);
        assert!(problems.iter().all(|p| p.warning), "{}", report(&problems));
    }

    #[test]
    fn hours_round_to_the_nearest_five() {
        assert_eq!(round5(59.0), "60");
        assert_eq!(round5(33.0), "35");
        assert_eq!(round5(6.0), "5");
        assert_eq!(round5(2.0), "under 5");
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
