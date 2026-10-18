//! Function signatures, per language: the abstract types used to pick
//! generators and harness conversions, and a normalized form used by the
//! prototype check (parameter names never matter).

use crate::gens::{DEFAULT_ALPHABET, Gen};

#[derive(Clone, Debug, PartialEq)]
pub enum Ty {
    Int {
        unsigned: bool,
    },
    Float,
    Bool,
    Char,
    Str,
    /// C `char **`: NULL-terminated array of strings.
    StrList,
    List(Box<Ty>),
    Opt(Box<Ty>),
    Void,
    Other(String),
}

#[derive(Clone, Debug)]
pub struct Param {
    pub name: String,
    pub ty: Ty,
    /// The parameter's type as written (C/Rust) or annotation (Python).
    pub raw: String,
    pub has_default: bool,
}

#[derive(Clone, Debug)]
pub struct Proto {
    /// Full name as in the spec (may be dotted for Python).
    pub name: String,
    pub params: Vec<Param>,
    pub ret: Ty,
    pub ret_raw: String,
    /// Canonical text compared by the prototype check.
    pub norm: String,
    pub is_method: bool,
    /// The signature exactly as the spec wrote it.
    pub sig_text: String,
}

impl Proto {
    pub fn short_name(&self) -> &str {
        self.name.rsplit('.').next().unwrap_or(&self.name)
    }
}

pub fn parse(lang: &str, sig: &str, name: &str) -> Result<Proto, String> {
    let p = match lang {
        "c" => parse_c(sig)?,
        "python" => parse_py(sig)?,
        "rust" => parse_rust(sig)?,
        _ => parse_generic(sig)?,
    };
    let short = name.rsplit('.').next().unwrap_or(name);
    if p.name != short {
        return Err(format!("signature `{sig}` does not declare `{short}`"));
    }
    Ok(Proto {
        name: name.to_string(),
        sig_text: sig.trim().to_string(),
        ..p
    })
}

pub fn default_gen(t: &Ty) -> Option<Gen> {
    Some(match t {
        Ty::Int { unsigned: true } => Gen::Int {
            lo: 0,
            hi: 1000,
            width: 0,
        },
        Ty::Int { unsigned: false } => Gen::Int {
            lo: -1000,
            hi: 1000,
            width: 0,
        },
        Ty::Float => Gen::Float {
            lo: -1000.0,
            hi: 1000.0,
        },
        Ty::Bool => Gen::Bool,
        Ty::Char => Gen::Char {
            alphabet: DEFAULT_ALPHABET.chars().collect(),
        },
        Ty::Str => Gen::Str {
            alphabet: DEFAULT_ALPHABET.chars().collect(),
            lo: 0,
            hi: 20,
        },
        Ty::StrList => Gen::List {
            inner: Box::new(Gen::Str {
                alphabet: DEFAULT_ALPHABET.chars().collect(),
                lo: 0,
                hi: 10,
            }),
            lo: 0,
            hi: 8,
        },
        Ty::List(inner) => Gen::List {
            inner: Box::new(default_gen(inner)?),
            lo: 0,
            hi: 10,
        },
        Ty::Opt(inner) => Gen::Opt(Box::new(default_gen(inner)?)),
        Ty::Void | Ty::Other(_) => return None,
    })
}

// ------------------------------------------------------------------------- C

/// Tokens of C declarations: identifiers, numbers, and single punctuation.
pub fn c_tokens(s: &str) -> Vec<String> {
    let mut out = Vec::new();
    let cs: Vec<char> = s.chars().collect();
    let mut i = 0;
    while i < cs.len() {
        let c = cs[i];
        if c.is_whitespace() {
            i += 1;
        } else if c.is_alphanumeric() || c == '_' {
            let start = i;
            while i < cs.len() && (cs[i].is_alphanumeric() || cs[i] == '_') {
                i += 1;
            }
            out.push(cs[start..i].iter().collect());
        } else if c == '.' && cs.get(i + 1) == Some(&'.') && cs.get(i + 2) == Some(&'.') {
            out.push("...".into());
            i += 3;
        } else {
            out.push(c.to_string());
            i += 1;
        }
    }
    out
}

const C_TYPE_WORDS: &[&str] = &[
    "void", "char", "short", "int", "long", "float", "double", "signed", "unsigned", "_Bool",
    "bool", "const", "volatile", "restrict", "struct", "union", "enum", "size_t", "ssize_t",
];

fn is_ident(t: &str) -> bool {
    t.chars()
        .next()
        .is_some_and(|c| c.is_alphabetic() || c == '_')
}

