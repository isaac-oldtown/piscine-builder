//! One `skillcheck check`: structure, prototypes, allowed, build, tests,
//! regression. Results stream to a `Reporter` as they happen.

use crate::course::CourseDir;
use crate::expr::{self, EvalError, Expr, Host, Outcome};
use crate::gens::{self, Gen, Rng};
use crate::harness::Harness;
use crate::lang::{self, BuildError, Ctx};
use crate::program::{self, Program};
use crate::run::{copy_tree, split_words};
use crate::scan::{self, ProtoFinding};
use crate::sig::{self, Proto};
use crate::structure;
use crate::unit::{Fuzz, Law, Target, Unit};
use crate::value::Value;
use std::collections::{BTreeMap, BTreeSet};
use std::fs;
use std::path::{Path, PathBuf};
use std::time::{Duration, Instant};

pub const DEFAULT_ROUNDS: u32 = 200;
pub const DEFAULT_TIMEOUT_MS: u64 = 10_000;
const SHRINK_BUDGET: usize = 300;

#[derive(Clone, Debug)]
pub struct Item {
    pub ok: bool,
    pub name: String,
    pub detail: String,
    /// Randomized: the seed replays it.
    pub random: bool,
    /// The spec, not the student, is at fault.
    pub course_error: bool,
}

