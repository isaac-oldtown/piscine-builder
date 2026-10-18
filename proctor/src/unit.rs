//! On-disk course format. One YAML file per unit holds both the contract
//! (what the instructions say) and the tests (what proctor runs), so the
//! two can never drift apart.

use serde::Deserialize;
use std::collections::BTreeMap;
use yaml_serde::Value as Yaml;

#[derive(Deserialize, Debug, Clone)]
#[serde(deny_unknown_fields)]
pub struct Course {
    pub title: String,
    #[serde(default)]
    pub language: Option<String>,
    #[serde(default)]
    pub hours_per_week: Option<f64>,
    #[serde(default)]
    pub weeks: Option<f64>,
    /// How thoroughly the hidden tests probe: relaxed, standard or strict
    /// (the default). Stated in RULES.md.
    #[serde(default)]
    pub rigor: Option<String>,
    /// Course-wide habits the units rely on, quoted in RULES.md.
    #[serde(default)]
    pub conventions: Option<String>,
    /// Unit ids in teaching order. Project stages appear in place.
    pub units: Vec<String>,
    #[serde(default)]
    pub projects: BTreeMap<String, Project>,
    /// Per-language defaults inherited by every unit of that language.
    #[serde(default)]
    pub languages: BTreeMap<String, LangConfig>,
}

/// How to build, run and inspect one language. proctor ships defaults for
/// common languages (see `defaults.rs`); a course's `languages:` entry
/// overrides any field, and unit fields win over both. Every field is optional
/// so "not set" (inherit) differs from "set to empty" (replace).
#[derive(Deserialize, Debug, Clone, Default)]
#[serde(deny_unknown_fields)]
pub struct LangConfig {
    /// Display name in the instructions.
    #[serde(default)]
    pub name: Option<String>,
    /// Compiler for C-family builds (default: CC, then cc, gcc, clang).
    #[serde(default)]
    pub compiler: Option<String>,
    /// Compiler flags, used for the program build and the function harness.
    #[serde(default)]
    pub flags: Option<Vec<String>>,
    #[serde(default)]
    pub build: Option<String>,
    #[serde(default)]
    pub run: Option<String>,
    #[serde(default, rename = "static")]
    pub static_checks: Option<Vec<StaticCheck>>,
    /// Import-detection regexes (group 1 = module) for the Allowed check.
    #[serde(default)]
    pub imports: Option<Vec<String>>,
    /// Source file extensions, without the dot.
    #[serde(default)]
    pub extensions: Option<Vec<String>>,
    /// Line-comment marker (`#`, `--`, `;`...), so commented-out code
    /// is ignored by the Allowed and prototype checks.
    #[serde(default)]
    pub comment: Option<String>,
    #[serde(default)]
    pub env: Option<BTreeMap<String, String>>,
    #[serde(default)]
    pub timeout_ms: Option<u64>,
    /// Command prefix for every run of student code, e.g.
    /// `valgrind --leak-check=full --error-exitcode=99`.
    #[serde(default)]
    pub wrap: Option<String>,
    /// Course files (relative to `.proctor/`) built together with the
    /// student's code, e.g. a malloc-failure shim.
    #[serde(default)]
    pub support: Option<Vec<String>>,
}

impl LangConfig {
    /// `self` with every field that `over` sets replaced.
    pub fn with(self, over: &LangConfig) -> LangConfig {
        let o = over.clone();
        LangConfig {
            name: o.name.or(self.name),
            compiler: o.compiler.or(self.compiler),
            flags: o.flags.or(self.flags),
            build: o.build.or(self.build),
            run: o.run.or(self.run),
            static_checks: o.static_checks.or(self.static_checks),
            imports: o.imports.or(self.imports),
            extensions: o.extensions.or(self.extensions),
            comment: o.comment.or(self.comment),
            env: o.env.or(self.env),
            timeout_ms: o.timeout_ms.or(self.timeout_ms),
            wrap: o.wrap.or(self.wrap),
            support: o.support.or(self.support),
        }
    }
}
#[derive(Deserialize, Debug, Clone)]
#[serde(deny_unknown_fields)]
pub struct Project {
    pub title: String,
    pub goal: String,
    /// What the project must do to count as a success.
    #[serde(default)]
    pub minimum: Option<String>,
    #[serde(default)]
    pub language: Option<String>,
    #[serde(default)]
    pub difficulty: Option<u8>,
    #[serde(default)]
    pub prereqs: Vec<String>,
    #[serde(default)]
    pub concepts: Vec<String>,
}