/// Drops the parameter name from a C parameter's tokens; `[]` becomes `*`.
pub fn c_param_type(tokens: &[String]) -> (Vec<String>, String) {
    let mut t: Vec<String> = tokens.to_vec();
    let mut arrays = 0;
    while t.last().map(String::as_str) == Some("]") {
        match t.iter().rposition(|x| x == "[") {
            Some(open) => {
                t.truncate(open);
                arrays += 1;
            }
            None => break,
        }
    }
    let mut name = String::new();
    if t.len() >= 2 {
        let last = &t[t.len() - 1];
        let before = &t[t.len() - 2];
        if is_ident(last)
            && !C_TYPE_WORDS.contains(&last.as_str())
            && !matches!(before.as_str(), "struct" | "union" | "enum")
        {
            name = last.clone();
            t.pop();
        }
    }
    for _ in 0..arrays {
        t.push("*".into());
    }
    (t, name)
}

fn c_storage_free(tokens: &[String]) -> Vec<String> {
    tokens
        .iter()
        .filter(|t| {
            !matches!(
                t.as_str(),
                "static" | "inline" | "extern" | "__inline" | "__inline__"
            )
        })
        .cloned()
        .collect()
}

pub fn c_ty(tokens: &[String]) -> Ty {
    let t: Vec<&str> = tokens
        .iter()
        .map(String::as_str)
        .filter(|t| !matches!(*t, "const" | "volatile" | "restrict"))
        .collect();
    let joined = t.join(" ");
    let stars = t.iter().filter(|x| **x == "*").count();
    let base: Vec<&str> = t.iter().copied().filter(|x| *x != "*").collect();
    let is_char = base == ["char"];
    match (stars, joined.as_str()) {
        (1, _) if is_char => Ty::Str,
        (2, _) if is_char => Ty::StrList,
        (0, "char") => Ty::Char,
        (0, "void") => Ty::Void,
        (0, "double" | "float" | "long double") => Ty::Float,
        (0, "bool" | "_Bool") => Ty::Bool,
        (0, _) => {
            let ints = [
                "int",
                "long",
                "short",
                "unsigned",
                "signed",
                "char",
                "size_t",
                "ssize_t",
                "ptrdiff_t",
                "intptr_t",
                "uintptr_t",
                "int8_t",
                "int16_t",
                "int32_t",
                "int64_t",
                "uint8_t",
                "uint16_t",
                "uint32_t",
                "uint64_t",
            ];
            if !base.is_empty() && base.iter().all(|w| ints.contains(w)) {
                let unsigned = base.contains(&"unsigned")
                    || base.iter().any(|w| w.starts_with("uint") || *w == "size_t");
                Ty::Int { unsigned }
            } else {
                Ty::Other(joined)
            }
        }
        _ => Ty::Other(joined),
    }
}

/// Canonical C prototype text: `size_t str_len(const char *)`.
pub fn c_norm(ret: &[String], name: &str, params: &[Vec<String>]) -> String {
    let join = |t: &[String]| -> String {
        let mut s = String::new();
        for (i, x) in t.iter().enumerate() {
            if i > 0 && !(x == "*" && t[i - 1] == "*") {
                s.push(' ');
            }
            s.push_str(x);
        }
        s.replace("* ", "*")
    };
    let ps: Vec<String> = if params.is_empty() || (params.len() == 1 && params[0] == ["void"]) {
        vec!["void".into()]
    } else {
        params.iter().map(|p| join(p)).collect()
    };
    let r = join(ret);
    let sep = if r.ends_with('*') { "" } else { " " };
    format!("{r}{sep}{name}({})", ps.join(", "))
}

pub fn split_top(tokens: &[String], sep: &str) -> Vec<Vec<String>> {
    let mut out = vec![Vec::new()];
    let mut depth = 0i32;
    for t in tokens {
        match t.as_str() {
            "(" | "[" | "{" | "<" => depth += 1,
            ")" | "]" | "}" | ">" => depth -= 1,
            _ => {}
        }
        if depth == 0 && t == sep {
            out.push(Vec::new());
        } else if let Some(last) = out.last_mut() {
            last.push(t.clone());
        }
    }
    if out.len() == 1 && out[0].is_empty() {
        out.clear();
    }
    out
}

