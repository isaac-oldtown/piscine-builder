//! Dynamic values: what harnesses return, what generators produce, what the
//! expression language computes with. Also the line-based wire format that
//! every language harness speaks.

use std::cmp::Ordering;
use std::fmt::{self, Write as _};

#[derive(Clone, Debug)]
pub enum Value {
    Null,
    Bool(bool),
    Int(i128),
    Float(f64),
    Str(String),
    Char(char),
    List(Vec<Value>),
    Map(Vec<(Value, Value)>),
    /// A value the harness could only describe (e.g. a Python object repr).
    Opaque(String),
}

impl Value {
    pub fn type_name(&self) -> &'static str {
        match self {
            Value::Null => "null",
            Value::Bool(_) => "bool",
            Value::Int(_) => "int",
            Value::Float(_) => "float",
            Value::Str(_) => "string",
            Value::Char(_) => "char",
            Value::List(_) => "list",
            Value::Map(_) => "map",
            Value::Opaque(_) => "object",
        }
    }

    pub fn truthy(&self) -> bool {
        match self {
            Value::Null => false,
            Value::Bool(b) => *b,
            Value::Int(i) => *i != 0,
            Value::Float(f) => *f != 0.0,
            Value::Str(s) => !s.is_empty(),
            Value::Char(_) | Value::Opaque(_) => true,
            Value::List(l) => !l.is_empty(),
            Value::Map(m) => !m.is_empty(),
        }
    }

    pub fn as_f64(&self) -> Option<f64> {
        match self {
            Value::Int(i) => Some(*i as f64),
            Value::Float(f) => Some(*f),
            _ => None,
        }
    }

    /// Text content of strings, chars and opaque reprs.
    pub fn text(&self) -> Option<String> {
        match self {
            Value::Str(s) | Value::Opaque(s) => Some(s.clone()),
            Value::Char(c) => Some(c.to_string()),
            _ => None,
        }
    }

    fn rank(&self) -> u8 {
        match self {
            Value::Null => 0,
            Value::Bool(_) => 1,
            Value::Int(_) | Value::Float(_) => 2,
            Value::Char(_) | Value::Str(_) | Value::Opaque(_) => 3,
            Value::List(_) => 4,
            Value::Map(_) => 5,
        }
    }

    /// Total order used by `sorted`, `min`, `max` and `<`.
    pub fn cmp_values(&self, other: &Value) -> Ordering {
        match (self, other) {
            (Value::Int(a), Value::Int(b)) => a.cmp(b),
            (Value::Bool(a), Value::Bool(b)) => a.cmp(b),
            (Value::List(a), Value::List(b)) => {
                for (x, y) in a.iter().zip(b) {
                    let o = x.cmp_values(y);
                    if o != Ordering::Equal {
                        return o;
                    }
                }
                a.len().cmp(&b.len())
            }
            _ => {
                if let (Some(a), Some(b)) = (self.as_f64(), other.as_f64()) {
                    return a.partial_cmp(&b).unwrap_or(Ordering::Equal);
                }
                if let (Some(a), Some(b)) = (self.text(), other.text()) {
                    return a.cmp(&b);
                }
                self.rank().cmp(&other.rank())
            }
        }
    }

    /// Display form, cut to roughly `max` characters.
    pub fn short(&self, max: usize) -> String {
        let s = self.to_string();
        if s.chars().count() <= max {
            s
        } else {
            let cut: String = s.chars().take(max).collect();
            format!("{cut}...")
        }
    }
}

fn float_eq(a: f64, b: f64) -> bool {
    if a.is_nan() && b.is_nan() {
        return true;
    }
    if a == b {
        return true;
    }
    let scale = a.abs().max(b.abs()).max(1.0);
    (a - b).abs() <= 1e-9 * scale
}