impl Item {
    fn pass(name: impl Into<String>, detail: impl Into<String>) -> Item {
        Item {
            ok: true,
            name: name.into(),
            detail: detail.into(),
            random: false,
            course_error: false,
        }
    }
    fn fail(name: impl Into<String>, detail: impl Into<String>) -> Item {
        Item {
            ok: false,
            name: name.into(),
            detail: detail.into(),
            random: false,
            course_error: false,
        }
    }
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Status {
    Pass,
    Fail,
    Skip,
}

#[derive(Clone, Debug)]
pub struct Phase {
    pub name: &'static str,
    pub status: Status,
    pub summary: String,
    pub items: Vec<Item>,
    /// Items were streamed one by one (tests); otherwise only failures show.
    pub streamed: bool,
}

pub struct Verdict {
    pub ok: bool,
    pub passed: usize,
    pub total: usize,
    pub first_fail: Option<(String, Item)>,
    pub warnings: usize,
    pub phases: Vec<Phase>,
}

pub trait Reporter {
    fn start(&mut self, unit: &Unit, seed: u64, against: &str);
    fn phase_begin(&mut self, name: &str, streamed: bool);
    fn item(&mut self, item: &Item);
    fn phase_end(&mut self, phase: &Phase);
    fn log(&mut self, text: &str);
}

pub enum Against {
    Student,
    Reference,
    Dir(PathBuf),
}

pub struct Options {
    pub seed: u64,
    pub against: Against,
    /// Only build and run tests (self-test against stubs).
    pub tests_only: bool,
}

// ------------------------------------------------------------------ tests

pub enum Kind {
    Check {
        expr: Expr,
    },
    Prog(program::Case),
    Fuzz {
        func: String,
        params: Vec<(String, Gen)>,
        rounds: u32,
    },
    FuzzProg {
        argv: Option<Gen>,
        stdin: Option<Gen>,
        rounds: u32,
    },
    Law {
        vars: Vec<(String, Gen)>,
        checks: Vec<(String, Expr)>,
        rounds: u32,
        time_ms: Option<u64>,
    },
}

pub struct Test {
    pub name: String,
    pub kind: Kind,
    pub target: Target,
}

impl Test {
    pub fn is_random(&self) -> bool {
        !matches!(self.kind, Kind::Check { .. } | Kind::Prog(_))
    }
}

/// Test-only helper functions of `units` (provided by support files).
pub fn helpers_for(c: &CourseDir, units: &[&Unit]) -> Result<Vec<Proto>, String> {
    let mut out: Vec<Proto> = Vec::new();
    for u in units {
        let lang = c.language(u);
        for f in &u.helpers {
            let p =
                sig::parse(&lang, &f.sig, &f.name).map_err(|e| format!("{}: helper {e}", u.id))?;
            out.retain(|x| x.name != p.name);
            out.push(p);
        }
    }
    Ok(out)
}

pub fn protos_for(c: &CourseDir, units: &[&Unit]) -> Result<Vec<Proto>, String> {
    let mut out: Vec<Proto> = Vec::new();
    for u in units {
        let lang = c.language(u);
        for f in &u.functions {
            let p = sig::parse(&lang, &f.sig, &f.name).map_err(|e| format!("{}: {e}", u.id))?;
            out.retain(|x| x.name != p.name);
            out.push(p);
        }
    }
    Ok(out)
}

fn arities(protos: &[Proto]) -> Vec<expr::Arity> {
    protos
        .iter()
        .map(|p| {
            let max = p.params.len();
            let min = p.params.iter().filter(|x| !x.has_default).count();
            // A method's receiver may or may not be passed: no count check.
            let (min, max) = if p.is_method {
                (0, usize::MAX)
            } else {
                (min, max)
            };
            (p.name.clone(), min, max)
        })
        .collect()
}

/// Parses one check and validates its names against the expected functions.
fn parse_check(src: &str, arities: &[expr::Arity], vars: &[String]) -> Result<expr::Expr, String> {
    let e = expr::resolve_methods(expr::parse(src)?, arities)?;
    expr::static_check(&e, arities, vars)?;
    Ok(e)
}

fn rounds_of(u: &Unit, own: Option<u32>) -> u32 {
    own.or(u.random.rounds).unwrap_or(DEFAULT_ROUNDS).max(1)
}

fn parse_gen(c: &CourseDir, y: &yaml_serde::Value) -> Result<Gen, String> {
    gens::parse(y, &c.lib)
}

/// Builds the test list of one unit. Errors are spec errors.
pub fn tests_of(c: &CourseDir, u: &Unit, protos: &[Proto]) -> Result<Vec<Test>, String> {
    let mut out = Vec::new();
    let find = |name: &str| protos.iter().find(|p| p.name == name);
    let arities = arities(protos);
    for (n, line) in u.case_lines() {
        let short = crate::report::truncate(&line, 70);
        let kind = match u.target {
            Target::Function => Kind::Check {
                expr: parse_check(&line, &arities, &[])
                    .map_err(|e| format!("{} cases line {n}: {e}", u.id))?,
            },
            Target::Program => Kind::Prog(
                program::parse_case(&line).map_err(|e| format!("{} cases line {n}: {e}", u.id))?,
            ),
        };
        out.push(Test {
            name: format!("case {short}"),
            kind,
            target: u.target,
        });
    }
    for f in &u.random.fuzz {
        let spec = match f {
            Fuzz::Name(name) if u.target == Target::Function => crate::unit::FuzzSpec {
                func: Some(name.clone()),
                ..Default::default()
            },
            Fuzz::Name(name) => crate::unit::FuzzSpec {
                name: Some(name.clone()),
                ..Default::default()
            },
            Fuzz::Spec(s) => s.clone(),
        };
        let rounds = rounds_of(u, spec.rounds);
        if u.target == Target::Function {
            let fname = spec
                .func
                .clone()
                .ok_or_else(|| format!("{}: fuzz entry needs `fn:`", u.id))?;
            let p = find(&fname)
                .ok_or_else(|| format!("{}: fuzz `{fname}` is not an expected function", u.id))?;
            let mut params = Vec::new();
            for prm in &p.params {
                let g = match spec.args.get(&prm.name) {
                    Some(y) => parse_gen(c, y).map_err(|e| format!("{}: fuzz {fname} arg {}: {e}", u.id, prm.name))?,
                    None => sig::default_gen(&prm.ty).ok_or_else(|| {
                        format!("{}: fuzz {fname}: no default generator for `{}: {}`; give one under args:", u.id, prm.name, prm.raw)
                    })?,
                };
                params.push((prm.name.clone(), g));
            }
            for k in spec.args.keys() {
                if !p.params.iter().any(|x| &x.name == k) {
                    return Err(format!("{}: fuzz {fname}: `{k}` is not a parameter", u.id));
                }
            }
            out.push(Test {
                name: format!("fuzz {fname}"),
                kind: Kind::Fuzz {
                    func: fname,
                    params,
                    rounds,
                },
                target: u.target,
            });
        } else {
            let argv = spec
                .argv
                .as_ref()
                .map(|y| parse_gen(c, y))
                .transpose()
                .map_err(|e| format!("{}: fuzz argv: {e}", u.id))?;
            let stdin = spec
                .stdin
                .as_ref()
                .map(|y| parse_gen(c, y))
                .transpose()
                .map_err(|e| format!("{}: fuzz stdin: {e}", u.id))?;
            if argv.is_none() && stdin.is_none() {
                return Err(format!(
                    "{}: a program fuzz entry needs `argv:` and/or `stdin:`",
                    u.id
                ));
            }
            let name = spec.name.clone().unwrap_or_else(|| "program".into());
            out.push(Test {
                name: format!("fuzz {name}"),
                kind: Kind::FuzzProg {
                    argv,
                    stdin,
                    rounds,
                },
                target: u.target,
            });
        }
    }
    for (i, law) in u.random.laws.iter().enumerate() {
        out.push(law_test(c, u, law, i, protos)?);
    }
    Ok(out)
}

fn law_test(
    c: &CourseDir,
    u: &Unit,
    law: &Law,
    i: usize,
    protos: &[Proto],
) -> Result<Test, String> {
    let find = |name: &str| {
        protos.iter().find(|p| p.name == name).ok_or_else(|| {
            format!(
                "{}: law uses `{name}`, which is not an expected function",
                u.id
            )
        })
    };
    let mut vars: Vec<(String, Gen)> = Vec::new();
    for (k, y) in &law.vars {
        vars.push((
            k.clone(),
            parse_gen(c, y).map_err(|e| format!("{}: law gen `{k}`: {e}", u.id))?,
        ));
    }
    let defaults = |p: &Proto, vars: &mut Vec<(String, Gen)>| -> Result<Vec<String>, String> {
        let mut names = Vec::new();
        for prm in &p.params {
            if !vars.iter().any(|(n, _)| n == &prm.name) {
                let g = sig::default_gen(&prm.ty).ok_or_else(|| {
                    format!(
                        "{}: law: no default generator for `{}: {}`; add it under gen:",
                        u.id, prm.name, prm.raw
                    )
                })?;
                vars.push((prm.name.clone(), g));
            }
            names.push(prm.name.clone());
        }
        Ok(names)
    };
    let mut checks: Vec<String> = law.check.clone();
    let mut label = law.name.clone();
    if let Some(pair) = &law.roundtrip {
        let [f, g] = pair.as_slice() else {
            return Err(format!("{}: roundtrip takes [f, g]", u.id));
        };
        let names = defaults(find(f)?, &mut vars)?;
        let first = names
            .first()
            .ok_or_else(|| format!("{}: roundtrip: `{f}` takes no argument", u.id))?;
        checks.push(format!("{g}({f}({})) == {first}", names.join(", ")));
        label.get_or_insert(format!("roundtrip {f} / {g}"));
    }
    if let Some(f) = &law.idempotent {
        let names = defaults(find(f)?, &mut vars)?;
        if names.len() != 1 {
            return Err(format!(
                "{}: idempotent `{f}` must take exactly one argument",
                u.id
            ));
        }
        checks.push(format!("{f}({f}({0})) == {f}({0})", names[0]));
        label.get_or_insert(format!("idempotent {f}"));
    }
    if let Some(fs_) = &law.agree {
        let [f, g] = fs_.as_slice() else {
            return Err(format!("{}: agree takes [f, g]", u.id));
        };
        let names = defaults(find(f)?, &mut vars)?;
        checks.push(format!("{f}({0}) == {g}({0})", names.join(", ")));
        label.get_or_insert(format!("agree {f} / {g}"));
    }
    if checks.is_empty() {
        return Err(format!("{}: law #{} has no check", u.id, i + 1));
    }
    let mut parsed = Vec::new();
    let arities = arities(protos);
    let names: Vec<String> = vars.iter().map(|(n, _)| n.clone()).collect();
    for src in checks {
        let e = parse_check(&src, &arities, &names)
            .map_err(|e| format!("{}: law check `{src}`: {e}", u.id))?;
        parsed.push((src, e));
    }
    let label = label.unwrap_or_else(|| format!("law {}", i + 1));
    Ok(Test {
        name: format!("law {label}"),
        kind: Kind::Law {
            vars,
            checks: parsed,
            rounds: rounds_of(u, law.rounds),
            time_ms: law.time_ms,
        },
        target: u.target,
    })
}

// ------------------------------------------------------------------- hosts

struct EvalHost<'a> {
    vars: &'a [(String, Value)],
    harness: Option<&'a mut Harness>,
    program: Option<&'a Program>,
}

