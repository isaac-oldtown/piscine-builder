//! The small expression language used by function cases and laws:
//!   str_len("abc") == 3
//!   join(nonempty(split(s, ",")), ",") == join(str_split(s, ','), ",")
//!   raises(parse(""))
//! Calls resolve to the student's functions (through the harness), to
//! `run(...)` for programs, or to built-ins. There are no user-defined
//! functions or lambdas, so a spec cannot smuggle in a reference solution.

use crate::value::Value;
use regex::Regex;

#[derive(Clone, Debug, PartialEq)]
enum Tok {
    Int(i128),
    Float(f64),
    Str(String),
    Char(char),
    Ident(String),
    P(&'static str),
}

const PUNCT2: &[&str] = &["==", "!=", "<=", ">=", "&&", "||"];
const PUNCT1: &[&str] = &[
    "(", ")", "[", "]", "{", "}", ",", ":", "=", "<", ">", "!", "+", "-", "*", "/", "%", ".",
];

fn read_escape(cs: &[char], i: &mut usize) -> Result<char, String> {
    let c = *cs.get(*i).ok_or("unterminated escape")?;
    *i += 1;
    Ok(match c {
        'n' => '\n',
        't' => '\t',
        'r' => '\r',
        '0' => '\0',
        '\\' => '\\',
        '"' => '"',
        '\'' => '\'',
        'x' => {
            let h: String = cs.get(*i..*i + 2).ok_or("bad \\x escape")?.iter().collect();
            *i += 2;
            char::from(u8::from_str_radix(&h, 16).map_err(|_| "bad \\x escape")?)
        }
        'u' => {
            if cs.get(*i) != Some(&'{') {
                return Err("expected \\u{...}".into());
            }
            let end = cs[*i..]
                .iter()
                .position(|&c| c == '}')
                .ok_or("bad \\u escape")?;
            let h: String = cs[*i + 1..*i + end].iter().collect();
            *i += end + 1;
            u32::from_str_radix(&h, 16)
                .ok()
                .and_then(char::from_u32)
                .ok_or("bad \\u escape")?
        }
        other => return Err(format!("unknown escape \\{other}")),
    })
}

/// Reads a "double-quoted" string starting at `cs[*i] == '"'`.
pub fn read_string(cs: &[char], i: &mut usize) -> Result<String, String> {
    *i += 1;
    let mut s = String::new();
    loop {
        match cs.get(*i) {
            None => return Err("unterminated string".into()),
            Some('"') => {
                *i += 1;
                return Ok(s);
            }
            Some('\\') => {
                *i += 1;
                s.push(read_escape(cs, i)?);
            }
            Some(&c) => {
                s.push(c);
                *i += 1;
            }
        }
    }
}

fn lex(src: &str) -> Result<Vec<Tok>, String> {
    let cs: Vec<char> = src.chars().collect();
    let mut i = 0;
    let mut out = Vec::new();
    while i < cs.len() {
        let c = cs[i];
        if c.is_whitespace() {
            i += 1;
        } else if c.is_ascii_digit() {
            let start = i;
            while i < cs.len() && (cs[i].is_ascii_digit() || cs[i] == '_') {
                i += 1;
            }
            let mut float = false;
            if i + 1 < cs.len() && cs[i] == '.' && cs[i + 1].is_ascii_digit() {
                float = true;
                i += 1;
                while i < cs.len() && cs[i].is_ascii_digit() {
                    i += 1;
                }
            }
            if i < cs.len() && (cs[i] == 'e' || cs[i] == 'E') {
                let mut j = i + 1;
                if j < cs.len() && (cs[j] == '+' || cs[j] == '-') {
                    j += 1;
                }
                if j < cs.len() && cs[j].is_ascii_digit() {
                    float = true;
                    i = j;
                    while i < cs.len() && cs[i].is_ascii_digit() {
                        i += 1;
                    }
                }
            }
            let text: String = cs[start..i].iter().filter(|&&c| c != '_').collect();
            out.push(if float {
                Tok::Float(text.parse().map_err(|_| format!("bad number `{text}`"))?)
            } else {
                Tok::Int(text.parse().map_err(|_| format!("bad number `{text}`"))?)
            });
        } else if c == '"' {
            out.push(Tok::Str(read_string(&cs, &mut i)?));
        } else if c == '\'' {
            i += 1;
            let ch = match cs.get(i) {
                Some('\\') => {
                    i += 1;
                    read_escape(&cs, &mut i)?
                }
                Some(&ch) => {
                    i += 1;
                    ch
                }
                None => return Err("unterminated char".into()),
            };
            if cs.get(i) != Some(&'\'') {
                return Err("char literal must hold exactly one character".into());
            }
            i += 1;
            out.push(Tok::Char(ch));
        } else if c.is_alphabetic() || c == '_' {
            let start = i;
            while i < cs.len() {
                let d = cs[i];
                let dotted = d == '.'
                    && cs
                        .get(i + 1)
                        .is_some_and(|n| n.is_alphabetic() || *n == '_');
                if d.is_alphanumeric() || d == '_' || dotted {
                    i += 1;
                } else {
                    break;
                }
            }
            out.push(Tok::Ident(cs[start..i].iter().collect()));
        } else {
            let two: String = cs[i..(i + 2).min(cs.len())].iter().collect();
            if let Some(p) = PUNCT2.iter().find(|p| **p == two) {
                out.push(Tok::P(p));
                i += 2;
            } else if let Some(p) = PUNCT1.iter().find(|p| p.starts_with(c)) {
                out.push(Tok::P(p));
                i += 1;
            } else {
                return Err(format!("unexpected character `{c}`"));
            }
        }
    }
    Ok(out)
}

#[derive(Clone, Copy, Debug, PartialEq)]
pub enum BinOp {
    Or,
    And,
    Eq,
    Ne,
    Lt,
    Le,
    Gt,
    Ge,
    Add,
    Sub,
    Mul,
    Div,
    Rem,
}

#[derive(Clone, Debug)]
pub enum Expr {
    Lit(Value),
    Name(String),
    Call(String, Vec<(Option<String>, Expr)>),
    List(Vec<Expr>),
    Map(Vec<(Expr, Expr)>),
    Not(Box<Expr>),
    Neg(Box<Expr>),
    Bin(BinOp, Box<Expr>, Box<Expr>),
    Field(Box<Expr>, String),
    Index(Box<Expr>, Box<Expr>),
}

struct Parser {
    toks: Vec<Tok>,
    pos: usize,
}

impl Parser {
    fn peek(&self) -> Option<&Tok> {
        self.toks.get(self.pos)
    }
    fn is(&self, p: &str) -> bool {
        matches!(self.peek(), Some(Tok::P(q)) if *q == p)
    }
    fn eat(&mut self, p: &str) -> bool {
        if self.is(p) {
            self.pos += 1;
            true
        } else {
            false
        }
    }
    fn expect(&mut self, p: &str) -> Result<(), String> {
        if self.eat(p) {
            Ok(())
        } else {
            Err(format!("expected `{p}`"))
        }
    }