/// Parses C tokens `ret name ( params )` starting from a known name position.
pub fn c_from_tokens(ret: &[String], name: &str, inside: &[String]) -> Proto {
    let ret = c_storage_free(ret);
    let mut params = Vec::new();
    let mut types = Vec::new();
    for (i, p) in split_top(inside, ",").iter().enumerate() {
        if p.len() == 1 && p[0] == "void" {
            continue;
        }
        let (ty_tokens, pname) = c_param_type(p);
        params.push(Param {
            name: if pname.is_empty() {
                format!("a{i}")
            } else {
                pname
            },
            ty: c_ty(&ty_tokens),
            raw: ty_tokens.join(" "),
            has_default: false,
        });
        types.push(ty_tokens);
    }
    Proto {
        name: name.to_string(),
        norm: c_norm(&ret, name, &types),
        ret: c_ty(&ret),
        ret_raw: ret.join(" "),
        params,
        is_method: false,
        sig_text: String::new(),
    }
}

fn parse_c(sig: &str) -> Result<Proto, String> {
    let toks = c_tokens(sig.trim().trim_end_matches(';'));
    let open = toks
        .iter()
        .enumerate()
        .position(|(i, t)| t == "(" && i > 0 && is_ident(&toks[i - 1]))
        .ok_or_else(|| format!("`{sig}` is not a C function prototype"))?;
    let close = matching(&toks, open).ok_or_else(|| format!("unbalanced `(` in `{sig}`"))?;
    Ok(c_from_tokens(
        &toks[..open - 1],
        &toks[open - 1],
        &toks[open + 1..close],
    ))
}

pub fn matching(toks: &[String], open: usize) -> Option<usize> {
    let (o, c) = match toks[open].as_str() {
        "(" => ("(", ")"),
        "[" => ("[", "]"),
        "{" => ("{", "}"),
        "<" => ("<", ">"),
        _ => return None,
    };
    let mut depth = 0;
    for (i, t) in toks.iter().enumerate().skip(open) {
        if t == o {
            depth += 1;
        } else if t == c {
            depth -= 1;
            if depth == 0 {
                return Some(i);
            }
        }
    }
    None
}

// -------------------------------------------------------------------- Python

pub fn py_ty(ann: &str) -> Ty {
    let a: String = ann.chars().filter(|c| !c.is_whitespace()).collect();
    if let Some(inner) = a.strip_suffix("|None").or_else(|| a.strip_prefix("None|")) {
        return Ty::Opt(Box::new(py_ty(inner)));
    }
    let generic = |prefixes: &[&str]| -> Option<String> {
        prefixes.iter().find_map(|p| {
            a.strip_prefix(&format!("{p}["))
                .and_then(|r| r.strip_suffix(']'))
                .map(str::to_string)
        })
    };
    if let Some(inner) = generic(&["Optional", "typing.Optional"]) {
        return Ty::Opt(Box::new(py_ty(&inner)));
    }
    if let Some(inner) = generic(&[
        "list",
        "List",
        "Sequence",
        "Iterable",
        "typing.List",
        "Iterator",
        "Collection",
    ]) {
        return Ty::List(Box::new(py_ty(&inner)));
    }
    match a.as_str() {
        "int" => Ty::Int { unsigned: false },
        "float" => Ty::Float,
        "bool" => Ty::Bool,
        "str" => Ty::Str,
        "None" => Ty::Void,
        "" => Ty::Other("untyped".into()),
        other => Ty::Other(other.to_string()),
    }
}

fn split_depth(s: &str) -> Vec<String> {
    let mut out = Vec::new();
    let mut cur = String::new();
    let mut depth = 0i32;
    let mut quote: Option<char> = None;
    for c in s.chars() {
        match (quote, c) {
            (Some(q), c) if c == q => {
                quote = None;
                cur.push(c);
            }
            (Some(_), c) => cur.push(c),
            (None, '"' | '\'') => {
                quote = Some(c);
                cur.push(c);
            }
            (None, '(' | '[' | '{' | '<') => {
                depth += 1;
                cur.push(c);
            }
            (None, ')' | ']' | '}' | '>') => {
                depth -= 1;
                cur.push(c);
            }
            (None, ',') if depth == 0 => out.push(std::mem::take(&mut cur)),
            (None, c) => cur.push(c),
        }
    }
    if !cur.trim().is_empty() {
        out.push(cur);
    }
    out.into_iter()
        .map(|s| s.trim().to_string())
        .filter(|s| !s.is_empty())
        .collect()
}

/// Splits `name(params) -> ret` after the keyword; `params` must be balanced.
fn name_params_ret(s: &str) -> Result<(String, String, String), String> {
    let open = s.find('(').ok_or("missing `(`")?;
    let name = s[..open].trim().to_string();
    let mut depth = 0;
    let mut close = None;
    for (i, c) in s.char_indices().skip(open) {
        match c {
            '(' => depth += 1,
            ')' => {
                depth -= 1;
                if depth == 0 {
                    close = Some(i);
                    break;
                }
            }
            _ => {}
        }
    }
    let close = close.ok_or("unbalanced `(`")?;
    Ok((
        name,
        s[open + 1..close].to_string(),
        s[close + 1..].trim().to_string(),
    ))
}

