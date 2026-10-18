//! Seeded random input generators and shrinking.
//!
//! A generator is written as a short string (`string "ab," 0..30`,
//! `list(int 0..9) 0..10`, `lines(log_line) 0..50`), or as a template map
//! `{ fmt: "{h}:{m}", vars: { h: "int 0..23 w2", m: "int 0..59 w2" } }`.
//! Names not built in are looked up in the course's `lib.yaml`.

use crate::expr;
use crate::value::Value;
use std::collections::BTreeMap;
use yaml_serde::Value as Yaml;

pub struct Rng(u64);

impl Rng {
    pub fn new(seed: u64) -> Rng {
        Rng(seed)
    }
    pub fn next_u64(&mut self) -> u64 {
        self.0 = self.0.wrapping_add(0x9E37_79B9_7F4A_7C15);
        let mut z = self.0;
        z = (z ^ (z >> 30)).wrapping_mul(0xBF58_476D_1CE4_E5B9);
        z = (z ^ (z >> 27)).wrapping_mul(0x94D0_49BB_1331_11EB);
        z ^ (z >> 31)
    }
    pub fn below(&mut self, n: u64) -> u64 {
        if n == 0 { 0 } else { self.next_u64() % n }
    }
    pub fn range(&mut self, lo: i128, hi: i128) -> i128 {
        if hi <= lo {
            return lo;
        }
        let span = (hi - lo) as u128 + 1;
        lo + ((((self.next_u64() as u128) << 64) | self.next_u64() as u128) % span) as i128
    }
    pub fn chance(&mut self, p: f64) -> bool {
        self.unit() < p
    }
    pub fn unit(&mut self) -> f64 {
        (self.next_u64() >> 11) as f64 / (1u64 << 53) as f64
    }
}

/// Mixes a run seed with a test name so each test has its own stream.
pub fn sub_seed(seed: u64, name: &str) -> u64 {
    let mut h: u64 = 0xcbf2_9ce4_8422_2325;
    for b in name.bytes() {
        h ^= b as u64;
        h = h.wrapping_mul(0x0100_0000_01b3);
    }
    Rng::new(seed ^ h).next_u64()
}

#[derive(Clone, Debug)]
pub enum Gen {
    Int {
        lo: i128,
        hi: i128,
        width: usize,
    },
    Float {
        lo: f64,
        hi: f64,
    },
    Bool,
    Char {
        alphabet: Vec<char>,
    },
    Str {
        alphabet: Vec<char>,
        lo: usize,
        hi: usize,
    },
    List {
        inner: Box<Gen>,
        lo: usize,
        hi: usize,
    },
    Lines {
        inner: Box<Gen>,
        lo: usize,
        hi: usize,
    },
    Opt(Box<Gen>),
    Choice(Vec<Value>),
    Const(Value),
    Fmt(Vec<FmtPart>),
}

#[derive(Clone, Debug)]
pub enum FmtPart {
    Lit(String),
    Var(Gen),
}

pub const DEFAULT_ALPHABET: &str =
    "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789 ,.;:!?-_";

fn printable() -> Vec<char> {
    (0x20u8..0x7f).map(char::from).collect()
}

pub type Lib = BTreeMap<String, Yaml>;

pub fn parse(v: &Yaml, lib: &Lib) -> Result<Gen, String> {
    parse_depth(v, lib, 0)
}

fn parse_depth(v: &Yaml, lib: &Lib, depth: usize) -> Result<Gen, String> {
    if depth > 16 {
        return Err("generator definitions are nested too deeply (cycle in lib.yaml?)".into());
    }
    match v {
        Yaml::String(s) => parse_dsl(s, lib, depth),
        Yaml::Number(n) => Ok(Gen::Const(match n.as_i64() {
            Some(i) => Value::Int(i as i128),
            None => Value::Float(n.as_f64().unwrap_or_default()),
        })),
        Yaml::Bool(b) => Ok(Gen::Const(Value::Bool(*b))),
        Yaml::Sequence(items) => {
            let vals: Result<Vec<Value>, String> = items.iter().map(yaml_value).collect();
            Ok(Gen::Choice(vals?))
        }
        Yaml::Mapping(m) => {
            let get = |k: &str| m.get(Yaml::String(k.into()));
            let fmt = get("fmt")
                .and_then(Yaml::as_str)
                .ok_or("a generator map needs `fmt: \"...{var}...\"` and `vars:`")?;
            let mut vars: BTreeMap<String, Gen> = BTreeMap::new();
            if let Some(Yaml::Mapping(vm)) = get("vars") {
                for (k, g) in vm {
                    let k = k.as_str().ok_or("`vars` keys must be names")?;
                    vars.insert(k.to_string(), parse_depth(g, lib, depth + 1)?);
                }
            }
            parse_fmt(fmt, &vars).map(Gen::Fmt)
        }
        _ => Err("unsupported generator value".into()),
    }
}