    fn expr(&mut self) -> Result<Expr, String> {
        self.binary(0)
    }

    fn binary(&mut self, level: usize) -> Result<Expr, String> {
        const LEVELS: &[&[(&str, BinOp)]] = &[
            &[("||", BinOp::Or)],
            &[("&&", BinOp::And)],
            &[
                ("==", BinOp::Eq),
                ("!=", BinOp::Ne),
                ("<=", BinOp::Le),
                (">=", BinOp::Ge),
                ("<", BinOp::Lt),
                (">", BinOp::Gt),
            ],
            &[("+", BinOp::Add), ("-", BinOp::Sub)],
            &[("*", BinOp::Mul), ("/", BinOp::Div), ("%", BinOp::Rem)],
        ];
        if level == LEVELS.len() {
            return self.unary();
        }
        let mut left = self.binary(level + 1)?;
        loop {
            let Some(&(_, op)) = LEVELS[level].iter().find(|(p, _)| self.is(p)) else {
                return Ok(left);
            };
            self.pos += 1;
            let right = self.binary(level + 1)?;
            left = Expr::Bin(op, Box::new(left), Box::new(right));
            // Comparisons do not chain.
            if level == 2 {
                return Ok(left);
            }
        }
    }

    fn unary(&mut self) -> Result<Expr, String> {
        if self.eat("!") {
            return Ok(Expr::Not(Box::new(self.unary()?)));
        }
        if self.eat("-") {
            return Ok(Expr::Neg(Box::new(self.unary()?)));
        }
        self.postfix()
    }