pub fn py_from_parts(name: &str, params: &str, ret: &str) -> Proto {
    let mut ps = Vec::new();
    for raw in split_depth(params) {
        if raw == "/" || raw == "*" || raw.starts_with('*') {
            continue;
        }
        let (head, default) = match raw.split_once('=') {
            Some((h, _)) => (h.trim(), true),
            None => (raw.as_str(), false),
        };
        let (pname, ann) = match head.split_once(':') {
            Some((n, a)) => (n.trim(), a.trim()),
            None => (head.trim(), ""),
        };
        if pname == "self" || pname == "cls" {
            continue;
        }
        ps.push(Param {
            name: pname.to_string(),
            ty: py_ty(ann),
            raw: ann.to_string(),
            has_default: default,
        });
    }
    let ret = ret
        .trim_start_matches("->")
        .trim()
        .trim_end_matches(':')
        .trim();
    let required = ps.iter().filter(|p| !p.has_default).count();
    Proto {
        name: name.to_string(),
        norm: format!("{name}: {} parameter(s), {required} required", ps.len()),
        ret: py_ty(ret),
        ret_raw: ret.to_string(),
        params: ps,
        is_method: false,
        sig_text: String::new(),
    }
}

fn parse_py(sig: &str) -> Result<Proto, String> {
    let s = sig.trim();
    let s = s.strip_prefix("async ").unwrap_or(s).trim();
    let s = s
        .strip_prefix("def ")
        .ok_or_else(|| format!("`{sig}` must start with `def`"))?;
    let (name, params, ret) = name_params_ret(s)?;
    Ok(py_from_parts(&name, &params, &ret))
}

// ---------------------------------------------------------------------- Rust

pub fn rust_ty(t: &str) -> Ty {
    let t = strip_lifetimes(t);
    let t = t.trim();
    let t = t
        .strip_prefix("&mut")
        .or_else(|| t.strip_prefix('&'))
        .unwrap_or(t)
        .trim();
    let inner = |p: &str| -> Option<String> {
        t.strip_prefix(p)
            .and_then(|r| r.strip_prefix('<'))
            .and_then(|r| r.strip_suffix('>'))
            .map(str::to_string)
    };
    if let Some(i) = inner("Option") {
        return Ty::Opt(Box::new(rust_ty(&i)));
    }
    if let Some(i) = inner("Vec") {
        return Ty::List(Box::new(rust_ty(&i)));
    }
    if let Some(i) = t.strip_prefix('[').and_then(|r| r.strip_suffix(']'))
        && !i.contains(';')
    {
        return Ty::List(Box::new(rust_ty(i)));
    }
    match t {
        "i8" | "i16" | "i32" | "i64" | "i128" | "isize" => Ty::Int { unsigned: false },
        "u8" | "u16" | "u32" | "u64" | "u128" | "usize" => Ty::Int { unsigned: true },
        "f32" | "f64" => Ty::Float,
        "bool" => Ty::Bool,
        "char" => Ty::Char,
        "str" | "String" => Ty::Str,
        "()" | "" => Ty::Void,
        other => Ty::Other(other.to_string()),
    }
}

pub fn strip_lifetimes(t: &str) -> String {
    let mut out = String::new();
    let cs: Vec<char> = t.chars().collect();
    let mut i = 0;
    while i < cs.len() {
        if cs[i] == '\'' {
            i += 1;
            while i < cs.len() && (cs[i].is_alphanumeric() || cs[i] == '_') {
                i += 1;
            }
            while i < cs.len() && (cs[i] == ' ' || cs[i] == ',') {
                i += 1;
            }
            continue;
        }
        out.push(cs[i]);
        i += 1;
    }
    out.replace("<>", "")
}

fn squash(s: &str) -> String {
    strip_lifetimes(s)
        .chars()
        .filter(|c| !c.is_whitespace())
        .collect()
}