fn yaml_value(v: &Yaml) -> Result<Value, String> {
    Ok(match v {
        Yaml::Null => Value::Null,
        Yaml::Bool(b) => Value::Bool(*b),
        Yaml::Number(n) => match n.as_i64() {
            Some(i) => Value::Int(i as i128),
            None => Value::Float(n.as_f64().unwrap_or_default()),
        },
        Yaml::String(s) => Value::Str(s.clone()),
        Yaml::Sequence(s) => Value::List(s.iter().map(yaml_value).collect::<Result<_, _>>()?),
        _ => return Err("unsupported literal in generator".into()),
    })
}

fn parse_fmt(fmt: &str, vars: &BTreeMap<String, Gen>) -> Result<Vec<FmtPart>, String> {
    let mut parts = Vec::new();
    let mut lit = String::new();
    let mut chars = fmt.chars().peekable();
    while let Some(c) = chars.next() {
        match c {
            '{' if chars.peek() == Some(&'{') => {
                chars.next();
                lit.push('{');
            }
            '}' if chars.peek() == Some(&'}') => {
                chars.next();
                lit.push('}');
            }
            '{' => {
                let name: String = chars.by_ref().take_while(|&c| c != '}').collect();
                let g = vars.get(name.trim()).ok_or_else(|| {
                    format!("fmt uses `{{{name}}}` but `vars` does not define it")
                })?;
                if !lit.is_empty() {
                    parts.push(FmtPart::Lit(std::mem::take(&mut lit)));
                }
                parts.push(FmtPart::Var(g.clone()));
            }
            c => lit.push(c),
        }
    }
    if !lit.is_empty() {
        parts.push(FmtPart::Lit(lit));
    }
    Ok(parts)
}

/// Splits `name(inner) rest` / `name rest`.
fn head(s: &str) -> Result<(String, Option<String>, String), String> {
    let s = s.trim();
    let name: String = s
        .chars()
        .take_while(|c| c.is_alphanumeric() || *c == '_')
        .collect();
    if name.is_empty() {
        return Err(format!("generator `{s}` must start with a name"));
    }
    let rest = &s[name.len()..];
    if let Some(r) = rest.strip_prefix('(') {
        let mut depth = 1;
        let mut in_str = false;
        let mut prev = ' ';
        for (i, c) in r.char_indices() {
            match c {
                '"' if prev != '\\' => in_str = !in_str,
                '(' if !in_str => depth += 1,
                ')' if !in_str => {
                    depth -= 1;
                    if depth == 0 {
                        return Ok((name, Some(r[..i].to_string()), r[i + 1..].to_string()));
                    }
                }
                _ => {}
            }
            prev = c;
        }
        return Err(format!("unbalanced parentheses in `{s}`"));
    }
    Ok((name, None, rest.to_string()))
}

#[derive(Default)]
struct Opts {
    text: Option<String>,
    range: Option<(f64, f64, bool)>,
    width: usize,
    list: Option<Value>,
}

fn options(rest: &str) -> Result<Opts, String> {
    let cs: Vec<char> = rest.chars().collect();
    let mut o = Opts::default();
    let mut i = 0;
    while i < cs.len() {
        let c = cs[i];
        if c.is_whitespace() {
            i += 1;
        } else if c == '"' {
            o.text = Some(expr::read_string(&cs, &mut i)?);
        } else if c == '[' {
            let start = i;
            let mut depth = 0;
            let mut in_str = false;
            while i < cs.len() {
                match cs[i] {
                    '"' if i == 0 || cs[i - 1] != '\\' => in_str = !in_str,
                    '[' if !in_str => depth += 1,
                    ']' if !in_str => {
                        depth -= 1;
                        if depth == 0 {
                            i += 1;
                            break;
                        }
                    }
                    _ => {}
                }
                i += 1;
            }
            let src: String = cs[start..i].iter().collect();
            o.list = Some(expr::constant(&src)?);
        } else if c == 'w' && cs.get(i + 1).is_some_and(char::is_ascii_digit) {
            let digits: String = cs[i + 1..]
                .iter()
                .take_while(|c| c.is_ascii_digit())
                .collect();
            o.width = digits.parse().unwrap_or(0);
            i += 1 + digits.len();
        } else {
            let word: String = cs[i..].iter().take_while(|c| !c.is_whitespace()).collect();
            i += word.chars().count();
            let (a, b) = word.split_once("..").ok_or_else(|| {
                format!("unexpected `{word}` (expected \"alphabet\", lo..hi, wN or [list])")
            })?;
            let num = |t: &str| t.parse::<f64>().map_err(|_| format!("bad range `{word}`"));
            let float = a.contains('.') || b.contains('.') || a.contains('e') || b.contains('e');
            o.range = Some((num(a)?, num(b)?, float));
        }
    }
    Ok(o)
}