fn args_short(args: &[Value]) -> String {
    args.iter()
        .map(|a| a.short(40))
        .collect::<Vec<_>>()
        .join(", ")
}

impl Host for EvalHost<'_> {
    fn var(&self, name: &str) -> Option<Value> {
        self.vars
            .iter()
            .find(|(n, _)| n == name)
            .map(|(_, v)| v.clone())
    }
    fn call(&mut self, name: &str, args: expr::Args) -> Option<Result<Value, EvalError>> {
        if name == "run" {
            return Some(match self.program {
                Some(p) => p.call(args),
                None => Err(EvalError::Spec(
                    "run() is only available for program targets".into(),
                )),
            });
        }
        let h = self.harness.as_mut()?;
        if !h.funcs.contains(name) {
            return None;
        }
        let vals: Vec<Value> = args.into_iter().map(|(_, v)| v).collect();
        Some(h.call(name, &vals).map_err(|e| {
            EvalError::Student(format!("{}({}) {}", name, args_short(&vals), e.message()))
        }))
    }
    fn is_external(&self, name: &str) -> bool {
        name == "run"
            || self
                .harness
                .as_ref()
                .is_some_and(|h| h.funcs.contains(name))
    }
}

fn describe_vars(vars: &[(String, Value)]) -> String {
    vars.iter()
        .map(|(n, v)| format!("{n} = {}", v.short(60)))
        .collect::<Vec<_>>()
        .join(", ")
}

/// Greedy shrinking: keep any smaller value that still fails.
fn shrink(
    gens: &[(String, Gen)],
    values: &mut Vec<(String, Value)>,
    mut fails: impl FnMut(&[(String, Value)]) -> bool,
) {
    let mut budget = SHRINK_BUDGET;
    loop {
        let mut improved = false;
        'vars: for i in 0..values.len() {
            let Some((_, g)) = gens.iter().find(|(n, _)| *n == values[i].0) else {
                continue;
            };
            for cand in g.shrink(&values[i].1) {
                if budget == 0 {
                    return;
                }
                budget -= 1;
                let mut trial = values.clone();
                trial[i].1 = cand;
                if fails(&trial) {
                    *values = trial;
                    improved = true;
                    break 'vars;
                }
            }
        }
        if !improved {
            return;
        }
    }
}

// -------------------------------------------------------------- the runner

struct Built {
    harness: Option<Harness>,
    program: Option<Program>,
}

struct Reference {
    built: Option<Result<Built, String>>,
}

pub struct Engine<'a> {
    c: &'a CourseDir,
    unit: &'a Unit,
    lang: String,
    opts: &'a Options,
    env: BTreeMap<String, String>,
    timeout: Duration,
}