pub fn rust_from_parts(name: &str, params: &str, ret: &str) -> Proto {
    let mut ps = Vec::new();
    let mut types = Vec::new();
    let mut is_method = false;
    for raw in split_depth(params) {
        let (pat, ty) = match raw.split_once(':') {
            Some((p, t)) => (p.trim().to_string(), t.trim().to_string()),
            None => {
                is_method = true;
                types.push(squash(&raw));
                continue;
            }
        };
        let pname = pat.trim_start_matches("mut ").trim().to_string();
        types.push(squash(&ty));
        ps.push(Param {
            name: pname,
            ty: rust_ty(&ty),
            raw: ty.clone(),
            has_default: false,
        });
    }
    let ret = ret.trim();
    let ret = ret.strip_prefix("->").unwrap_or(ret);
    let ret = ret.split(" where ").next().unwrap_or(ret).trim();
    let ret_n = squash(ret);
    Proto {
        name: name.to_string(),
        norm: format!(
            "fn {name}({}){}",
            types.join(", "),
            if ret_n.is_empty() || ret_n == "()" {
                String::new()
            } else {
                format!(" -> {ret_n}")
            }
        ),
        ret: rust_ty(ret),
        ret_raw: ret.to_string(),
        params: ps,
        is_method,
        sig_text: String::new(),
    }
}

fn parse_rust(sig: &str) -> Result<Proto, String> {
    let s = sig
        .trim()
        .trim_end_matches(';')
        .trim_end_matches('{')
        .trim();
    let at = s
        .find("fn ")
        .ok_or_else(|| format!("`{sig}` must contain `fn name(...)`"))?;
    let rest = &s[at + 3..];
    let name_end = rest
        .find(|c: char| !(c.is_alphanumeric() || c == '_'))
        .unwrap_or(rest.len());
    let name = rest[..name_end].to_string();
    let mut tail = rest[name_end..].trim_start();
    if tail.starts_with('<') {
        let mut depth = 0;
        let mut end = 0;
        for (i, c) in tail.char_indices() {
            match c {
                '<' => depth += 1,
                '>' => {
                    depth -= 1;
                    if depth == 0 {
                        end = i + 1;
                        break;
                    }
                }
                _ => {}
            }
        }
        tail = &tail[end..];
    }
    let (_, params, ret) = name_params_ret(&format!("{name}{tail}"))?;
    Ok(rust_from_parts(&name, &params, &ret))
}

// ------------------------------------------------------------------- generic

fn parse_generic(sig: &str) -> Result<Proto, String> {
    let re = regex::Regex::new(r"([A-Za-z_][A-Za-z0-9_]*)\s*\(").map_err(|e| e.to_string())?;
    let name = re
        .captures(sig)
        .map(|c| c[1].to_string())
        .ok_or_else(|| format!("cannot find a function name in `{sig}`"))?;
    Ok(Proto {
        norm: name.clone(),
        name,
        params: Vec::new(),
        ret: Ty::Other("unknown".into()),
        ret_raw: String::new(),
        is_method: false,
        sig_text: String::new(),
    })
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn c_signatures_ignore_names_and_spacing() {
        let a = parse("c", "size_t str_len(const char *s)", "str_len").unwrap();
        let b = parse("c", "size_t   str_len ( const char*str );", "str_len").unwrap();
        assert_eq!(a.norm, b.norm);
        assert_eq!(a.norm, "size_t str_len(const char *)");
        assert_eq!(a.params[0].ty, Ty::Str);
        let s = parse(
            "c",
            "char **str_split(const char *s, char sep)",
            "str_split",
        )
        .unwrap();
        assert_eq!(s.norm, "char **str_split(const char *, char)");
        assert_eq!(s.ret, Ty::StrList);
        assert_eq!(s.params[1].ty, Ty::Char);
        let v = parse("c", "int f(void)", "f").unwrap();
        assert_eq!(v.norm, "int f(void)");
        assert!(v.params.is_empty());
        let arr = parse("c", "int sum(int v[], size_t n)", "sum").unwrap();
        assert_eq!(arr.norm, "int sum(int *, size_t)");
    }

    #[test]
    fn python_and_rust() {
        let p = parse(
            "python",
            "def top(entries: Iterable[str], n: int = 3) -> list[str]",
            "logstat.top",
        )
        .unwrap();
        assert_eq!(p.params.len(), 2);
        assert_eq!(p.params[0].ty, Ty::List(Box::new(Ty::Str)));
        assert!(p.params[1].has_default);
        let r = parse(
            "rust",
            "pub fn longest<'a>(text: &'a str) -> Option<&'a str>",
            "longest",
        )
        .unwrap();
        assert_eq!(r.norm, "fn longest(&str) -> Option<&str>");
        assert_eq!(r.ret, Ty::Opt(Box::new(Ty::Str)));
        assert!(parse("rust", "fn a(x: u8)", "b").is_err());
    }
}