#[derive(Deserialize, Debug, Clone, Copy, PartialEq, Eq, Default)]
#[serde(rename_all = "lowercase")]
pub enum Target {
    /// proctor loads the functions and calls them from its own main.
    #[default]
    Function,
    /// proctor runs the student's program as a whole.
    Program,
}

#[derive(Deserialize, Debug, Clone, Copy, PartialEq, Eq, Default)]
#[serde(rename_all = "lowercase")]
pub enum Scaffold {
    None,
    #[default]
    Minimal,
    Full,
}

#[derive(Deserialize, Debug, Clone)]
#[serde(deny_unknown_fields)]
pub struct Func {
    pub name: String,
    pub sig: String,
    #[serde(default)]
    pub returns: String,
}

#[derive(Deserialize, Debug, Clone, Copy, PartialEq, Eq, Default)]
#[serde(rename_all = "snake_case")]
pub enum Rule {
    #[default]
    Required,
    OneOrMore,
    Free,
    Forbidden,
}

#[derive(Deserialize, Debug, Clone)]
#[serde(deny_unknown_fields)]
pub struct FileRule {
    pub path: String,
    #[serde(default)]
    pub rule: Rule,
    #[serde(default)]
    pub note: String,
}

#[derive(Deserialize, Debug, Clone)]
#[serde(untagged)]
pub enum Requirements {
    Text(String),
    List(Vec<String>),
}

impl Default for Requirements {
    fn default() -> Self {
        Requirements::List(Vec::new())
    }
}

impl Requirements {
    pub fn is_empty(&self) -> bool {
        match self {
            Requirements::Text(t) => t.trim().is_empty(),
            Requirements::List(l) => l.iter().all(|x| x.trim().is_empty()),
        }
    }

    /// The whole section as markdown.
    pub fn markdown(&self) -> String {
        match self {
            Requirements::Text(t) => format!("{}\n", t.trim()),
            Requirements::List(l) => l.iter().map(|r| format!("- {}\n", r.trim())).collect(),
        }
    }
}

#[derive(Deserialize, Debug, Clone)]
#[serde(deny_unknown_fields)]
pub struct StaticCheck {
    pub name: String,
    pub cmd: String,
}

#[derive(Deserialize, Debug, Clone)]
#[serde(untagged)]
pub enum Fuzz {
    Name(String),
    Spec(FuzzSpec),
}

#[derive(Deserialize, Debug, Clone, Default)]
#[serde(deny_unknown_fields)]
pub struct FuzzSpec {
    /// Function target: the function to compare against the reference.
    #[serde(default, rename = "fn")]
    pub func: Option<String>,
    /// Program target: a name for the test.
    #[serde(default)]
    pub name: Option<String>,
    /// Function target: generator per parameter name (overrides defaults).
    #[serde(default)]
    pub args: BTreeMap<String, Yaml>,
    /// Program target: generator for the argument list (list or string).
    #[serde(default)]
    pub argv: Option<Yaml>,
    /// Program target: generator for stdin.
    #[serde(default)]
    pub stdin: Option<Yaml>,
    #[serde(default)]
    pub rounds: Option<u32>,
}

#[derive(Deserialize, Debug, Clone, Default)]
#[serde(deny_unknown_fields)]
pub struct Law {
    #[serde(default)]
    pub name: Option<String>,
    #[serde(default, rename = "gen")]
    pub vars: BTreeMap<String, Yaml>,
    #[serde(default)]
    pub check: Vec<String>,
    /// `[f, g]`: g(f(x, ...)) == x
    #[serde(default)]
    pub roundtrip: Option<Vec<String>>,
    /// `f`: f(f(x)) == f(x)
    #[serde(default)]
    pub idempotent: Option<String>,
    /// `[f, g]`: f(args) == g(args)
    #[serde(default)]
    pub agree: Option<Vec<String>>,
    #[serde(default)]
    pub rounds: Option<u32>,
    /// Per-round time limit, for performance laws.
    #[serde(default)]
    pub time_ms: Option<u64>,
}

#[derive(Deserialize, Debug, Clone, Default)]
#[serde(deny_unknown_fields)]
pub struct Random {
    #[serde(default)]
    pub rounds: Option<u32>,
    #[serde(default)]
    pub fuzz: Vec<Fuzz>,
    #[serde(default)]
    pub laws: Vec<Law>,
}