impl<'a> Engine<'a> {
    pub fn new(c: &'a CourseDir, unit: &'a Unit, opts: &'a Options) -> Engine<'a> {
        let env = c.env(unit);
        Engine {
            c,
            unit,
            lang: c.language(unit),
            opts,
            env,
            timeout: Duration::from_millis(c.timeout_ms(unit).unwrap_or(DEFAULT_TIMEOUT_MS)),
        }
    }

    fn source_dir(&self) -> PathBuf {
        match &self.opts.against {
            Against::Student => self.c.workspace(self.unit),
            Against::Reference => self.c.ref_dir(self.unit),
            Against::Dir(p) => p.clone(),
        }
    }

    fn cache_key(&self, what: &str) -> String {
        format!("{what}-{}", self.unit.id)
    }

    /// Prepares a buildable tree for `src`: in place for Rust, else a fresh copy.
    fn stage(&self, src: &Path, key: &str) -> Result<(PathBuf, PathBuf, PathBuf), String> {
        let (copy, build, base) = lang::work_paths(&self.c.hidden().join("cache"), key);
        if lang::builds_in_place(&self.lang) {
            return Ok((src.to_path_buf(), build, base));
        }
        let _ = fs::remove_dir_all(&copy);
        copy_tree(src, &copy)?;
        Ok((copy, build, base))
    }

    fn build(
        &self,
        ws: &Path,
        work: &Path,
        cache: &Path,
        protos: &[Proto],
        need_fn: bool,
        need_prog: bool,
    ) -> Result<(Built, String), BuildError> {
        let mut cfg = self.c.lang_config(self.unit);
        let extra: Vec<String> = self
            .c
            .chain(self.unit)
            .iter()
            .flat_map(|u| u.extra_flags.clone())
            .collect();
        if !extra.is_empty() {
            let mut flags = cfg
                .flags
                .clone()
                .unwrap_or_else(|| lang::C_FLAGS.iter().map(|s| s.to_string()).collect());
            flags.extend(extra);
            cfg.flags = Some(flags);
        }
        let support = self.c.support(self.unit);
        let wrap = self.c.wrap(self.unit);
        let ctx = Ctx {
            lang: &self.lang,
            ws,
            work,
            env: &self.env,
            cache,
            cfg: &cfg,
            support: &support,
            wrap: wrap.as_deref(),
        };
        if self.lang == "rust" {
            // Cargo trusts mtimes; a copied file can look older than the last
            // build. Always rebuild the graded crate itself (deps stay cached).
            if let Ok(pkg) = lang::cargo_package(ws) {
                let mut c = std::process::Command::new("cargo");
                c.args(["clean", "--quiet", "-p", &pkg])
                    .current_dir(ws)
                    .env("CARGO_TARGET_DIR", cache.join("target"));
                let _ = crate::run::run(&mut c, None, lang::BUILD_TIMEOUT);
            }
        }
        let mut built = Built {
            harness: None,
            program: None,
        };
        let mut log = String::new();
        if need_prog {
            let (cmd, l) = lang::build_program(
                &ctx,
                self.unit_run().as_deref(),
                self.c.build_cmd(self.unit).as_deref(),
            )?;
            log.push_str(&l);
            built.program = Some(Program {
                cmd,
                cwd: ws.to_path_buf(),
                env: self.env.clone(),
                timeout: self.timeout,
            });
        }
        if need_fn {
            let module = self
                .c
                .chain(self.unit)
                .iter()
                .rev()
                .find_map(|u| u.module.clone());
            let (h, l) = lang::build_harness(&ctx, protos, module.as_deref(), self.timeout)?;
            log.push_str(&l);
            built.harness = Some(h);
        }
        Ok((built, log))
    }

    fn src_opts(&self) -> scan::SrcOpts {
        let l = self.c.lang_config(self.unit);
        scan::SrcOpts {
            exts: l.extensions.unwrap_or_default(),
            comment: l.comment,
        }
    }

    fn unit_run(&self) -> Option<String> {
        self.c.run_cmd(self.unit)
    }

    pub fn check(&self, rep: &mut dyn Reporter) -> Result<Verdict, String> {
        let chain = self.c.chain(self.unit);
        let protos = protos_for(self.c, &chain)?;
        // What the harness exposes: the student's functions plus test helpers.
        let mut callable = protos.clone();
        callable.extend(helpers_for(self.c, &chain)?);
        let mut unit_tests: Vec<(&Unit, Vec<Test>)> = Vec::new();
        for u in &chain {
            unit_tests.push((u, tests_of(self.c, u, &callable)?));
        }
        let against = match &self.opts.against {
            Against::Student => "your work".to_string(),
            Against::Reference => "the reference solution".to_string(),
            Against::Dir(p) => p.display().to_string(),
        };
        rep.start(self.unit, self.opts.seed, &against);
        let mut phases: Vec<Phase> = Vec::new();
        let mut blocked = false;
        let mut warnings = 0;
        let src = self.source_dir();

        // structure
        if !src.is_dir() {
            let ws_name = self.c.workspace_name(self.unit);
            let p = Phase {
                name: "structure",
                status: Status::Fail,
                summary: format!("{ws_name}/ not found"),
                items: vec![Item::fail(
                    format!("{ws_name}/"),
                    "the workspace directory does not exist",
                )],
                streamed: false,
            };
            rep.phase_end(&p);
            phases.push(p);
            return Ok(self.verdict(phases, 0));
        }
        if !self.unit.files.is_empty() && !self.opts.tests_only {
            let items: Vec<Item> = structure::check(&src, &self.unit.files)
                .into_iter()
                .map(|f| Item {
                    ok: f.ok,
                    name: f.name,
                    detail: f.detail,
                    random: false,
                    course_error: false,
                })
                .collect();
            let p = phase("structure", items, false, |n, t| format!("{n}/{t} rules"));
            blocked |= p.status == Status::Fail;
            rep.phase_end(&p);
            phases.push(p);
        }

        // prototypes
        if !protos.is_empty() && !self.opts.tests_only {
            let srcs = scan::sources_ext(&self.lang, &src, &self.src_opts());
            let items: Vec<Item> = protos
                .iter()
                .map(|p| match scan::find_prototype(&self.lang, &srcs, p) {
                    ProtoFinding::Ok => Item::pass(p.name.clone(), "found"),
                    ProtoFinding::Missing => Item::fail(
                        p.name.clone(),
                        format!("not found (expected `{}`)", p.sig_text),
                    ),
                    ProtoFinding::Mismatch(m) => {
                        Item::fail(p.name.clone(), format!("expected `{}`, {m}", p.norm))
                    }
                })
                .collect();
            let p = phase("prototypes", items, false, |_, _| {
                protos
                    .iter()
                    .map(|p| p.short_name().to_string())
                    .collect::<Vec<_>>()
                    .join(", ")
            });
            blocked |= p.status == Status::Fail;
            rep.phase_end(&p);
            phases.push(p);
        }

        // allowed
        let allowed: Option<Vec<String>> = if self.unit.allowed.is_some() && !self.opts.tests_only {
            let mut all: BTreeSet<String> = BTreeSet::new();
            for u in &chain {
                all.extend(u.allowed.clone().unwrap_or_default());
            }
            Some(all.into_iter().collect())
        } else {
            None
        };
        if let Some(list) = allowed {
            let extra = self.c.import_patterns(self.unit);
            let opts = self.src_opts();
            let r = scan::check_allowed(&self.lang, &src, &list, &extra, &opts)?;
            let checked = r.checked;
            let items: Vec<Item> = if r.violations.is_empty() {
                vec![Item::pass("whitelist", format!("{checked} use(s) checked"))]
            } else {
                r.violations
                    .into_iter()
                    .map(|v| Item::fail(v.what.clone(), format!("at {} is not in Allowed", v.at)))
                    .collect()
            };
            let p = phase("allowed", items, false, move |_, _| {
                if checked == 0 {
                    "only built-ins and your own code".into()
                } else {
                    format!("{checked} use(s) checked, all allowed")
                }
            });
            rep.phase_end(&p);
            phases.push(p);
        }

        if blocked {
            for name in ["build", "tests"] {
                let p = Phase {
                    name,
                    status: Status::Skip,
                    summary: "skipped (fix the steps above first)".into(),
                    items: Vec::new(),
                    streamed: false,
                };
                rep.phase_end(&p);
                phases.push(p);
            }
            return Ok(self.verdict(phases, warnings));
        }

        // build
        let need_fn = unit_tests
            .iter()
            .any(|(_, ts)| ts.iter().any(|t| t.target == Target::Function))
            && !protos.is_empty();
        let need_prog = chain.iter().any(|u| u.target == Target::Program);
        let key = self.cache_key(match self.opts.against {
            Against::Student => "student",
            Against::Reference => "ref",
            Against::Dir(_) => "dir",
        });
        let (ws, work, base) = self.stage(&src, &key)?;
        let mut items = Vec::new();
        let mut built = Built {
            harness: None,
            program: None,
        };
        let mut build_ok = true;
        if need_fn || need_prog || lang::has_harness(&self.lang) || self.lang == "bash" {
            match self.build(&ws, &work, &base, &callable, need_fn, need_prog) {
                Ok((b, log)) => {
                    warnings += lang::count_warnings(&log);
                    let what = match (need_fn, need_prog) {
                        (true, true) => "functions and program",
                        (true, false) => "functions loaded by skillcheck's own main",
                        (false, true) => "program",
                        _ => "sources",
                    };
                    items.push(Item::pass("compile", what));
                    built = b;
                }
                Err(e) => {
                    rep.log(&e.log);
                    items.push(Item::fail("compile", e.msg));
                    build_ok = false;
                }
            }
        }
        if build_ok && !self.opts.tests_only {
            for (name, cmd) in self.static_checks() {
                let words = split_words(&cmd);
                if words.is_empty() {
                    continue;
                }
                let mut command = std::process::Command::new(lang::resolve(&words[0]));
                command.args(&words[1..]).current_dir(&ws).envs(&self.env);
                if self.lang == "rust" {
                    command.env("CARGO_TARGET_DIR", base.join("target"));
                }
                match crate::run::run(&mut command, None, lang::BUILD_TIMEOUT) {
                    Ok(o) if o.success => items.push(Item::pass(name, "clean")),
                    Ok(o) => {
                        let all = format!("{}{}", o.stdout, o.stderr);
                        rep.log(&all);
                        let missing =
                            all.contains("no such command") || all.contains("is not installed");
                        let msg = if missing {
                            format!(
                                "`{}` is not installed",
                                words[..2.min(words.len())].join(" ")
                            )
                        } else if name == "fmt" {
                            all.lines()
                                .find(|l| l.starts_with("Diff in "))
                                .map(|l| {
                                    format!(
                                        "not formatted: {}",
                                        relativize(
                                            l.trim_start_matches("Diff in ").trim_end_matches(':'),
                                            &ws
                                        )
                                    )
                                })
                                .unwrap_or_else(|| "not formatted (run the formatter)".into())
                        } else {
                            lang::first_error(&relativize(&all, &ws))
                                .unwrap_or_else(|| "failed".into())
                        };
                        items.push(Item::fail(name, msg));
                    }
                    Err(e) => items.push(Item::fail(name, e)),
                }
            }
        }
        if !items.is_empty() {
            let p = phase("build", items, false, |_, _| {
                if warnings > 0 {
                    format!("ok ({warnings} warning(s))")
                } else {
                    "ok, no warning".into()
                }
            });
            blocked |= p.status == Status::Fail;
            rep.phase_end(&p);
            phases.push(p);
        }
        if blocked {
            let p = Phase {
                name: "tests",
                status: Status::Skip,
                summary: "skipped (the build failed)".into(),
                items: Vec::new(),
                streamed: false,
            };
            rep.phase_end(&p);
            phases.push(p);
            return Ok(self.verdict(phases, warnings));
        }

        // tests, then regression
        let mut reference = Reference { built: None };
        let n = unit_tests.len();
        // Current stage first, then regression on earlier stages in order.
        let order: Vec<usize> = std::iter::once(n - 1).chain(0..n - 1).collect();
        for idx in order {
            let (u, tests) = &unit_tests[idx];
            let is_current = idx + 1 == n;
            if tests.is_empty() {
                continue;
            }
            let name: &'static str = if is_current { "tests" } else { "regression" };
            rep.phase_begin(name, true);
            let mut items = Vec::new();
            for t in tests {
                let mut item = self.run_test(t, &mut built, &mut reference, &callable);
                if !is_current {
                    item.name =
                        format!("{} {}", u.id.split('-').next().unwrap_or(&u.id), item.name);
                }
                rep.item(&item);
                items.push(item);
            }
            let p = phase(name, items, true, |ok, total| format!("{ok}/{total}"));
            rep.phase_end(&p);
            phases.push(p);
        }
        // Leak checkers and sanitizers report when the process ends.
        if let Some(msg) = built.harness.as_mut().and_then(Harness::finish) {
            let p = phase(
                "exit",
                vec![Item::fail("clean exit", msg)],
                false,
                |_, _| String::new(),
            );
            rep.phase_end(&p);
            phases.push(p);
        }
        Ok(self.verdict(phases, warnings))
    }

    fn static_checks(&self) -> Vec<(String, String)> {
        self.c
            .static_checks(self.unit)
            .into_iter()
            .map(|s| (s.name, s.cmd))
            .collect()
    }

    fn verdict(&self, phases: Vec<Phase>, warnings: usize) -> Verdict {
        let mut passed = 0;
        let mut total = 0;
        let mut first_fail = None;
        for p in &phases {
            if p.status == Status::Skip {
                continue;
            }
            for i in &p.items {
                total += 1;
                if i.ok {
                    passed += 1;
                } else if first_fail.is_none() {
                    first_fail = Some((p.name.to_string(), i.clone()));
                }
            }
        }
        let ok = first_fail.is_none() && phases.iter().all(|p| p.status != Status::Fail);
        Verdict {
            ok,
            passed,
            total,
            first_fail,
            warnings,
            phases,
        }
    }

    fn reference<'r>(
        &self,
        r: &'r mut Reference,
        protos: &[Proto],
    ) -> Result<&'r mut Built, String> {
        if r.built.is_none() {
            let res = (|| -> Result<Built, String> {
                let dir = self.c.ref_dir(self.unit);
                if !dir.is_dir() {
                    return Err(format!(
                        "no reference solution at .skillcheck/refs/{}",
                        self.unit.id
                    ));
                }
                let key = self.cache_key("oracle");
                let (ws, work, base) = self.stage(&dir, &key)?;
                let chain = self.c.chain(self.unit);
                let need_fn = !protos.is_empty()
                    && chain
                        .iter()
                        .any(|u| u.target == Target::Function && u.has_tests());
                let need_prog = chain.iter().any(|u| u.target == Target::Program);
                self.build(&ws, &work, &base, protos, need_fn, need_prog)
                    .map(|(b, _)| b)
                    .map_err(|e| format!("the reference solution does not build: {}", e.msg))
            })();
            r.built = Some(res);
        }
        match r.built.as_mut() {
            Some(Ok(b)) => Ok(b),
            Some(Err(e)) => Err(e.clone()),
            None => Err("internal: reference not built".into()),
        }
    }

    fn run_test(
        &self,
        t: &Test,
        built: &mut Built,
        reference: &mut Reference,
        protos: &[Proto],
    ) -> Item {
        let mut rng = Rng::new(gens::sub_seed(self.opts.seed, &t.name));
        let mut item = match &t.kind {
            Kind::Check { expr: e } => {
                let mut host = EvalHost {
                    vars: &[],
                    harness: built.harness.as_mut(),
                    program: built.program.as_ref(),
                };
                match expr::check(e, &mut host) {
                    Outcome::Pass => Item::pass(&t.name, ""),
                    Outcome::Fail(m) => Item::fail(&t.name, m),
                    Outcome::SpecError(m) => course_error(&t.name, m),
                }
            }
            Kind::Prog(case) => match &built.program {
                None => course_error(&t.name, "no program was built"),
                Some(p) => match p.run(&case.args, case.stdin.as_deref(), &case.files) {
                    Err(e) => course_error(&t.name, e),
                    Ok(r) => match program::judge(case, &r) {
                        None => Item::pass(&t.name, ""),
                        Some(m) => Item::fail(&t.name, m),
                    },
                },
            },
            Kind::Law {
                vars,
                checks,
                rounds,
                time_ms,
            } => self.run_law(t, vars, checks, *rounds, *time_ms, built, &mut rng),
            Kind::Fuzz {
                func,
                params,
                rounds,
            } => match self.reference(reference, protos) {
                Err(e) => course_error(&t.name, e),
                Ok(rb) => {
                    let Some(rh) = rb.harness.as_mut() else {
                        return course_error(&t.name, "reference has no harness");
                    };
                    let Some(sh) = built.harness.as_mut() else {
                        return course_error(&t.name, "no harness was built");
                    };
                    fuzz_function(&t.name, func, params, *rounds, sh, rh, &mut rng)
                }
            },
            Kind::FuzzProg {
                argv,
                stdin,
                rounds,
            } => match self.reference(reference, protos) {
                Err(e) => course_error(&t.name, e),
                Ok(rb) => match (built.program.as_ref(), rb.program.as_ref()) {
                    (Some(sp), Some(rp)) => fuzz_program(
                        &t.name,
                        argv.as_ref(),
                        stdin.as_ref(),
                        *rounds,
                        sp,
                        rp,
                        &mut rng,
                    ),
                    _ => course_error(&t.name, "no program was built"),
                },
            },
        };
        item.random = t.is_random();
        item
    }

    #[allow(clippy::too_many_arguments)]
    fn run_law(
        &self,
        t: &Test,
        vars: &[(String, Gen)],
        checks: &[(String, Expr)],
        rounds: u32,
        time_ms: Option<u64>,
        built: &mut Built,
        rng: &mut Rng,
    ) -> Item {
        // Returns the first failing check's message, a spec error, or None.
        let eval_round =
            |vals: &[(String, Value)], built: &mut Built| -> Result<Option<String>, String> {
                let started = Instant::now();
                for (src, e) in checks {
                    let mut host = EvalHost {
                        vars: vals,
                        harness: built.harness.as_mut(),
                        program: built.program.as_ref(),
                    };
                    match expr::check(e, &mut host) {
                        Outcome::Pass => {}
                        Outcome::Fail(m) => return Ok(Some(format!("{src}: {m}"))),
                        Outcome::SpecError(m) => return Err(format!("{src}: {m}")),
                    }
                }
                if let Some(limit) = time_ms {
                    let ms = started.elapsed().as_millis() as u64;
                    if ms > limit {
                        return Ok(Some(format!("took {ms} ms (limit {limit} ms)")));
                    }
                }
                Ok(None)
            };
        for round in 1..=rounds {
            let mut vals: Vec<(String, Value)> = vars
                .iter()
                .map(|(n, g)| (n.clone(), g.generate(rng)))
                .collect();
            match eval_round(&vals, built) {
                Ok(None) => continue,
                Err(m) => return course_error(&t.name, m),
                Ok(Some(_)) => {
                    if time_ms.is_none() {
                        shrink(vars, &mut vals, |trial| {
                            matches!(eval_round(trial, built), Ok(Some(_)))
                        });
                    }
                    let msg = match eval_round(&vals, built) {
                        Ok(Some(m)) => m,
                        _ => "failed".into(),
                    };
                    let with = if vals.is_empty() || time_ms.is_some() {
                        String::new()
                    } else {
                        format!(" with {}", describe_vars(&vals))
                    };
                    return Item::fail(&t.name, format!("{msg}{with} (round {round})"));
                }
            }
        }
        Item::pass(&t.name, format!("{rounds} rounds"))
    }
}