fn parse_dsl(s: &str, lib: &Lib, depth: usize) -> Result<Gen, String> {
    let (name, inner, rest) = head(s)?;
    let inner_gen = |what: &str| -> Result<Box<Gen>, String> {
        let i = inner
            .as_deref()
            .ok_or_else(|| format!("`{what}` needs an inner generator: {what}(...)"))?;
        Ok(Box::new(parse_depth(
            &Yaml::String(i.to_string()),
            lib,
            depth + 1,
        )?))
    };
    if name == "lit" {
        return Ok(Gen::Const(expr::constant(rest.trim())?));
    }
    let o = options(&rest)?;
    let urange = |dlo: usize, dhi: usize| -> (usize, usize) {
        match o.range {
            Some((a, b, _)) => (a.max(0.0) as usize, b.max(a).max(0.0) as usize),
            None => (dlo, dhi),
        }
    };
    let alphabet = |default: Vec<char>| -> Vec<char> {
        match &o.text {
            Some(t) if !t.is_empty() => t.chars().collect(),
            _ => default,
        }
    };
    Ok(match name.as_str() {
        "int" | "uint" => {
            let (lo, hi) = match o.range {
                Some((a, b, _)) => (a as i128, b as i128),
                None if name == "uint" => (0, 1000),
                None => (-1000, 1000),
            };
            Gen::Int {
                lo,
                hi: hi.max(lo),
                width: o.width,
            }
        }
        "float" => {
            let (lo, hi) = o.range.map(|(a, b, _)| (a, b)).unwrap_or((-1000.0, 1000.0));
            Gen::Float { lo, hi: hi.max(lo) }
        }
        "bool" => Gen::Bool,
        "char" => Gen::Char {
            alphabet: alphabet(printable()),
        },
        "string" | "str" => {
            let (lo, hi) = urange(0, 20);
            Gen::Str {
                alphabet: alphabet(DEFAULT_ALPHABET.chars().collect()),
                lo,
                hi,
            }
        }
        "word" => {
            let (lo, hi) = urange(1, 8);
            Gen::Str {
                alphabet: alphabet("abcdefghijklmnopqrstuvwxyz".chars().collect()),
                lo: lo.max(1),
                hi: hi.max(1),
            }
        }
        "ascii" => {
            let (lo, hi) = urange(0, 20);
            Gen::Str {
                alphabet: printable(),
                lo,
                hi,
            }
        }
        "list" => {
            let (lo, hi) = urange(0, 10);
            Gen::List {
                inner: inner_gen("list")?,
                lo,
                hi,
            }
        }
        "lines" => {
            let (lo, hi) = urange(0, 20);
            Gen::Lines {
                inner: inner_gen("lines")?,
                lo,
                hi,
            }
        }
        "opt" => Gen::Opt(inner_gen("opt")?),
        "choice" => match o.list {
            Some(Value::List(items)) if !items.is_empty() => Gen::Choice(items),
            _ => return Err("choice needs a non-empty list: choice [\"a\", \"b\"]".into()),
        },
        other => match lib.get(other) {
            Some(def) => parse_depth(def, lib, depth + 1)?,
            None => return Err(format!("unknown generator `{other}`")),
        },
    })
}

