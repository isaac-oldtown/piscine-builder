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

const PUNCT3: &[(&str, &str)] = &[("===", "=="), ("!==", "!=")];
const PUNCT2: &[&str] = &["==", "!=", "<=", ">=", "&&", "||", "::"];
const PUNCT1: &[&str] = &[
    "(", ")", "[", "]", "{", "}", ",", ":", "=", "<", ">", "!", "+", "-", "*", "/", "%", ".", "&",
];

fn hex_digits(cs: &[char], i: &mut usize, n: usize, what: &str) -> Result<u32, String> {
    let h: String = cs
        .get(*i..*i + n)
        .ok_or_else(|| format!("bad {what} escape"))?
        .iter()
        .collect();
    *i += n;
    u32::from_str_radix(&h, 16).map_err(|_| format!("bad {what} escape"))
}

/// Escapes of the common languages: `\n \t \r \0 \a \b \f \v \e \\ \" \'`,
/// octal `\NNN`, `\xHH`, `\uXXXX` (with surrogate pairs), `\u{X..}`, `\UXXXXXXXX`.
fn read_escape(cs: &[char], i: &mut usize) -> Result<char, String> {
    let c = *cs.get(*i).ok_or("unterminated escape")?;
    *i += 1;
    let scalar = |n: u32| char::from_u32(n).ok_or_else(|| format!("bad code point {n:#x}"));
    Ok(match c {
        'n' => '\n',
        't' => '\t',
        'r' => '\r',
        'a' => '\u{7}',
        'b' => '\u{8}',
        'f' => '\u{c}',
        'v' => '\u{b}',
        'e' => '\u{1b}',
        '\\' => '\\',
        '"' => '"',
        '\'' => '\'',
        '`' => '`',
        '/' => '/',
        '0'..='7' => {
            let mut n = c.to_digit(8).unwrap_or(0);
            for _ in 0..2 {
                match cs.get(*i).and_then(|d| d.to_digit(8)) {
                    Some(d) => {
                        n = n * 8 + d;
                        *i += 1;
                    }
                    None => break,
                }
            }
            scalar(n)?
        }
        'x' => char::from(hex_digits(cs, i, 2, "\\x")? as u8),
        'U' => scalar(hex_digits(cs, i, 8, "\\U")?)?,
        'u' if cs.get(*i) == Some(&'{') => {
            let end = cs[*i..]
                .iter()
                .position(|&c| c == '}')
                .ok_or("bad \\u escape")?;
            let h: String = cs[*i + 1..*i + end].iter().collect();
            *i += end + 1;
            scalar(u32::from_str_radix(&h, 16).map_err(|_| "bad \\u escape")?)?
        }
        'u' => {
            let hi = hex_digits(cs, i, 4, "\\u")?;
            if (0xD800..0xDC00).contains(&hi)
                && cs.get(*i) == Some(&'\\')
                && cs.get(*i + 1) == Some(&'u')
            {
                let mut j = *i + 2;
                let lo = hex_digits(cs, &mut j, 4, "\\u")?;
                if (0xDC00..0xE000).contains(&lo) {
                    *i = j;
                    return scalar(0x10000 + ((hi - 0xD800) << 10) + (lo - 0xDC00));
                }
            }
            scalar(hi)?
        }
        other => return Err(format!("unknown escape \\{other}")),
    })
}