impl PartialEq for Value {
    fn eq(&self, other: &Value) -> bool {
        match (self, other) {
            (Value::Null, Value::Null) => true,
            (Value::Bool(a), Value::Bool(b)) => a == b,
            (Value::Int(a), Value::Int(b)) => a == b,
            (Value::Float(a), Value::Float(b)) => float_eq(*a, *b),
            (Value::Int(a), Value::Float(b)) | (Value::Float(b), Value::Int(a)) => {
                float_eq(*a as f64, *b)
            }
            (Value::Str(a), Value::Str(b)) => a == b,
            (Value::Char(a), Value::Char(b)) => a == b,
            (Value::Char(c), Value::Str(s)) | (Value::Str(s), Value::Char(c)) => {
                let mut it = s.chars();
                it.next() == Some(*c) && it.next().is_none()
            }
            (Value::Opaque(a), Value::Opaque(b))
            | (Value::Opaque(a), Value::Str(b))
            | (Value::Str(b), Value::Opaque(a)) => a == b,
            (Value::List(a), Value::List(b)) => a == b,
            (Value::Map(a), Value::Map(b)) => {
                a.len() == b.len()
                    && a.iter()
                        .all(|(k, v)| b.iter().any(|(k2, v2)| k == k2 && v == v2))
            }
            _ => false,
        }
    }
}

impl fmt::Display for Value {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Value::Null => f.write_str("null"),
            Value::Bool(b) => write!(f, "{b}"),
            Value::Int(i) => write!(f, "{i}"),
            Value::Float(x) => {
                if x.is_finite() && x.fract() == 0.0 && x.abs() < 1e15 {
                    write!(f, "{x:.1}")
                } else {
                    write!(f, "{x}")
                }
            }
            Value::Str(s) => write!(f, "{s:?}"),
            Value::Char(c) => write!(f, "{c:?}"),
            Value::Opaque(s) => f.write_str(s),
            Value::List(l) => {
                f.write_str("[")?;
                for (i, v) in l.iter().enumerate() {
                    if i > 0 {
                        f.write_str(", ")?;
                    }
                    write!(f, "{v}")?;
                }
                f.write_str("]")
            }
            Value::Map(m) => {
                f.write_str("{")?;
                for (i, (k, v)) in m.iter().enumerate() {
                    if i > 0 {
                        f.write_str(", ")?;
                    }
                    write!(f, "{k}: {v}")?;
                }
                f.write_str("}")
            }
        }
    }
}

// ------------------------------------------------------------------ wire format
//
// One value is a sequence of space-separated tokens:
//   n            null            b0 / b1      bool
//   i<int>       integer         f<float>     float (Rust `{:e}` / C `%.17g`)
//   s<hex>       UTF-8 string    c<code>      char (Unicode scalar)
//   l<n> ...     list of n values that follow
//   m<n> ...     map: n key/value pairs that follow
//   o<hex>       opaque repr
// Requests:  `call <name> <nargs> <values...>`
// Responses: `ok <value>` | `exc <hex type> <hex message>` | `err <hex message>`

pub fn hex(bytes: &[u8], out: &mut String) {
    for b in bytes {
        let _ = write!(out, "{b:02x}");
    }
}

pub fn unhex(s: &str) -> Result<Vec<u8>, String> {
    if !s.len().is_multiple_of(2) {
        return Err(format!("odd hex length in `{s}`"));
    }
    (0..s.len())
        .step_by(2)
        .map(|i| u8::from_str_radix(&s[i..i + 2], 16).map_err(|e| format!("bad hex: {e}")))
        .collect()
}

pub fn unhex_text(s: &str) -> String {
    unhex(s)
        .map(|b| String::from_utf8_lossy(&b).into_owned())
        .unwrap_or_else(|_| s.to_string())
}

pub fn encode(v: &Value) -> String {
    let mut s = String::new();
    enc(v, &mut s);
    s
}