fn course_error(name: &str, msg: impl Into<String>) -> Item {
    Item {
        ok: false,
        name: name.to_string(),
        detail: format!("course error: {}", msg.into()),
        random: false,
        course_error: true,
    }
}

fn relativize(s: &str, base: &Path) -> String {
    let b = base.to_string_lossy();
    let mut out = s
        .replace(&format!("{b}\\"), "")
        .replace(&format!("{b}/"), "");
    out = out.replace(r"\\?\", "");
    out.replace('\\', "/")
}

fn phase(
    name: &'static str,
    items: Vec<Item>,
    streamed: bool,
    summary: impl FnOnce(usize, usize) -> String,
) -> Phase {
    let ok = items.iter().filter(|i| i.ok).count();
    let total = items.len();
    let status = if ok == total {
        Status::Pass
    } else {
        Status::Fail
    };
    let summary = if status == Status::Pass {
        summary(ok, total)
    } else if streamed {
        format!("{ok}/{total}")
    } else {
        let failing: Vec<&Item> = items.iter().filter(|i| !i.ok).collect();
        match failing.as_slice() {
            [one] if one.name == "compile" => one.detail.clone(),
            [one] => format!("{} {}", one.name, one.detail),
            many => format!("{} problems", many.len()),
        }
    };
    Phase {
        name,
        status,
        summary,
        items,
        streamed,
    }
}

fn call_result(h: &mut Harness, f: &str, args: &[Value]) -> Result<Value, String> {
    h.call(f, args).map_err(|e| e.message())
}

fn fuzz_function(
    name: &str,
    func: &str,
    params: &[(String, Gen)],
    rounds: u32,
    student: &mut Harness,
    reference: &mut Harness,
    rng: &mut Rng,
) -> Item {
    let mut skipped = 0;
    // Some(message) if the student disagrees with the reference.
    let differs = |vals: &[(String, Value)],
                   student: &mut Harness,
                   reference: &mut Harness|
     -> Option<Option<String>> {
        let args: Vec<Value> = vals.iter().map(|(_, v)| v.clone()).collect();
        let want = match call_result(reference, func, &args) {
            Ok(v) => v,
            // Outside the reference's domain: not a meaningful input.
            Err(_) => return None,
        };
        let call = format!("{func}({})", args_short(&args));
        Some(match call_result(student, func, &args) {
            Ok(got) if got == want => None,
            Ok(got) => Some(format!(
                "{call}: expected {}, got {}",
                want.short(100),
                got.short(100)
            )),
            Err(e) => Some(format!("{call} {e}")),
        })
    };
    for round in 1..=rounds {
        let mut vals: Vec<(String, Value)> = params
            .iter()
            .map(|(n, g)| (n.clone(), g.generate(rng)))
            .collect();
        match differs(&vals, student, reference) {
            None => skipped += 1,
            Some(None) => {}
            Some(Some(_)) => {
                shrink(params, &mut vals, |trial| {
                    matches!(differs(trial, student, reference), Some(Some(_)))
                });
                let msg = match differs(&vals, student, reference) {
                    Some(Some(m)) => m,
                    _ => "differs from the reference".into(),
                };
                return Item::fail(name, format!("{msg} (round {round})"));
            }
        }
    }
    if skipped == rounds {
        return course_error(
            name,
            "the reference rejected every generated input; check the generators",
        );
    }
    Item::pass(name, format!("{rounds} rounds vs reference"))
}

fn fuzz_program(
    name: &str,
    argv: Option<&Gen>,
    stdin: Option<&Gen>,
    rounds: u32,
    student: &Program,
    reference: &Program,
    rng: &mut Rng,
) -> Item {
    let to_args = |v: &Value| -> Vec<String> {
        match v {
            Value::List(l) => l
                .iter()
                .map(|x| x.text().unwrap_or_else(|| x.to_string()))
                .collect(),
            other => split_words(&other.text().unwrap_or_default()),
        }
    };
    let mut gens: Vec<(String, Gen)> = Vec::new();
    if let Some(g) = argv {
        gens.push(("argv".into(), g.clone()));
    }
    if let Some(g) = stdin {
        gens.push(("stdin".into(), g.clone()));
    }
    let differs = |vals: &[(String, Value)]| -> Result<Option<String>, String> {
        let args = vals
            .iter()
            .find(|(n, _)| n == "argv")
            .map(|(_, v)| to_args(v))
            .unwrap_or_default();
        let input = vals
            .iter()
            .find(|(n, _)| n == "stdin")
            .and_then(|(_, v)| v.text());
        let want = reference.run(&args, input.as_deref(), &[])?;
        let got = student.run(&args, input.as_deref(), &[])?;
        if got.timed_out {
            return Ok(Some(got.status));
        }
        if got.out != want.out {
            return Ok(Some(format!(
                "stdout: expected {}, got {}",
                Value::Str(want.out).short(80),
                Value::Str(got.out).short(80)
            )));
        }
        if got.exit != want.exit {
            return Ok(Some(format!(
                "exit code: expected {:?}, got {:?}",
                want.exit, got.exit
            )));
        }
        Ok(None)
    };
    for round in 1..=rounds {
        let mut vals: Vec<(String, Value)> = gens
            .iter()
            .map(|(n, g)| (n.clone(), g.generate(rng)))
            .collect();
        match differs(&vals) {
            Err(e) => return course_error(name, e),
            Ok(None) => {}
            Ok(Some(_)) => {
                shrink(&gens, &mut vals, |trial| {
                    matches!(differs(trial), Ok(Some(_)))
                });
                let msg = differs(&vals)
                    .ok()
                    .flatten()
                    .unwrap_or_else(|| "differs from the reference".into());
                return Item::fail(
                    name,
                    format!("{msg} with {} (round {round})", describe_vars(&vals)),
                );
            }
        }
    }
    Item::pass(name, format!("{rounds} rounds vs reference"))
}

/// Parses every unit's tests and signatures without running anything.
pub fn validate(c: &CourseDir) -> Vec<String> {
    let mut problems = Vec::new();
    for u in &c.units {
        let chain = c.chain(u);
        let lang = c.language(u);
        let protos = match (protos_for(c, &chain), helpers_for(c, &chain)) {
            (Ok(mut p), Ok(h)) => {
                p.extend(h);
                p
            }
            (Err(e), _) | (_, Err(e)) => {
                problems.push(e);
                continue;
            }
        };
        for s in c.support(u) {
            if !s.is_file() {
                problems.push(format!("{}: support file {} is missing", u.id, s.display()));
            }
        }
        if u.target == Target::Function && u.has_tests() {
            if !lang::has_harness(&lang) {
                problems.push(format!(
                    "{}: `target: function` is not available for {lang}; use `target: program`",
                    u.id
                ));
            }
            for p in &protos {
                if let Err(e) = lang::harness_supports(&lang, p) {
                    problems.push(format!("{}: {e}", u.id));
                }
            }
        }
        if u.target == Target::Program
            && c.run_cmd(u).is_none()
            && !matches!(lang.as_str(), "c" | "rust")
        {
            problems.push(format!(
                "{}: `target: program` needs `run:` for {lang}",
                u.id
            ));
        }
        if let Err(e) = tests_of(c, u, &protos) {
            problems.push(e);
        }
        let needs_ref = !u.random.fuzz.is_empty();
        if needs_ref && !c.ref_dir(u).is_dir() {
            problems.push(format!(
                "{}: fuzz needs a reference solution in .skillcheck/refs/{}/",
                u.id, u.id
            ));
        }
    }
    problems
}