/// Reads a string quoted by `cs[*i]` (`"` or `'`), with escapes.
fn read_quoted(cs: &[char], i: &mut usize) -> Result<String, String> {
    let q = cs[*i];
    *i += 1;
    let mut s = String::new();
    loop {
        match cs.get(*i) {
            None => return Err("unterminated string".into()),
            Some(&c) if c == q => {
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

/// Reads a "double-quoted" string starting at `cs[*i] == '"'`.
pub fn read_string(cs: &[char], i: &mut usize) -> Result<String, String> {
    read_quoted(cs, i)
}

/// Rust raw string at `cs[*i]` (after the `r`): `"..."` or `#"..."#`.
fn read_raw(cs: &[char], i: &mut usize) -> Result<String, String> {
    let mut hashes = 0;
    while cs.get(*i) == Some(&'#') {
        hashes += 1;
        *i += 1;
    }
    if cs.get(*i) != Some(&'"') {
        return Err("bad raw string".into());
    }
    *i += 1;
    let start = *i;
    while *i < cs.len() {
        if cs[*i] == '"' && (0..hashes).all(|k| cs.get(*i + 1 + k) == Some(&'#')) {
            let s: String = cs[start..*i].iter().collect();
            *i += 1 + hashes;
            return Ok(s);
        }
        *i += 1;
    }
    Err("unterminated raw string".into())
}

/// Type suffixes on number literals (`10u8`, `2.5f64`, `10L`, `10n`).
const NUM_SUFFIXES: &[&str] = &[
    "u8", "u16", "u32", "u64", "u128", "usize", "i8", "i16", "i32", "i64", "i128", "isize", "f32",
    "f64", "L", "l", "u", "U", "UL", "ul", "LL", "ll", "ULL", "ull", "n", "f", "F", "d", "D",
];

fn read_number(cs: &[char], i: &mut usize) -> Result<Tok, String> {
    let start = *i;
    let radix = match (cs[*i], cs.get(*i + 1)) {
        ('0', Some('x' | 'X')) => 16,
        ('0', Some('b' | 'B')) => 2,
        ('0', Some('o' | 'O')) => 8,
        _ => 10,
    };
    if radix != 10 {
        *i += 2;
        let s = *i;
        while *i < cs.len() && (cs[*i].is_digit(radix) || cs[*i] == '_') {
            *i += 1;
        }
        let digits: String = cs[s..*i].iter().filter(|&&c| c != '_').collect();
        let v = i128::from_str_radix(&digits, radix).map_err(|_| "bad number".to_string())?;
        skip_suffix(cs, i);
        return Ok(Tok::Int(v));
    }
    while *i < cs.len() && (cs[*i].is_ascii_digit() || cs[*i] == '_') {
        *i += 1;
    }
    let mut float = false;
    if *i + 1 < cs.len() && cs[*i] == '.' && cs[*i + 1].is_ascii_digit() {
        float = true;
        *i += 1;
        while *i < cs.len() && (cs[*i].is_ascii_digit() || cs[*i] == '_') {
            *i += 1;
        }
    }
    if *i < cs.len() && (cs[*i] == 'e' || cs[*i] == 'E') {
        let mut j = *i + 1;
        if j < cs.len() && (cs[j] == '+' || cs[j] == '-') {
            j += 1;
        }
        if j < cs.len() && cs[j].is_ascii_digit() {
            float = true;
            *i = j;
            while *i < cs.len() && cs[*i].is_ascii_digit() {
                *i += 1;
            }
        }
    }
    let text: String = cs[start..*i].iter().filter(|&&c| c != '_').collect();
    let suffix = skip_suffix(cs, i);
    float |= matches!(suffix.as_str(), "f32" | "f64" | "f" | "F" | "d" | "D");
    Ok(if float {
        Tok::Float(text.parse().map_err(|_| format!("bad number `{text}`"))?)
    } else {
        Tok::Int(text.parse().map_err(|_| format!("bad number `{text}`"))?)
    })
}

/// Skips a known type suffix right after a number; returns it.
fn skip_suffix(cs: &[char], i: &mut usize) -> String {
    let word: String = cs[*i..]
        .iter()
        .take_while(|c| c.is_ascii_alphanumeric() || **c == '_')
        .collect();
    let clean = word.trim_start_matches('_');
    if NUM_SUFFIXES.contains(&clean) {
        *i += word.chars().count();
        clean.to_string()
    } else {
        String::new()
    }
}

fn lex(src: &str) -> Result<Vec<Tok>, String> {
    let cs: Vec<char> = src.chars().collect();
    let mut i = 0;
    let mut out: Vec<Tok> = Vec::new();
    while i < cs.len() {
        let c = cs[i];
        if c.is_whitespace() {
            i += 1;
        } else if c.is_ascii_digit() {
            out.push(read_number(&cs, &mut i)?);
        } else if c == '"' || c == '`' {
            out.push(Tok::Str(read_quoted(&cs, &mut i)?));
        } else if c == '\'' {
            // One character is a char (Rust, C, Java); more is a string (Python, JS).
            let s = read_quoted(&cs, &mut i)?;
            let mut it = s.chars();
            out.push(match (it.next(), it.next()) {
                (Some(ch), None) => Tok::Char(ch),
                _ => Tok::Str(s),
            });
        } else if (c == 'r' || c == 'R')
            && cs[i + 1..].iter().find(|c| **c != '#') == Some(&'"')
            && !cs
                .get(i.wrapping_sub(1))
                .is_some_and(|p| p.is_alphanumeric() || *p == '_')
        {
            i += 1;
            out.push(Tok::Str(read_raw(&cs, &mut i)?));
        } else if c == 'b' && matches!(cs.get(i + 1), Some('"')) {
            i += 1;
            out.push(Tok::Str(read_quoted(&cs, &mut i)?));
        } else if c.is_alphabetic() || c == '_' || c == '$' {
            let start = i;
            while i < cs.len() {
                let d = cs[i];
                let dotted = d == '.'
                    && cs
                        .get(i + 1)
                        .is_some_and(|n| n.is_alphabetic() || *n == '_');
                if d.is_alphanumeric() || d == '_' || d == '$' || dotted {
                    i += 1;
                } else {
                    break;
                }
            }
            let word: String = cs[start..i].iter().collect();
            // Word operators of Python and friends.
            match word.as_str() {
                "and" => out.push(Tok::P("&&")),
                "or" => out.push(Tok::P("||")),
                "in" if out.last() == Some(&Tok::P("!")) => {
                    out.pop();
                    out.push(Tok::P("notin"));
                }
                "not" => out.push(Tok::P("!")),
                "in" => out.push(Tok::P("in")),
                "is" => {
                    let mut j = i;
                    while j < cs.len() && cs[j].is_whitespace() {
                        j += 1;
                    }
                    let negated = cs.get(j..j + 3) == Some(&['n', 'o', 't'][..])
                        && cs.get(j + 3).is_some_and(|c| c.is_whitespace());
                    if negated {
                        i = j + 3;
                        out.push(Tok::P("!="));
                    } else {
                        out.push(Tok::P("=="));
                    }
                }
                _ => out.push(Tok::Ident(word)),
            }
        } else {
            let three: String = cs[i..(i + 3).min(cs.len())].iter().collect();
            let two: String = cs[i..(i + 2).min(cs.len())].iter().collect();
            if let Some((_, p)) = PUNCT3.iter().find(|(s, _)| *s == three) {
                let p = PUNCT2.iter().find(|q| *q == p).copied().unwrap_or("==");
                out.push(Tok::P(p));
                i += 3;
            } else if let Some(p) = PUNCT2.iter().find(|p| **p == two) {
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

/// Constructors that only wrap a value in some language: `Ok(v)` is `v`
/// (harnesses unwrap them), `String::from("a")` is `"a"`.
const WRAPPERS: &[&str] = &[
    "Ok",
    "Some",
    "Just",
    "Right",
    "Box::new",
    "Rc::new",
    "Arc::new",
    "RefCell::new",
    "Cell::new",
    "Cow::from",
    "String::from",
    "str::to_string",
    "Vec::from",
    "HashSet::from",
    "BTreeSet::from",
    "VecDeque::from",
    "HashMap::from",
    "BTreeMap::from",
    "std::string::String::from",
];

/// Error results: a call compared with one of these must raise.
const ERR_WRAPPERS: &[&str] = &["Err", "Left"];

/// Empty constructors: `Vec::new()` is `[]`.
fn empty_constructor(name: &str) -> Option<Value> {
    let ty = name
        .strip_suffix("::new")
        .or_else(|| name.strip_suffix("::default"))?;
    let ty = ty.rsplit("::").next().unwrap_or(ty);
    Some(match ty {
        "String" => Value::Str(String::new()),
        "Vec" | "VecDeque" | "HashSet" | "BTreeSet" | "LinkedList" | "BinaryHeap" => {
            Value::List(Vec::new())
        }
        "HashMap" | "BTreeMap" => Value::Map(Vec::new()),
        _ => return None,
    })
}

fn is_err_wrapper(e: &Expr) -> bool {
    matches!(e, Expr::Call(n, _) if ERR_WRAPPERS.contains(&n.as_str()))
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
            // `x in xs` / `x not in xs` (Python).
            if level == 2 && (self.is("in") || self.is("notin")) {
                let negated = self.is("notin");
                self.pos += 1;
                let right = self.binary(level + 1)?;
                let c = Expr::Call("contains".into(), vec![(None, right), (None, left)]);
                return Ok(if negated { Expr::Not(Box::new(c)) } else { c });
            }
            let Some(&(_, op)) = LEVELS[level].iter().find(|(p, _)| self.is(p)) else {
                return Ok(left);
            };
            self.pos += 1;
            let right = self.binary(level + 1)?;
            // `f(x) == Err(...)`: the call must raise (the payload is not compared:
            // error values differ between correct solutions).
            if matches!(op, BinOp::Eq | BinOp::Ne)
                && (is_err_wrapper(&right) || is_err_wrapper(&left))
            {
                let call = if is_err_wrapper(&right) { left } else { right };
                let raises = Expr::Call("raises".into(), vec![(None, call)]);
                return Ok(if op == BinOp::Eq {
                    raises
                } else {
                    Expr::Not(Box::new(raises))
                });
            }
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
        if self.eat("+") {
            return self.unary();
        }
        // Borrows and dereferences carry no value: `&[1, 2]`, `&mut v`, `*p`.
        if self.eat("&") || self.eat("*") {
            if matches!(self.peek(), Some(Tok::Ident(w)) if w == "mut") {
                self.pos += 1;
            }
            return self.unary();
        }
        if self.eat("&&") {
            return self.unary();
        }
        self.postfix()
    }

    fn call_args(&mut self) -> Result<Vec<(Option<String>, Expr)>, String> {
        let mut args = Vec::new();
        while !self.eat(")") {
            let named = match (self.peek(), self.toks.get(self.pos + 1)) {
                (Some(Tok::Ident(n)), Some(Tok::P("=" | ":"))) => Some(n.clone()),
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
        Ok(args)
    }

    fn postfix(&mut self) -> Result<Expr, String> {
        let mut e = self.primary()?;
        loop {
            if self.eat(".") {
                match self.peek().cloned() {
                    Some(Tok::Ident(f)) => {
                        self.pos += 1;
                        // The lexer joins `a.b.c`; split it back into steps.
                        let mut parts: Vec<String> = f.split('.').map(str::to_string).collect();
                        let last = parts.pop().unwrap_or_default();
                        for p in parts {
                            e = Expr::Field(Box::new(e), p);
                        }
                        if self.eat("(") {
                            let args = self.call_args()?.into_iter().map(|(_, a)| a).collect();
                            e = method(e, &last, args)?;
                        } else {
                            e = Expr::Field(Box::new(e), last);
                        }
                    }
                    // Rust tuple fields: `pair.0`.
                    Some(Tok::Int(n)) => {
                        self.pos += 1;
                        e = Expr::Index(Box::new(e), Box::new(Expr::Lit(Value::Int(n))));
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

    fn list_items(&mut self, close: &str) -> Result<Vec<Expr>, String> {
        let mut items = Vec::new();
        while !self.eat(close) {
            items.push(self.expr()?);
            if !self.eat(",") {
                self.expect(close)?;
                break;
            }
        }
        Ok(items)
    }

    fn primary(&mut self) -> Result<Expr, String> {
        let t = self.peek().cloned().ok_or("unexpected end of expression")?;
        self.pos += 1;
        Ok(match t {
            Tok::Int(i) => Expr::Lit(Value::Int(i)),
            Tok::Float(f) => Expr::Lit(Value::Float(f)),
            Tok::Str(s) => Expr::Lit(Value::Str(s)),
            Tok::Char(c) => Expr::Lit(Value::Char(c)),
            Tok::Ident(first) => {
                // Paths: `String::from`, `std::collections::HashMap::new`.
                let mut name = first;
                while self.is("::") {
                    match self.toks.get(self.pos + 1).cloned() {
                        Some(Tok::Ident(seg)) => {
                            self.pos += 2;
                            name = format!("{name}::{seg}");
                        }
                        // Turbofish `::<T>` and the like: not supported, say so.
                        _ => return Err(format!("unexpected `::` after `{name}`")),
                    }
                }
                match name.as_str() {
                    "true" | "True" | "TRUE" => Expr::Lit(Value::Bool(true)),
                    "false" | "False" | "FALSE" => Expr::Lit(Value::Bool(false)),
                    "null" | "None" | "NULL" | "nil" | "Nil" | "undefined" | "Nothing"
                    | "nullptr" => Expr::Lit(Value::Null),
                    "vec" if self.is("!") => {
                        self.pos += 1;
                        if self.eat("[") {
                            Expr::List(self.list_items("]")?)
                        } else if self.eat("(") {
                            Expr::List(self.list_items(")")?)
                        } else {
                            return Err("expected `vec![...]`".into());
                        }
                    }
                    _ if self.eat("(") => {
                        let args = self.call_args()?;
                        let plain = args.iter().all(|(n, _)| n.is_none());
                        if plain && args.len() == 1 && WRAPPERS.contains(&name.as_str()) {
                            return Ok(args
                                .into_iter()
                                .next()
                                .map(|(_, a)| a)
                                .unwrap_or(Expr::Lit(Value::Null)));
                        }
                        if args.is_empty()
                            && let Some(v) = empty_constructor(&name)
                        {
                            return Ok(Expr::Lit(v));
                        }
                        // is_err(x) and friends, as free functions.
                        if plain
                            && args.len() == 1
                            && matches!(name.as_str(), "is_err" | "is_ok" | "is_some" | "is_none")
                        {
                            let arg = args.into_iter().next().map(|(_, a)| a);
                            return method(
                                arg.unwrap_or(Expr::Lit(Value::Null)),
                                &name,
                                Vec::new(),
                            );
                        }
                        Expr::Call(name, args)
                    }
                    _ => Expr::Name(name),
                }
            }
            // Tuples are lists; `()` is the unit value, i.e. nothing.
            Tok::P("(") => {
                if self.eat(")") {
                    return Ok(Expr::Lit(Value::Null));
                }
                let first = self.expr()?;
                if self.eat(")") {
                    return Ok(first);
                }
                self.expect(",")?;
                let mut items = vec![first];
                items.extend(self.list_items(")")?);
                Expr::List(items)
            }
            Tok::P("[") => Expr::List(self.list_items("]")?),
            Tok::P("{") => {
                let mut items = Vec::new();
                while !self.eat("}") {
                    let k = self.expr()?;
                    self.expect(":")?;
                    // `{a: 1}` (JS): a bare word key is a string.
                    let k = match k {
                        Expr::Name(n) => Expr::Lit(Value::Str(n)),
                        other => other,
                    };
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

/// Methods that only convert or borrow: `x.clone()` is `x`.
const IDENTITY_METHODS: &[&str] = &[
    "clone",
    "to_owned",
    "into",
    "as_str",
    "as_ref",
    "as_slice",
    "as_deref",
    "borrow",
    "to_vec",
    "iter",
    "into_iter",
    "collect",
    "cloned",
    "copied",
    "unwrap",
    "expect",
    "valueOf",
];

/// Other languages' method names for the built-ins.
const METHOD_ALIASES: &[(&str, &str)] = &[
    ("length", "len"),
    ("size", "len"),
    ("to_string", "str"),
    ("toString", "str"),
    ("to_lowercase", "lower"),
    ("to_ascii_lowercase", "lower"),
    ("toLowerCase", "lower"),
    ("to_uppercase", "upper"),
    ("to_ascii_uppercase", "upper"),
    ("toUpperCase", "upper"),
    ("strip", "trim"),
    ("split_whitespace", "words"),
    ("startswith", "starts_with"),
    ("startsWith", "starts_with"),
    ("endswith", "ends_with"),
    ("endsWith", "ends_with"),
    ("includes", "contains"),
    ("contains_key", "contains"),
    ("rev", "reversed"),
];

/// `recv.name(args)` becomes a built-in call: `s.len()` is `len(s)`,
/// `r.is_err()` is `raises(r)`. Only built-ins: a student's function is
/// called as `f(x)`.
fn method(recv: Expr, name: &str, mut args: Vec<Expr>) -> Result<Expr, String> {
    let no_args = |args: &[Expr]| {
        if args.is_empty() {
            Ok(())
        } else {
            Err(format!("`.{name}()` takes no arguments"))
        }
    };
    if IDENTITY_METHODS.contains(&name) {
        return Ok(recv);
    }
    match name {
        "is_err" | "is_ok" => {
            no_args(&args)?;
            let r = Expr::Call("raises".into(), vec![(None, recv)]);
            return Ok(if name == "is_err" {
                r
            } else {
                Expr::Not(Box::new(r))
            });
        }
        "is_some" | "is_none" => {
            no_args(&args)?;
            let op = if name == "is_none" {
                BinOp::Eq
            } else {
                BinOp::Ne
            };
            return Ok(Expr::Bin(
                op,
                Box::new(recv),
                Box::new(Expr::Lit(Value::Null)),
            ));
        }
        "is_empty" => {
            no_args(&args)?;
            let len = Expr::Call("len".into(), vec![(None, recv)]);
            return Ok(Expr::Bin(
                BinOp::Eq,
                Box::new(len),
                Box::new(Expr::Lit(Value::Int(0))),
            ));
        }
        _ => {}
    }
    let target = METHOD_ALIASES
        .iter()
        .find(|(m, _)| *m == name)
        .map_or(name, |(_, b)| b);
    let callable = BUILTINS.contains(&target)
        && !matches!(
            target,
            "raises" | "then" | "Ok" | "Some" | "range" | "approx"
        );
    if !callable {
        return Err(format!(
            "unknown method `.{name}()`: only built-ins work as methods (`s.len()` is len(s)); call your own functions as {name}(x)"
        ));
    }
    args.insert(0, recv);
    Ok(Expr::Call(
        target.to_string(),
        args.into_iter().map(|a| (None, a)).collect(),
    ))
}

/// `Err(...)` left anywhere but beside `==` / `!=`.
fn stray_err(e: &Expr) -> Option<String> {
    let mut found = None;
    walk(e, &mut |x| {
        if found.is_none()
            && let Expr::Call(n, _) = x
            && ERR_WRAPPERS.contains(&n.as_str())
        {
            found = Some(format!(
                "`{n}(...)` only works beside `==` or `!=` (f(x) == {n}(...) means the call fails); otherwise write raises(f(x))"
            ));
        }
    });
    found
}

/// Visits every node of `e`.
pub fn walk(e: &Expr, f: &mut dyn FnMut(&Expr)) {
    f(e);
    match e {
        Expr::Call(_, args) => args.iter().for_each(|(_, a)| walk(a, f)),
        Expr::List(l) => l.iter().for_each(|x| walk(x, f)),
        Expr::Map(m) => m.iter().for_each(|(k, v)| {
            walk(k, f);
            walk(v, f);
        }),
        Expr::Not(x) | Expr::Neg(x) | Expr::Field(x, _) => walk(x, f),
        Expr::Bin(_, a, b) | Expr::Index(a, b) => {
            walk(a, f);
            walk(b, f);
        }
        Expr::Lit(_) | Expr::Name(_) => {}
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
    if let Some(m) = stray_err(&e) {
        return Err(m);
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
        // JavaScript's `xs.length`.
        Value::List(_) | Value::Str(_) if name == "length" => builtin("len", vec![v.clone()]),
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
            // all(xs, pred) / any(xs, pred): `pred` names a one-argument built-in.
            if let ("all" | "any", [(None, xs), (None, Expr::Name(p))]) = (name.as_str(), &args[..])
                && h.var(p).is_none()
            {
                if !BUILTINS.contains(&p.as_str()) || matches!(p.as_str(), "raises" | "then") {
                    return spec(format!(
                        "{name}(xs, {p}): the second argument must name a built-in such as nonempty or is_sorted"
                    ));
                }
                let mut hits = Vec::new();
                for x in list(&eval(xs, h)?, name)? {
                    hits.push(Value::Bool(builtin(p, vec![x])?.truthy()));
                }
                return builtin(name, vec![Value::List(hits)]);
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
    "Ok",
    "Some",
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
            // Python's `sep.join(parts)` arrives as join(sep, parts).
            let (parts, sep) = match (&a[0], &a[1]) {
                (Value::Str(_), Value::List(_)) => (&a[1], &a[0]),
                _ => (&a[0], &a[1]),
            };
            let sep = text(sep, name)?;
            let parts: Result<Vec<String>, _> =
                list(parts, name)?.iter().map(|x| text(x, name)).collect();
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
        // Harnesses unwrap Ok(v) / Some(v) to v (None is null, Err raises), so
        // Rust-style expectations read as the plain value.
        "Ok" | "Some" => {
            arity(1)?;
            a[0].clone()
        }
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

fn distance(a: &str, b: &str) -> usize {
    let b: Vec<char> = b.chars().collect();
    let mut row: Vec<usize> = (0..=b.len()).collect();
    for (i, ca) in a.chars().enumerate() {
        let mut prev = row[0];
        row[0] = i + 1;
        for (j, cb) in b.iter().enumerate() {
            let cur = row[j + 1];
            row[j + 1] = (prev + usize::from(ca != *cb)).min(row[j] + 1).min(cur + 1);
            prev = cur;
        }
    }
    row[b.len()]
}

/// An expected function: name, fewest and most arguments.
pub type Arity = (String, usize, usize);

/// `s.len()` lexes as a call named `s.len`. Unless that is an expected
/// function (Python's `Stack.push`), it is the method `len` on `s`.
pub fn resolve_methods(e: Expr, functions: &[Arity]) -> Result<Expr, String> {
    let r = |x: Expr| resolve_methods(x, functions);
    let rb = |x: Box<Expr>| resolve_methods(*x, functions).map(Box::new);
    Ok(match e {
        Expr::Call(name, args) => {
            let args = args
                .into_iter()
                .map(|(n, a)| Ok((n, r(a)?)))
                .collect::<Result<Vec<_>, String>>()?;
            match name.rsplit_once('.') {
                Some((recv, m))
                    if !functions.iter().any(|(f, ..)| *f == name)
                        && !BUILTINS.contains(&name.as_str()) =>
                {
                    let args = args.into_iter().map(|(_, a)| a).collect();
                    method(Expr::Name(recv.to_string()), m, args)?
                }
                _ => Expr::Call(name, args),
            }
        }
        Expr::List(l) => Expr::List(l.into_iter().map(r).collect::<Result<_, _>>()?),
        Expr::Map(m) => Expr::Map(
            m.into_iter()
                .map(|(k, v)| Ok((r(k)?, r(v)?)))
                .collect::<Result<_, String>>()?,
        ),
        Expr::Not(x) => Expr::Not(rb(x)?),
        Expr::Neg(x) => Expr::Neg(rb(x)?),
        Expr::Field(x, f) => Expr::Field(rb(x)?, f),
        Expr::Bin(op, a, b) => Expr::Bin(op, rb(a)?, rb(b)?),
        Expr::Index(a, b) => Expr::Index(rb(a)?, rb(b)?),
        e @ (Expr::Lit(_) | Expr::Name(_)) => e,
    })
}

/// ` (closest: `x`)` when a known name is a likely typo of `name`.
fn closest<'a>(name: &str, known: impl Iterator<Item = &'a str>) -> String {
    known
        .map(|k| {
            let prefix =
                k.len().min(name.len()) >= 3 && (name.starts_with(k) || k.starts_with(name));
            (if prefix { 1 } else { distance(name, k) }, k)
        })
        .filter(|(d, k)| *d <= 2.max(k.len() / 3))
        .min()
        .map(|(_, k)| format!(" (closest: `{k}`)"))
        .unwrap_or_default()
}

/// Checks names before anything runs: every call is one of `functions`
/// (name, parameter count), a built-in or `run`, and every bare name is one
/// of `vars`. The message says what to write instead.
pub fn static_check(e: &Expr, functions: &[Arity], vars: &[String]) -> Result<(), String> {
    let mut err: Option<String> = None;
    let mut predicates: Vec<*const Expr> = Vec::new();
    walk(e, &mut |x| {
        if err.is_some() {
            return;
        }
        match x {
            Expr::Call(name, args) => {
                if let ("all" | "any", [_, (None, p @ Expr::Name(_))]) = (name.as_str(), &args[..])
                {
                    predicates.push(p as *const Expr);
                }
                if let Some((_, min, max)) = functions.iter().find(|(f, ..)| f == name) {
                    if !(*min..=*max).contains(&args.len()) {
                        let want = if min == max {
                            min.to_string()
                        } else {
                            format!("{min} to {max}")
                        };
                        err = Some(format!(
                            "`{name}` takes {want} argument(s), this call passes {}",
                            args.len()
                        ));
                    }
                } else if !BUILTINS.contains(&name.as_str()) && name != "run" {
                    let known = functions
                        .iter()
                        .map(|(f, ..)| f.as_str())
                        .chain(BUILTINS.iter().copied());
                    err = Some(format!(
                        "unknown function `{name}`{}: call an expected function or a built-in",
                        closest(name, known)
                    ));
                }
            }
            Expr::Name(n) if !predicates.contains(&(x as *const Expr)) => {
                let base = n.split('.').next().unwrap_or(n);
                if !vars.iter().any(|v| v == base) {
                    err = Some(if vars.is_empty() {
                        format!(
                            "unknown name `{n}`: a case has no variables, write the value itself"
                        )
                    } else {
                        format!(
                            "unknown name `{n}`{}: the variables here are {}",
                            closest(base, vars.iter().map(String::as_str)),
                            vars.join(", ")
                        )
                    });
                }
            }
            Expr::Name(n) if !BUILTINS.contains(&n.as_str()) => {
                err = Some(format!(
                    "`{n}` is not a built-in{}: all(xs, p) / any(xs, p) need a built-in such as nonempty",
                    closest(n, BUILTINS.iter().copied())
                ));
            }
            _ => {}
        }
    });
    err.map_or(Ok(()), Err)
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
        assert_eq!(c("Ok(80) == 80"), Value::Bool(true));
        assert_eq!(c("Some([1, 2])"), constant("[1, 2]").unwrap());
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

    /// Student functions for the corpus: an `Err` is a raise, as in harnesses.
    struct Fake;
    impl Host for Fake {
        fn var(&self, name: &str) -> Option<Value> {
            (name == "s").then(|| Value::Str("one  two".into()))
        }
        fn call(&mut self, name: &str, args: Args) -> Option<Result<Value, EvalError>> {
            let a: Vec<Value> = args.into_iter().map(|(_, v)| v).collect();
            let txt = |i: usize| a[i].text().unwrap_or_default();
            Some(match name {
                "ft_parse_port" => match txt(0).parse::<i128>() {
                    Ok(n) if n > 0 => Ok(Value::Int(n)),
                    _ => Err(EvalError::Student("port".into())),
                },
                "ft_find" => {
                    let l = list(&a[0], name).unwrap_or_default();
                    Ok(l.iter()
                        .position(|x| *x == a[1])
                        .map_or(Value::Null, |i| Value::Int(i as i128)))
                }
                "ft_split" => Ok(Value::List(
                    txt(0)
                        .split_whitespace()
                        .map(|w| Value::Str(w.into()))
                        .collect(),
                )),
                "ft_pair" => Ok(Value::List(vec![Value::Null, Value::List(Vec::new())])),
                _ => return None,
            })
        }
        fn is_external(&self, name: &str) -> bool {
            name.starts_with("ft_")
        }
    }

    fn fns() -> Vec<Arity> {
        [
            ("ft_parse_port", 1),
            ("ft_find", 2),
            ("ft_split", 1),
            ("ft_pair", 0),
        ]
        .iter()
        .map(|(n, k)| (n.to_string(), *k, *k))
        .collect()
    }

    fn holds(src: &str) -> Result<bool, String> {
        let e = resolve_methods(parse(src)?, &fns())?;
        static_check(&e, &fns(), &["s".into()])?;
        match eval(&e, &mut Fake) {
            Ok(v) => Ok(v.truthy()),
            Err(EvalError::Spec(m) | EvalError::Student(m)) => Err(m),
        }
    }

    /// Lines models really wrote, in the habits of their languages. Each one
    /// must parse, pass the static check and hold. Add new idioms here.
    #[test]
    fn corpus_of_language_habits() {
        let corpus = [
            // Rust
            r#"ft_parse_port("80") == Ok(80)"#,
            r#"ft_parse_port("0") == Err("port must not be zero")"#,
            r#"ft_parse_port("0").is_err()"#,
            r#"ft_parse_port("8").is_ok()"#,
            r#"ft_find(&[], 1) == None"#,
            r#"ft_find(&vec![1, 2], 2) == Some(1)"#,
            r#"ft_find(&[1, 2], 2).is_some()"#,
            r#"ft_find(&mut vec![], 2).is_none()"#,
            r#"ft_pair() == (None, vec![])"#,
            r#"ft_pair() == (None, Vec::new())"#,
            r#"ft_pair().1.is_empty()"#,
            r#"ft_split(&String::from("a b")) == vec!["a".to_string(), "b".to_string()]"#,
            r#"ft_split("a b").len() == 2usize"#,
            r#"ft_split(r"a\b") == [r"a\b"]"#,
            r##"ft_split(r#"x"y"#) == ["x\"y"]"##,
            r#"all(ft_split(s), nonempty)"#,
            r#"s.split_whitespace().len() == 2"#,
            r#"s.len() == 8"#,
            r#"s.starts_with("one")"#,
            // Python
            r#"ft_find([], 1) is None"#,
            r#"ft_find([1], 1) is not None"#,
            r#"ft_split("a b") == ["a", "b"] and True"#,
            r#""b" in ft_split("a b")"#,
            r#""z" not in ft_split("a b")"#,
            r#"" ".join(ft_split("a  b")) == 'a b'"#,
            r#"ft_split('caf\u00e9') == ["caf\u{e9}"]"#,
            r#"ft_split("caf\xe9") == ["café"]"#,
            r#"ft_split("\U0001F600") == ["😀"]"#,
            // JavaScript / Java / C
            r#"ft_find([], 1) === null"#,
            r#"ft_find([5], 5) !== undefined"#,
            r#"ft_split("a b").length == 2"#,
            r#"ft_split("a b").size() == 2"#,
            r#"ft_split("A")[0].toLowerCase() == "a""#,
            r#"ft_find([1], 1) == 0L"#,
            r#"ft_split("\101") == ["A"]"#,
            r#"ft_split("\uD83D\uDE00") == ["😀"]"#,
            r#"ft_find([1], 2) == NULL"#,
            r#"ft_find([1], 2) == nil"#,
        ];
        for line in corpus {
            assert_eq!(holds(line), Ok(true), "{line}");
        }
    }

    /// Mistakes are reported before anything runs, with the fix.
    #[test]
    fn static_check_explains() {
        let err = |src: &str| holds(src).unwrap_err();
        assert!(err("ft_spilt(s) == []").contains("closest: `ft_split`"));
        assert!(err("ft_find([1]) == null").contains("takes 2 argument(s)"));
        assert!(err("ft_split(x) == []").contains("unknown name `x`"));
        assert!(err("all(ft_split(s), nonempty_list)").contains("closest: `nonempty`"));
        assert!(err("s.frobnicate()").contains("only built-ins work as methods"));
        assert!(err("raises(Err(1))").contains("beside `==`"));
        // A case has no variables at all.
        let e = parse("ft_split(s) == []").unwrap();
        assert!(
            static_check(&e, &fns(), &[])
                .unwrap_err()
                .contains("no variables")
        );
    }

    #[test]
    fn err_comparisons_mean_raises() {
        assert_eq!(holds(r#"ft_parse_port("80") == Err(1)"#), Ok(false));
        assert_eq!(holds(r#"ft_parse_port("0") != Err(1)"#), Ok(false));
        assert_eq!(holds(r#"ft_parse_port("80") != Err(1)"#), Ok(true));
    }
}