    fn postfix(&mut self) -> Result<Expr, String> {
        let mut e = self.primary()?;
        loop {
            if self.eat(".") {
                match self.peek().cloned() {
                    Some(Tok::Ident(f)) => {
                        self.pos += 1;
                        e = Expr::Field(Box::new(e), f);
                    }
                    _ => return Err("expected a field name after `.`".into()),
                }
            } else if self.eat("[") {
                let i = self.expr()?;
                self.expect("]")?;
                e = Expr::Index(Box::new(e), Box::new(i));
            } else {
                return Ok(e);
            }
        }
    }

    fn primary(&mut self) -> Result<Expr, String> {
        let t = self.peek().cloned().ok_or("unexpected end of expression")?;
        self.pos += 1;
        Ok(match t {
            Tok::Int(i) => Expr::Lit(Value::Int(i)),
            Tok::Float(f) => Expr::Lit(Value::Float(f)),
            Tok::Str(s) => Expr::Lit(Value::Str(s)),
            Tok::Char(c) => Expr::Lit(Value::Char(c)),
            Tok::Ident(name) => match name.as_str() {
                "true" => Expr::Lit(Value::Bool(true)),
                "false" => Expr::Lit(Value::Bool(false)),
                "null" | "None" | "NULL" => Expr::Lit(Value::Null),
                _ if self.eat("(") => {
                    let mut args = Vec::new();
                    while !self.eat(")") {
                        let named = match (self.peek(), self.toks.get(self.pos + 1)) {
                            (Some(Tok::Ident(n)), Some(Tok::P("="))) => Some(n.clone()),
                            _ => None,
                        };
                        if named.is_some() {
                            self.pos += 2;
                        }
                        args.push((named, self.expr()?));
                        if !self.eat(",") {
                            self.expect(")")?;
                            break;
                        }
                    }
                    Expr::Call(name, args)
                }
                _ => Expr::Name(name),
            },
            Tok::P("(") => {
                let e = self.expr()?;
                self.expect(")")?;
                e
            }
            Tok::P("[") => {
                let mut items = Vec::new();
                while !self.eat("]") {
                    items.push(self.expr()?);
                    if !self.eat(",") {
                        self.expect("]")?;
                        break;
                    }
                }
                Expr::List(items)
            }
            Tok::P("{") => {
                let mut items = Vec::new();
                while !self.eat("}") {
                    let k = self.expr()?;
                    self.expect(":")?;
                    items.push((k, self.expr()?));
                    if !self.eat(",") {
                        self.expect("}")?;
                        break;
                    }
                }
                Expr::Map(items)
            }
            Tok::P(p) => return Err(format!("unexpected `{p}`")),
        })
    }
}

pub fn parse(src: &str) -> Result<Expr, String> {
    let mut p = Parser {
        toks: lex(src)?,
        pos: 0,
    };
    let e = p.expr()?;
    if p.pos != p.toks.len() {
        return Err("unexpected text after the expression".into());
    }
    Ok(e)
}

#[derive(Clone, Debug)]
pub enum EvalError {
    /// The student's code failed (raised, crashed, timed out).
    Student(String),
    /// The spec itself is wrong (unknown name, type error in a built-in).
    Spec(String),
}

pub type Args = Vec<(Option<String>, Value)>;

pub trait Host {
    fn var(&self, name: &str) -> Option<Value>;
    /// Student functions and `run`. `None` means "not mine, try built-ins".
    fn call(&mut self, name: &str, args: Args) -> Option<Result<Value, EvalError>>;
    fn is_external(&self, name: &str) -> bool;
}

fn spec<T>(msg: impl Into<String>) -> Result<T, EvalError> {
    Err(EvalError::Spec(msg.into()))
}

fn field(v: &Value, name: &str) -> Result<Value, EvalError> {
    match v {
        Value::Map(m) => Ok(m
            .iter()
            .find(|(k, _)| k.text().as_deref() == Some(name))
            .map(|(_, v)| v.clone())
            .unwrap_or(Value::Null)),
        other => spec(format!("`.{name}` on a {}", other.type_name())),
    }
}

pub fn eval(e: &Expr, h: &mut dyn Host) -> Result<Value, EvalError> {
    match e {
        Expr::Lit(v) => Ok(v.clone()),
        Expr::Name(n) => {
            if let Some(v) = h.var(n) {
                return Ok(v);
            }
            let mut parts = n.split('.');
            let base = parts.next().unwrap_or_default();
            let Some(mut v) = h.var(base) else {
                return spec(format!("unknown name `{n}`"));
            };
            for p in parts {
                v = field(&v, p)?;
            }
            Ok(v)
        }
        Expr::List(items) => Ok(Value::List(
            items.iter().map(|x| eval(x, h)).collect::<Result<_, _>>()?,
        )),
        Expr::Map(items) => {
            let mut m = Vec::new();
            for (k, v) in items {
                m.push((eval(k, h)?, eval(v, h)?));
            }
            Ok(Value::Map(m))
        }
        Expr::Not(x) => Ok(Value::Bool(!eval(x, h)?.truthy())),
        Expr::Neg(x) => match eval(x, h)? {
            Value::Int(i) => Ok(Value::Int(-i)),
            Value::Float(f) => Ok(Value::Float(-f)),
            v => spec(format!("cannot negate a {}", v.type_name())),
        },
        Expr::Field(x, f) => field(&eval(x, h)?, f),
        Expr::Index(x, i) => index(&eval(x, h)?, &eval(i, h)?),
        Expr::Bin(BinOp::And, a, b) => {
            Ok(Value::Bool(eval(a, h)?.truthy() && eval(b, h)?.truthy()))
        }
        Expr::Bin(BinOp::Or, a, b) => Ok(Value::Bool(eval(a, h)?.truthy() || eval(b, h)?.truthy())),
        Expr::Bin(op, a, b) => {
            let (a, b) = (eval(a, h)?, eval(b, h)?);
            binop(*op, &a, &b)
        }
        Expr::Call(name, args) => {
            if name == "raises" {
                let [(_, inner)] = args.as_slice() else {
                    return spec("raises(...) takes exactly one expression");
                };
                return match eval(inner, h) {
                    Ok(_) => Ok(Value::Bool(false)),
                    Err(EvalError::Student(_)) => Ok(Value::Bool(true)),
                    Err(e) => Err(e),
                };
            }
            let mut vals = Vec::with_capacity(args.len());
            for (n, a) in args {
                vals.push((n.clone(), eval(a, h)?));
            }
            if let Some(r) = h.call(name, vals.clone()) {
                return r;
            }
            if vals.iter().any(|(n, _)| n.is_some()) {
                return spec(format!("`{name}` takes no named arguments"));
            }
            builtin(name, vals.into_iter().map(|(_, v)| v).collect())
        }
    }
}

fn index(v: &Value, i: &Value) -> Result<Value, EvalError> {
    let pos = |len: usize| -> Result<usize, EvalError> {
        let Value::Int(i) = i else {
            return spec("index must be an int");
        };
        let idx = if *i < 0 { len as i128 + i } else { *i };
        if idx < 0 || idx >= len as i128 {
            return spec(format!("index {i} out of range (len {len})"));
        }
        Ok(idx as usize)
    };
    match v {
        Value::List(l) => Ok(l[pos(l.len())?].clone()),
        Value::Str(s) => {
            let cs: Vec<char> = s.chars().collect();
            Ok(Value::Char(cs[pos(cs.len())?]))
        }
        Value::Map(_) => {
            let key = i.text().unwrap_or_else(|| i.to_string());
            field(v, &key)
        }
        other => spec(format!("cannot index a {}", other.type_name())),
    }
}

fn binop(op: BinOp, a: &Value, b: &Value) -> Result<Value, EvalError> {
    use std::cmp::Ordering::*;
    Ok(match op {
        BinOp::Eq => Value::Bool(a == b),
        BinOp::Ne => Value::Bool(a != b),
        BinOp::Lt => Value::Bool(a.cmp_values(b) == Less),
        BinOp::Le => Value::Bool(a.cmp_values(b) != Greater),
        BinOp::Gt => Value::Bool(a.cmp_values(b) == Greater),
        BinOp::Ge => Value::Bool(a.cmp_values(b) != Less),
        BinOp::Add => match (a, b) {
            (Value::Int(x), Value::Int(y)) => Value::Int(
                x.checked_add(*y)
                    .ok_or_else(|| EvalError::Spec("overflow".into()))?,
            ),
            (Value::List(x), Value::List(y)) => Value::List(x.iter().chain(y).cloned().collect()),
            _ if a.text().is_some() && b.text().is_some() => {
                Value::Str(a.text().unwrap_or_default() + &b.text().unwrap_or_default())
            }
            _ => num(op, a, b)?,
        },
        BinOp::Mul => match (a, b) {
            (Value::Str(s), Value::Int(n)) | (Value::Int(n), Value::Str(s)) => {
                Value::Str(s.repeat((*n).max(0) as usize))
            }
            _ => num(op, a, b)?,
        },
        BinOp::And | BinOp::Or => unreachable!("short-circuited in eval"),
        _ => num(op, a, b)?,
    })
}

fn num(op: BinOp, a: &Value, b: &Value) -> Result<Value, EvalError> {
    if let (Value::Int(x), Value::Int(y)) = (a, b) {
        let r = match op {
            BinOp::Add => x.checked_add(*y),
            BinOp::Sub => x.checked_sub(*y),
            BinOp::Mul => x.checked_mul(*y),
            BinOp::Div => x.checked_div(*y),
            BinOp::Rem => x.checked_rem(*y),
            _ => None,
        };
        return r
            .map(Value::Int)
            .ok_or_else(|| EvalError::Spec("integer overflow or division by zero".into()));
    }
    let (Some(x), Some(y)) = (a.as_f64(), b.as_f64()) else {
        return spec(format!(
            "arithmetic on {} and {}",
            a.type_name(),
            b.type_name()
        ));
    };
    Ok(Value::Float(match op {
        BinOp::Add => x + y,
        BinOp::Sub => x - y,
        BinOp::Mul => x * y,
        BinOp::Div => x / y,
        BinOp::Rem => x % y,
        _ => unreachable!(),
    }))
}

pub const BUILTINS: &[&str] = &[
    "len",
    "bytes",
    "sorted",
    "reversed",
    "sum",
    "min",
    "max",
    "abs",
    "count",
    "contains",
    "join",
    "split",
    "words",
    "lines",
    "lower",
    "upper",
    "trim",
    "chars",
    "unique",
    "keys",
    "values",
    "get",
    "all",
    "any",
    "int",
    "float",
    "str",
    "is_sorted",
    "permutation",
    "repeat",
    "range",
    "slice",
    "starts_with",
    "ends_with",
    "matches",
    "replace",
    "nonempty",
    "concat",
    "flatten",
    "approx",
    "raises",
    "then",
];

fn list(v: &Value, f: &str) -> Result<Vec<Value>, EvalError> {
    match v {
        Value::List(l) => Ok(l.clone()),
        Value::Str(s) => Ok(s.chars().map(Value::Char).collect()),
        Value::Null => Ok(Vec::new()),
        other => spec(format!("{f}: expected a list, got {}", other.type_name())),
    }
}

fn text(v: &Value, f: &str) -> Result<String, EvalError> {
    v.text()
        .ok_or_else(|| EvalError::Spec(format!("{f}: expected a string, got {}", v.type_name())))
}

fn int(v: &Value, f: &str) -> Result<i128, EvalError> {
    match v {
        Value::Int(i) => Ok(*i),
        other => spec(format!("{f}: expected an int, got {}", other.type_name())),
    }
}

fn builtin(name: &str, a: Vec<Value>) -> Result<Value, EvalError> {
    let n = a.len();
    let arity = |k: usize| -> Result<(), EvalError> {
        if n == k {
            Ok(())
        } else {
            spec(format!("{name} takes {k} argument(s), got {n}"))
        }
    };
    let strs = |l: Vec<String>| Value::List(l.into_iter().map(Value::Str).collect());
    Ok(match name {
        "len" => {
            arity(1)?;
            Value::Int(match &a[0] {
                Value::Str(s) | Value::Opaque(s) => s.chars().count(),
                Value::List(l) => l.len(),
                Value::Map(m) => m.len(),
                Value::Char(_) => 1,
                Value::Null => 0,
                v => return spec(format!("len of a {}", v.type_name())),
            } as i128)
        }
        "bytes" => {
            arity(1)?;
            Value::Int(text(&a[0], name)?.len() as i128)
        }
        "sorted" | "reversed" | "unique" | "nonempty" | "flatten" => {
            arity(1)?;
            let mut l = list(&a[0], name)?;
            match name {
                "sorted" => l.sort_by(|x, y| x.cmp_values(y)),
                "reversed" => l.reverse(),
                "unique" => {
                    let mut seen: Vec<Value> = Vec::new();
                    l.retain(|x| {
                        if seen.contains(x) {
                            false
                        } else {
                            seen.push(x.clone());
                            true
                        }
                    });
                }
                "nonempty" => l.retain(|x| x.truthy()),
                _ => {
                    l = l
                        .into_iter()
                        .flat_map(|x| match x {
                            Value::List(inner) => inner,
                            other => vec![other],
                        })
                        .collect()
                }
            }
            if matches!(a[0], Value::Str(_)) && name != "flatten" {
                Value::Str(l.iter().filter_map(Value::text).collect())
            } else {
                Value::List(l)
            }
        }
        "sum" => {
            arity(1)?;
            let mut acc = Value::Int(0);
            for x in list(&a[0], name)? {
                acc = num(BinOp::Add, &acc, &x)?;
            }
            acc
        }
        "min" | "max" => {
            let items = if n == 1 { list(&a[0], name)? } else { a };
            let pick = items.into_iter().reduce(|x, y| {
                let less = x.cmp_values(&y) == std::cmp::Ordering::Greater;
                if (name == "min") == less { y } else { x }
            });
            pick.unwrap_or(Value::Null)
        }
        "abs" => {
            arity(1)?;
            match &a[0] {
                Value::Int(i) => Value::Int(i.abs()),
                Value::Float(f) => Value::Float(f.abs()),
                v => return spec(format!("abs of a {}", v.type_name())),
            }
        }
        "count" => {
            arity(2)?;
            match &a[0] {
                Value::Str(s) => {
                    let pat = text(&a[1], name)?;
                    if pat.is_empty() {
                        return spec("count: empty pattern");
                    }
                    Value::Int(s.matches(pat.as_str()).count() as i128)
                }
                v => Value::Int(list(v, name)?.iter().filter(|x| **x == a[1]).count() as i128),
            }
        }
        "contains" => {
            arity(2)?;
            Value::Bool(match &a[0] {
                Value::Str(s) => s.contains(text(&a[1], name)?.as_str()),
                Value::Map(m) => m.iter().any(|(k, _)| *k == a[1]),
                v => list(v, name)?.contains(&a[1]),
            })
        }
        "join" => {
            arity(2)?;
            let sep = text(&a[1], name)?;
            let parts: Result<Vec<String>, _> =
                list(&a[0], name)?.iter().map(|x| text(x, name)).collect();
            Value::Str(parts?.join(&sep))
        }
        "split" => {
            arity(2)?;
            let s = text(&a[0], name)?;
            let sep = text(&a[1], name)?;
            if sep.is_empty() {
                return spec("split: empty separator");
            }
            strs(s.split(sep.as_str()).map(str::to_string).collect())
        }
        "words" => {
            arity(1)?;
            strs(
                text(&a[0], name)?
                    .split_whitespace()
                    .map(str::to_string)
                    .collect(),
            )
        }
        "lines" => {
            arity(1)?;
            strs(text(&a[0], name)?.lines().map(str::to_string).collect())
        }
        "lower" => {
            arity(1)?;
            Value::Str(text(&a[0], name)?.to_lowercase())
        }
        "upper" => {
            arity(1)?;
            Value::Str(text(&a[0], name)?.to_uppercase())
        }
        "trim" => {
            arity(1)?;
            Value::Str(text(&a[0], name)?.trim().to_string())
        }
        "chars" => {
            arity(1)?;
            Value::List(text(&a[0], name)?.chars().map(Value::Char).collect())
        }
        "keys" | "values" => {
            arity(1)?;
            match &a[0] {
                Value::Map(m) => Value::List(
                    m.iter()
                        .map(|(k, v)| if name == "keys" { k.clone() } else { v.clone() })
                        .collect(),
                ),
                v => return spec(format!("{name} of a {}", v.type_name())),
            }
        }
        "get" => {
            if !(2..=3).contains(&n) {
                return spec("get(map, key[, default])");
            }
            match &a[0] {
                Value::Map(m) => m
                    .iter()
                    .find(|(k, _)| *k == a[1])
                    .map(|(_, v)| v.clone())
                    .unwrap_or_else(|| a.get(2).cloned().unwrap_or(Value::Null)),
                v => index(v, &a[1])?,
            }
        }
        "all" => {
            arity(1)?;
            Value::Bool(list(&a[0], name)?.iter().all(Value::truthy))
        }
        "any" => {
            arity(1)?;
            Value::Bool(list(&a[0], name)?.iter().any(Value::truthy))
        }
        "int" => {
            arity(1)?;
            match &a[0] {
                Value::Int(i) => Value::Int(*i),
                Value::Float(f) => Value::Int(f.trunc() as i128),
                Value::Bool(b) => Value::Int(*b as i128),
                Value::Char(c) => Value::Int(*c as i128),
                v => Value::Int(
                    text(v, name)?
                        .trim()
                        .parse()
                        .map_err(|_| EvalError::Spec(format!("int: cannot parse {v}")))?,
                ),
            }
        }
        "float" => {
            arity(1)?;
            match &a[0] {
                v if v.as_f64().is_some() => Value::Float(v.as_f64().unwrap_or_default()),
                v => Value::Float(
                    text(v, name)?
                        .trim()
                        .parse()
                        .map_err(|_| EvalError::Spec(format!("float: cannot parse {v}")))?,
                ),
            }
        }
        "str" => {
            arity(1)?;
            Value::Str(a[0].text().unwrap_or_else(|| a[0].to_string()))
        }
        "is_sorted" => {
            arity(1)?;
            let l = list(&a[0], name)?;
            Value::Bool(
                l.windows(2)
                    .all(|w| w[0].cmp_values(&w[1]) != std::cmp::Ordering::Greater),
            )
        }
        "permutation" => {
            arity(2)?;
            let mut x = list(&a[0], name)?;
            let mut y = list(&a[1], name)?;
            x.sort_by(|p, q| p.cmp_values(q));
            y.sort_by(|p, q| p.cmp_values(q));
            Value::Bool(x == y)
        }
        "repeat" => {
            arity(2)?;
            let k = int(&a[1], name)?.clamp(0, 1_000_000) as usize;
            match &a[0] {
                Value::List(l) => {
                    Value::List(l.iter().cycle().take(l.len() * k).cloned().collect())
                }
                v => Value::Str(text(v, name)?.repeat(k)),
            }
        }
        "range" => {
            let (lo, hi) = match n {
                1 => (0, int(&a[0], name)?),
                2 => (int(&a[0], name)?, int(&a[1], name)?),
                _ => return spec("range(n) or range(lo, hi)"),
            };
            if hi - lo > 1_000_000 {
                return spec("range too large");
            }
            Value::List((lo..hi).map(Value::Int).collect())
        }
        "slice" => {
            if !(2..=3).contains(&n) {
                return spec("slice(x, start[, end])");
            }
            let is_str = matches!(a[0], Value::Str(_));
            let l = list(&a[0], name)?;
            let len = l.len() as i128;
            let norm = |i: i128| (if i < 0 { len + i } else { i }).clamp(0, len) as usize;
            let s = norm(int(&a[1], name)?);
            let e = if n == 3 {
                norm(int(&a[2], name)?)
            } else {
                l.len()
            };
            let part = if s < e { l[s..e].to_vec() } else { Vec::new() };
            if is_str {
                Value::Str(part.iter().filter_map(Value::text).collect())
            } else {
                Value::List(part)
            }
        }
        "starts_with" | "ends_with" => {
            arity(2)?;
            let s = text(&a[0], name)?;
            let p = text(&a[1], name)?;
            Value::Bool(if name == "starts_with" {
                s.starts_with(&p)
            } else {
                s.ends_with(&p)
            })
        }
        "matches" => {
            arity(2)?;
            let re = Regex::new(&text(&a[1], name)?)
                .map_err(|e| EvalError::Spec(format!("matches: bad regex: {e}")))?;
            Value::Bool(re.is_match(&text(&a[0], name)?))
        }
        "replace" => {
            arity(3)?;
            Value::Str(text(&a[0], name)?.replace(&text(&a[1], name)?, &text(&a[2], name)?))
        }
        "concat" => {
            let mut out = Vec::new();
            for x in &a {
                out.extend(list(x, name)?);
            }
            Value::List(out)
        }
        // Sequencing: evaluates every argument in order, returns the last.
        "then" => a.last().cloned().unwrap_or(Value::Null),
        "approx" => {
            if !(2..=3).contains(&n) {
                return spec("approx(a, b[, eps])");
            }
            let (Some(x), Some(y)) = (a[0].as_f64(), a[1].as_f64()) else {
                return spec("approx: expected numbers");
            };
            let eps = a.get(2).and_then(Value::as_f64).unwrap_or(1e-6);
            Value::Bool((x - y).abs() <= eps * x.abs().max(y.abs()).max(1.0))
        }
        _ => return spec(format!("unknown function `{name}`")),
    })
}

/// Evaluates a constant expression (no variables, no external calls).
pub fn constant(src: &str) -> Result<Value, String> {
    struct NoHost;
    impl Host for NoHost {
        fn var(&self, _: &str) -> Option<Value> {
            None
        }
        fn call(&mut self, _: &str, _: Args) -> Option<Result<Value, EvalError>> {
            None
        }
        fn is_external(&self, _: &str) -> bool {
            false
        }
    }
    let e = parse(src)?;
    eval(&e, &mut NoHost).map_err(|e| match e {
        EvalError::Spec(m) | EvalError::Student(m) => m,
    })
}

/// Names of every call in `e` (to validate specs and to find external calls).
pub fn calls(e: &Expr, out: &mut Vec<String>) {
    match e {
        Expr::Call(n, args) => {
            out.push(n.clone());
            for (_, a) in args {
                calls(a, out);
            }
        }
        Expr::List(l) => l.iter().for_each(|x| calls(x, out)),
        Expr::Map(m) => m.iter().for_each(|(k, v)| {
            calls(k, out);
            calls(v, out);
        }),
        Expr::Not(x) | Expr::Neg(x) | Expr::Field(x, _) => calls(x, out),
        Expr::Bin(_, a, b) | Expr::Index(a, b) => {
            calls(a, out);
            calls(b, out);
        }
        Expr::Lit(_) | Expr::Name(_) => {}
    }
}

pub enum Outcome {
    Pass,
    Fail(String),
    SpecError(String),
}

/// Runs one boolean check. For a top-level `==`, the side that calls the
/// student's code is reported as "got".
pub fn check(e: &Expr, h: &mut dyn Host) -> Outcome {
    let res = (|| -> Result<Outcome, EvalError> {
        if let Expr::Bin(op @ (BinOp::Eq | BinOp::Ne), l, r) = e {
            let lv = eval(l, h)?;
            let rv = eval(r, h)?;
            let same = lv == rv;
            if same == (*op == BinOp::Eq) {
                return Ok(Outcome::Pass);
            }
            let mut names = Vec::new();
            calls(r, &mut names);
            let right_is_student = names.iter().any(|n| h.is_external(n));
            let mut lnames = Vec::new();
            calls(l, &mut lnames);
            let left_is_student = lnames.iter().any(|n| h.is_external(n));
            let (got, want) = if right_is_student && !left_is_student {
                (&rv, &lv)
            } else {
                (&lv, &rv)
            };
            return Ok(Outcome::Fail(if *op == BinOp::Eq {
                format!("expected {}, got {}", want.short(120), got.short(120))
            } else {
                format!("got {}, which it must not be", got.short(120))
            }));
        }
        let v = eval(e, h)?;
        Ok(if v.truthy() {
            Outcome::Pass
        } else {
            Outcome::Fail("check is false".into())
        })
    })();
    match res {
        Ok(o) => o,
        Err(EvalError::Student(m)) => Outcome::Fail(m),
        Err(EvalError::Spec(m)) => Outcome::SpecError(m),
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn constants_and_builtins() {
        let c = |s: &str| constant(s).unwrap();
        assert_eq!(c("1 + 2 * 3"), Value::Int(7));
        assert_eq!(
            c(r#"join(split("a,,b", ","), "-")"#),
            Value::Str("a--b".into())
        );
        assert_eq!(
            c(r#"nonempty(split("a,,b", ","))"#),
            constant(r#"["a", "b"]"#).unwrap()
        );
        assert_eq!(c(r#"len("héllo")"#), Value::Int(5));
        assert_eq!(c(r#"bytes("héllo")"#), Value::Int(6));
        assert_eq!(c("sorted([3, 1, 2]) == [1, 2, 3]"), Value::Bool(true));
        assert_eq!(c(r#"{"a": 1}.a"#), Value::Int(1));
        assert_eq!(c("[1, 2, 3][-1]"), Value::Int(3));
        assert_eq!(c("!(1 < 2) || 2 >= 2"), Value::Bool(true));
        assert_eq!(c(r#"permutation("abc", "cab")"#), Value::Bool(true));
        assert_eq!(c("','"), Value::Char(','));
        assert!(constant("1 +").is_err());
        assert!(constant("nope(1)").is_err());
    }

    #[test]
    fn dotted_names_lex_as_one_ident() {
        let e = parse("logstat.count_levels([]) == {}").unwrap();
        let mut names = Vec::new();
        calls(&e, &mut names);
        assert_eq!(names, vec!["logstat.count_levels"]);
        let e = parse("run(stdin=\"x\").out").unwrap();
        assert!(matches!(e, Expr::Field(_, ref f) if f == "out"));
    }
}