fn enc(v: &Value, out: &mut String) {
    match v {
        Value::Null => out.push('n'),
        Value::Bool(b) => out.push_str(if *b { "b1" } else { "b0" }),
        Value::Int(i) => {
            let _ = write!(out, "i{i}");
        }
        Value::Float(f) => {
            let _ = write!(out, "f{f:e}");
        }
        Value::Str(s) => {
            out.push('s');
            hex(s.as_bytes(), out);
        }
        Value::Char(c) => {
            let _ = write!(out, "c{}", *c as u32);
        }
        Value::Opaque(s) => {
            out.push('o');
            hex(s.as_bytes(), out);
        }
        Value::List(l) => {
            let _ = write!(out, "l{}", l.len());
            for x in l {
                out.push(' ');
                enc(x, out);
            }
        }
        Value::Map(m) => {
            let _ = write!(out, "m{}", m.len());
            for (k, x) in m {
                out.push(' ');
                enc(k, out);
                out.push(' ');
                enc(x, out);
            }
        }
    }
}

fn parse_float(s: &str) -> f64 {
    s.parse::<f64>().unwrap_or_else(|_| {
        let l = s.to_ascii_lowercase();
        if l.contains("nan") {
            f64::NAN
        } else if l.starts_with('-') {
            f64::NEG_INFINITY
        } else {
            f64::INFINITY
        }
    })
}

pub fn decode<'a>(it: &mut impl Iterator<Item = &'a str>) -> Result<Value, String> {
    let t = it.next().ok_or("truncated value")?;
    let (tag, rest) = t.split_at(1);
    Ok(match tag {
        "n" => Value::Null,
        "b" => Value::Bool(rest == "1"),
        "i" => match rest.parse::<i128>() {
            Ok(i) => Value::Int(i),
            Err(_) => Value::Opaque(rest.to_string()),
        },
        "f" => Value::Float(parse_float(rest)),
        "s" => Value::Str(String::from_utf8_lossy(&unhex(rest)?).into_owned()),
        "c" => Value::Char(
            rest.parse::<u32>()
                .ok()
                .and_then(char::from_u32)
                .unwrap_or('\u{fffd}'),
        ),
        "o" => Value::Opaque(String::from_utf8_lossy(&unhex(rest)?).into_owned()),
        "l" => {
            let n: usize = rest
                .parse()
                .map_err(|_| format!("bad list length `{rest}`"))?;
            let mut v = Vec::with_capacity(n.min(1 << 16));
            for _ in 0..n {
                v.push(decode(it)?);
            }
            Value::List(v)
        }
        "m" => {
            let n: usize = rest
                .parse()
                .map_err(|_| format!("bad map length `{rest}`"))?;
            let mut v = Vec::with_capacity(n.min(1 << 16));
            for _ in 0..n {
                let k = decode(it)?;
                v.push((k, decode(it)?));
            }
            Value::Map(v)
        }
        _ => return Err(format!("unknown value tag in `{t}`")),
    })
}

pub fn decode_str(s: &str) -> Result<Value, String> {
    let mut it = s.split_ascii_whitespace();
    let v = decode(&mut it)?;
    if it.next().is_some() {
        return Err("trailing tokens after value".into());
    }
    Ok(v)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn wire_round_trip() {
        let v = Value::List(vec![
            Value::Str("a b\n".into()),
            Value::Str(String::new()),
            Value::Null,
            Value::Int(-42),
            Value::Float(0.1),
            Value::Char(','),
            Value::Map(vec![(Value::Str("k".into()), Value::Bool(true))]),
        ]);
        assert_eq!(decode_str(&encode(&v)), Ok(v));
    }

    #[test]
    fn semantic_equality() {
        assert_eq!(Value::Char('a'), Value::Str("a".into()));
        assert_eq!(Value::Int(2), Value::Float(2.0));
        assert_eq!(Value::Float(0.1 + 0.2), Value::Float(0.3));
        let a = Value::Map(vec![
            (Value::Str("x".into()), Value::Int(1)),
            (Value::Str("y".into()), Value::Int(2)),
        ]);
        let b = Value::Map(vec![
            (Value::Str("y".into()), Value::Int(2)),
            (Value::Str("x".into()), Value::Int(1)),
        ]);
        assert_eq!(a, b);
        assert_ne!(Value::List(vec![]), Value::Null);
    }
}