#[derive(Deserialize, Debug, Clone)]
#[serde(deny_unknown_fields)]
pub struct Unit {
    pub id: String,
    pub title: String,
    /// Set for project stages: the project id (also the workspace name).
    #[serde(default)]
    pub project: Option<String>,
    #[serde(default)]
    pub language: Option<String>,
    #[serde(default)]
    pub difficulty: Option<u8>,
    #[serde(default)]
    pub prereqs: Vec<String>,
    #[serde(default)]
    pub concepts: Vec<String>,
    #[serde(default)]
    pub introduces: Vec<String>,
    #[serde(default)]
    pub assumes: Vec<String>,
    /// Note files (in `.proctor/notes/`) revealed with this unit.
    #[serde(default)]
    pub notes: Vec<String>,
    /// How hard the ideas are to understand (`difficulty` is implementation).
    #[serde(default)]
    pub concept_difficulty: Option<u8>,
    /// Planned hours, for the schedule in PROGRESS.md. Never shown in instructions.
    #[serde(default)]
    pub hours: Option<f64>,
    /// Optional unit: the student may skip it with `proctor skip`.
    #[serde(default)]
    pub stretch: bool,
    /// `linux`: needs Linux APIs; on other systems proctor builds, runs
    /// and tests it in a Linux container (Docker). Absent: any system.
    #[serde(default)]
    pub platform: Option<String>,
    #[serde(default)]
    pub scaffold: Scaffold,
    #[serde(default)]
    pub target: Target,
    pub goal: String,
    /// Markdown shown right after the goal: a step-by-step walkthrough for
    /// guided units (first contact with a language or a tool).
    #[serde(default)]
    pub lesson: Option<String>,
    /// The lesson is a step-by-step walkthrough (first contact with a language).
    #[serde(default)]
    pub guided: bool,
    /// Program target: what a run prints, shown when no test case gives an
    /// exact output to use as an example.
    #[serde(default)]
    pub example: Option<String>,
    /// Edge-case categories worth deciding about, shown without answers.
    #[serde(default)]
    pub think_about: Vec<String>,
    #[serde(default)]
    pub functions: Vec<Func>,
    #[serde(default)]
    pub files: Vec<FileRule>,
    /// Only what the functions table, Allowed, the examples and RULES.md
    /// cannot say: markdown text, or a list of bullets.
    #[serde(default)]
    pub requirements: Requirements,
    /// Absent = unrestricted. Present = strict whitelist (built-ins implied).
    #[serde(default)]
    pub allowed: Option<Vec<String>>,
    /// Items the exercise's code may not use even though `allowed` covers
    /// them (`std` allowed, `std::mem::swap` forbidden). Prefix match; the
    /// student's tests in `tests/` are not checked.
    #[serde(default)]
    pub forbidden: Vec<String>,
    #[serde(default)]
    pub environment: Option<String>,
    #[serde(default)]
    pub env: BTreeMap<String, String>,
    /// Python: default module for undotted function names.
    #[serde(default)]
    pub module: Option<String>,
    /// Where `proctor next` writes empty mandatory functions.
    #[serde(default)]
    pub stubs: Option<String>,
    /// Program target: how to run the program ({exe}, {python}, {cargo_bin}).
    #[serde(default)]
    pub run: Option<String>,
    /// Overrides the preset build command.
    #[serde(default)]
    pub build: Option<String>,
    #[serde(default, rename = "static")]
    pub static_checks: Vec<StaticCheck>,
    /// Extra import-detection regexes (group 1 = module name), for languages
    /// without a built-in scanner.
    #[serde(default)]
    pub imports: Vec<String>,
    /// Test-only functions provided by support files (never required from
    /// the student, never shown), e.g. a switch that makes malloc fail.
    #[serde(default)]
    pub helpers: Vec<Func>,
    /// Overrides the language's wrap for this unit.
    #[serde(default)]
    pub wrap: Option<String>,
    /// Added to the language's `support` files.
    #[serde(default)]
    pub support: Vec<String>,
    /// Appended to the language's compiler flags for this unit, e.g.
    /// `-Dmalloc=sc_malloc` to route allocations through a support shim.
    #[serde(default)]
    pub extra_flags: Vec<String>,
    #[serde(default)]
    pub cases: Option<String>,
    #[serde(default)]
    pub random: Random,
    #[serde(default)]
    pub timeout_ms: Option<u64>,
}

impl Unit {
    pub fn is_stage(&self) -> bool {
        self.project.is_some()
    }

    /// Non-empty, non-comment lines of `cases`, with their 1-based line numbers.
    pub fn case_lines(&self) -> Vec<(usize, String)> {
        self.cases
            .as_deref()
            .unwrap_or("")
            .lines()
            .enumerate()
            .map(|(i, l)| (i + 1, l.trim().to_string()))
            .filter(|(_, l)| !l.is_empty() && !l.starts_with('#'))
            .collect()
    }

    pub fn has_tests(&self) -> bool {
        !self.case_lines().is_empty()
            || !self.random.fuzz.is_empty()
            || !self.random.laws.is_empty()
    }
}