impl Gen {
    pub fn generate(&self, rng: &mut Rng) -> Value {
        match self {
            Gen::Int { lo, hi, .. } => {
                if rng.chance(0.25) {
                    let mut edges = vec![*lo, *hi, (*lo + 1).min(*hi), (*hi - 1).max(*lo)];
                    if *lo <= 0 && 0 <= *hi {
                        edges.push(0);
                    }
                    Value::Int(edges[rng.below(edges.len() as u64) as usize])
                } else {
                    Value::Int(rng.range(*lo, *hi))
                }
            }
            Gen::Float { lo, hi } => {
                if rng.chance(0.15) {
                    let mut edges = vec![*lo, *hi];
                    if *lo <= 0.0 && 0.0 <= *hi {
                        edges.push(0.0);
                    }
                    Value::Float(edges[rng.below(edges.len() as u64) as usize])
                } else {
                    Value::Float(lo + (hi - lo) * rng.unit())
                }
            }
            Gen::Bool => Value::Bool(rng.chance(0.5)),
            Gen::Char { alphabet } => {
                Value::Char(alphabet[rng.below(alphabet.len() as u64) as usize])
            }
            Gen::Str { alphabet, lo, hi } => {
                let n = sized(rng, *lo, *hi);
                // Sometimes draw from two letters only, so separators and
                // repeats (",," / "aa") show up often.
                let pool: Vec<char> = if alphabet.len() > 2 && rng.chance(0.2) {
                    (0..2)
                        .map(|_| alphabet[rng.below(alphabet.len() as u64) as usize])
                        .collect()
                } else {
                    alphabet.clone()
                };
                Value::Str(
                    (0..n)
                        .map(|_| pool[rng.below(pool.len() as u64) as usize])
                        .collect(),
                )
            }
            Gen::List { inner, lo, hi } => {
                let n = sized(rng, *lo, *hi);
                Value::List((0..n).map(|_| inner.generate(rng)).collect())
            }
            Gen::Lines { inner, lo, hi } => {
                let n = sized(rng, *lo, *hi);
                let mut s = String::new();
                for _ in 0..n {
                    s.push_str(&render_text(&inner.generate(rng), inner));
                    s.push('\n');
                }
                Value::Str(s)
            }
            Gen::Opt(inner) => {
                if rng.chance(0.15) {
                    Value::Null
                } else {
                    inner.generate(rng)
                }
            }
            Gen::Choice(items) => items[rng.below(items.len() as u64) as usize].clone(),
            Gen::Const(v) => v.clone(),
            Gen::Fmt(parts) => {
                let mut s = String::new();
                for p in parts {
                    match p {
                        FmtPart::Lit(l) => s.push_str(l),
                        FmtPart::Var(g) => s.push_str(&render_text(&g.generate(rng), g)),
                    }
                }
                Value::Str(s)
            }
        }
    }

    /// Smaller variants of `v` that this generator could also have produced.
    pub fn shrink(&self, v: &Value) -> Vec<Value> {
        let mut out = Vec::new();
        match (self, v) {
            (Gen::Int { lo, hi, .. }, Value::Int(i)) => {
                let target = if *lo <= 0 && 0 <= *hi { 0 } else { *lo };
                for c in [target, target + (i - target) / 2, i - (i - target).signum()] {
                    if c != *i && (*lo..=*hi).contains(&c) && !out.contains(&Value::Int(c)) {
                        out.push(Value::Int(c));
                    }
                }
            }
            (Gen::Str { alphabet, lo, .. }, Value::Str(s)) => {
                let cs: Vec<char> = s.chars().collect();
                for part in shrink_seq(&cs, *lo) {
                    out.push(Value::Str(part.into_iter().collect()));
                }
                if let Some(&first) = alphabet.first() {
                    for i in (0..cs.len()).take(12) {
                        if cs[i] != first {
                            let mut t = cs.clone();
                            t[i] = first;
                            out.push(Value::Str(t.into_iter().collect()));
                        }
                    }
                }
            }
            (Gen::List { inner, lo, .. }, Value::List(items)) => {
                for part in shrink_seq(items, *lo) {
                    out.push(Value::List(part));
                }
                for i in (0..items.len()).take(8) {
                    for smaller in inner.shrink(&items[i]).into_iter().take(3) {
                        let mut t = items.clone();
                        t[i] = smaller;
                        out.push(Value::List(t));
                    }
                }
            }
            (Gen::Lines { lo, .. }, Value::Str(s)) => {
                let lines: Vec<&str> = s.lines().collect();
                for part in shrink_seq(&lines, *lo) {
                    let mut t: String = part.iter().map(|l| format!("{l}\n")).collect();
                    if part.is_empty() {
                        t.clear();
                    }
                    out.push(Value::Str(t));
                }
            }
            (Gen::Opt(inner), v) if !matches!(v, Value::Null) => {
                out.push(Value::Null);
                out.extend(inner.shrink(v));
            }
            _ => {}
        }
        out
    }
}

fn sized(rng: &mut Rng, lo: usize, hi: usize) -> usize {
    if hi <= lo {
        return lo;
    }
    match rng.below(10) {
        0 => lo,
        1 => hi,
        // Bias towards short inputs: easier to read when they fail.
        2..=6 => rng.range(lo as i128, (lo + (hi - lo) / 3).max(lo) as i128) as usize,
        _ => rng.range(lo as i128, hi as i128) as usize,
    }
}

fn shrink_seq<T: Clone>(items: &[T], min: usize) -> Vec<Vec<T>> {
    let n = items.len();
    let mut out = Vec::new();
    if n <= min {
        return out;
    }
    if min == 0 {
        out.push(Vec::new());
    }
    let half = n / 2;
    if half >= min && half > 0 {
        out.push(items[..half].to_vec());
        out.push(items[n - half..].to_vec());
    }
    for i in (0..n).take(16) {
        let mut t = items.to_vec();
        t.remove(i);
        out.push(t);
    }
    out
}

fn render_text(v: &Value, g: &Gen) -> String {
    match (v, g) {
        (Value::Int(i), Gen::Int { width, .. }) if *width > 0 => {
            let neg = *i < 0;
            let digits = format!("{:0w$}", i.abs(), w = *width);
            if neg { format!("-{digits}") } else { digits }
        }
        _ => v.text().unwrap_or_else(|| v.to_string()),
    }
}

/// Human description used by `proctor explain`.
pub fn describe(g: &Gen) -> String {
    match g {
        Gen::Int { lo, hi, width } => {
            if *width > 0 {
                format!("int {lo}..{hi} (width {width})")
            } else {
                format!("int {lo}..{hi}")
            }
        }
        Gen::Float { lo, hi } => format!("float {lo}..{hi}"),
        Gen::Bool => "bool".into(),
        Gen::Char { alphabet } => format!("char from {:?}", alphabet.iter().collect::<String>()),
        Gen::Str { alphabet, lo, hi } => {
            let a: String = alphabet.iter().collect();
            let a = if a.chars().count() > 40 {
                format!("{}...", a.chars().take(40).collect::<String>())
            } else {
                a
            };
            format!("string {lo}..{hi} over {a:?}")
        }
        Gen::List { inner, lo, hi } => format!("list {lo}..{hi} of ({})", describe(inner)),
        Gen::Lines { inner, lo, hi } => format!("{lo}..{hi} lines of ({})", describe(inner)),
        Gen::Opt(inner) => format!("({}) or null", describe(inner)),
        Gen::Choice(items) => format!("one of {}", Value::List(items.clone()).short(60)),
        Gen::Const(v) => format!("always {}", v.short(40)),
        Gen::Fmt(parts) => {
            let mut s = String::from("text \"");
            for p in parts {
                match p {
                    FmtPart::Lit(l) => s.push_str(l),
                    FmtPart::Var(g) => s.push_str(&format!("{{{}}}", describe(g))),
                }
            }
            s.push('"');
            s
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn g(s: &str) -> Gen {
        parse(&Yaml::String(s.into()), &Lib::new()).unwrap()
    }

    #[test]
    fn dsl_parses_and_respects_bounds() {
        let mut rng = Rng::new(7);
        let s = g(r#"string "ab," 2..5"#);
        for _ in 0..200 {
            let Value::Str(v) = s.generate(&mut rng) else {
                panic!()
            };
            assert!((2..=5).contains(&v.chars().count()), "{v:?}");
            assert!(v.chars().all(|c| "ab,".contains(c)));
        }
        let l = g("list(int 0..9) 0..3");
        for _ in 0..200 {
            let Value::List(v) = l.generate(&mut rng) else {
                panic!()
            };
            assert!(v.len() <= 3);
            assert!(v.iter().all(|x| matches!(x, Value::Int(0..=9))));
        }
        assert!(matches!(g(r#"choice ["INFO", "WARN"]"#), Gen::Choice(ref v) if v.len() == 2));
        assert!(matches!(g("lit 42"), Gen::Const(Value::Int(42))));
        assert!(parse(&Yaml::String("nope".into()), &Lib::new()).is_err());
    }

    #[test]
    fn templates_and_lib_refs() {
        let lib: Lib = yaml_serde::from_str(
            "hhmm: { fmt: \"{h}:{m}\", vars: { h: int 0..23 w2, m: int 0..59 w2 } }\n",
        )
        .unwrap();
        let lines = parse(&Yaml::String("lines(hhmm) 3..3".into()), &lib).unwrap();
        let Value::Str(s) = lines.generate(&mut Rng::new(1)) else {
            panic!()
        };
        assert_eq!(s.lines().count(), 3);
        assert!(s.lines().all(|l| l.len() == 5 && &l[2..3] == ":"), "{s:?}");
    }

    #[test]
    fn shrinking_moves_towards_small_values() {
        let s = g("string 0..10");
        let c = s.shrink(&Value::Str("hello".into()));
        assert_eq!(c[0], Value::Str(String::new()));
        let i = g("int 5..100");
        assert_eq!(i.shrink(&Value::Int(50))[0], Value::Int(5));
    }
}
