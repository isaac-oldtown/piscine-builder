//! `target: program`: running the student's whole program, and the one-line
//! case grammar
//!   [args...] [@file="content"] [<<< "stdin"] => out == "..." err~"regex" exit 0

use crate::expr::{self, EvalError};
use crate::run;
use crate::value::Value;
use regex::Regex;
use std::collections::BTreeMap;
use std::path::PathBuf;
use std::process::Command;
use std::time::Duration;

pub struct Program {
    pub cmd: Vec<String>,
    pub cwd: PathBuf,
    pub env: BTreeMap<String, String>,
    pub timeout: Duration,
}

pub struct RunResult {
    pub out: String,
    pub err: String,
    pub exit: Option<i32>,
    pub status: String,
    pub timed_out: bool,
}

impl RunResult {
    pub fn to_value(&self) -> Value {
        Value::Map(vec![
            (Value::Str("out".into()), Value::Str(self.out.clone())),
            (Value::Str("err".into()), Value::Str(self.err.clone())),
            (
                Value::Str("exit".into()),
                self.exit.map_or(Value::Null, |c| Value::Int(c as i128)),
            ),
            (Value::Str("status".into()), Value::Str(self.status.clone())),
        ])
    }
}

impl Program {
    pub fn run(
        &self,
        args: &[String],
        stdin: Option<&str>,
        files: &[(String, String)],
    ) -> Result<RunResult, String> {
        let mut created = Vec::new();
        for (name, body) in files {
            let p = self.cwd.join(name);
            if p.exists() {
                return Err(format!(
                    "input file `{name}` would overwrite a file of the workspace"
                ));
            }
            if let Some(dir) = p.parent() {
                let _ = std::fs::create_dir_all(dir);
            }
            std::fs::write(&p, body).map_err(|e| format!("{name}: {e}"))?;
            created.push(p);
        }
        let (prog, base) = self.cmd.split_first().ok_or("empty run command")?;
        let mut c = Command::new(prog);
        c.args(base)
            .args(args)
            .current_dir(&self.cwd)
            .envs(&self.env);
        let out = run::run(&mut c, Some(stdin.unwrap_or("").as_bytes()), self.timeout);
        for p in created {
            let _ = std::fs::remove_file(p);
        }
        let out = out?;
        Ok(RunResult {
            out: out.stdout.replace("\r\n", "\n"),
            err: out.stderr.replace("\r\n", "\n"),
            exit: if out.timed_out { None } else { out.code },
            status: out.status_text,
            timed_out: out.timed_out,
        })
    }

    /// `run(args=[...], stdin="...")` from the expression language.
    pub fn call(&self, args: Vec<(Option<String>, Value)>) -> Result<Value, EvalError> {
        let mut argv: Vec<String> = Vec::new();
        let mut stdin = String::new();
        for (i, (name, v)) in args.into_iter().enumerate() {
            match (name.as_deref(), i) {
                (Some("args") | None, 0) | (Some("args"), _) => {
                    argv = match v {
                        Value::List(l) => l
                            .iter()
                            .map(|x| x.text().unwrap_or_else(|| x.to_string()))
                            .collect(),
                        Value::Str(s) => run::split_words(&s),
                        other => {
                            return Err(EvalError::Spec(format!(
                                "run: args must be a list, got {}",
                                other.type_name()
                            )));
                        }
                    }
                }
                (Some("stdin"), _) | (None, 1) => stdin = v.text().unwrap_or_else(|| v.to_string()),
                (Some(other), _) => {
                    return Err(EvalError::Spec(format!("run: unknown argument `{other}`")));
                }
                (None, _) => {
                    return Err(EvalError::Spec(
                        "run(args, stdin) takes at most 2 arguments".into(),
                    ));
                }
            }
        }
        let r = self
            .run(&argv, Some(&stdin), &[])
            .map_err(EvalError::Spec)?;
        if r.timed_out {
            return Err(EvalError::Student(format!(
                "program timed out after {} ms",
                self.timeout.as_millis()
            )));
        }
        Ok(r.to_value())
    }
}

#[derive(Clone, Copy, Debug, PartialEq)]
pub enum Stream {
    Out,
    Err,
}

#[derive(Clone, Debug)]
pub enum Assert {
    Exit(i32),
    Eq(Stream, String),
    Ne(Stream, String),
    Re(Stream, String),
    NotRe(Stream, String),
    Empty(Stream),
}

#[derive(Clone, Debug)]
pub struct Case {
    pub args: Vec<String>,
    pub stdin: Option<String>,
    pub files: Vec<(String, String)>,
    pub asserts: Vec<Assert>,
}

fn find_arrow(line: &str) -> Option<usize> {
    let b = line.as_bytes();
    let mut quote = false;
    let mut i = 0;
    while i + 1 < b.len() {
        match b[i] {
            b'\\' if quote => i += 1,
            b'"' => quote = !quote,
            b'=' if !quote && b[i + 1] == b'>' => return Some(i),
            _ => {}
        }
        i += 1;
    }
    None
}

/// Words of the left side; "quoted" words use the expression escapes.
fn words(s: &str) -> Result<Vec<String>, String> {
    let cs: Vec<char> = s.chars().collect();
    let mut out = Vec::new();
    let mut i = 0;
    while i < cs.len() {
        if cs[i].is_whitespace() {
            i += 1;
            continue;
        }
        let mut w = String::new();
        while i < cs.len() && !cs[i].is_whitespace() {
            if cs[i] == '"' {
                w.push_str(&expr::read_string(&cs, &mut i)?);
            } else if cs[i] == '\'' {
                i += 1;
                while i < cs.len() && cs[i] != '\'' {
                    w.push(cs[i]);
                    i += 1;
                }
                i += 1;
            } else {
                w.push(cs[i]);
                i += 1;
            }
        }
        out.push(w);
    }
    Ok(out)
}

pub fn parse_case(line: &str) -> Result<Case, String> {
    let arrow = find_arrow(line).ok_or("a program case needs `=>` followed by checks")?;
    let (left, right) = (&line[..arrow], &line[arrow + 2..]);
    let mut c = Case {
        args: Vec::new(),
        stdin: None,
        files: Vec::new(),
        asserts: Vec::new(),
    };
    // `<<<` and `@name=` must be recognised before quote removal.
    let cs: Vec<char> = left.chars().collect();
    let mut i = 0;
    let mut rest = String::new();
    while i < cs.len() {
        if cs[i..].starts_with(&['<', '<', '<']) {
            i += 3;
            while i < cs.len() && cs[i].is_whitespace() {
                i += 1;
            }
            if cs.get(i) != Some(&'"') {
                return Err("`<<<` must be followed by a \"quoted string\"".into());
            }
            c.stdin = Some(expr::read_string(&cs, &mut i)?);
        } else if cs[i] == '@' && (i == 0 || cs[i - 1].is_whitespace()) {
            let start = i + 1;
            while i < cs.len() && cs[i] != '=' {
                i += 1;
            }
            let name: String = cs[start..i].iter().collect();
            i += 1;
            if cs.get(i) != Some(&'"') {
                return Err(format!("@{name}= must be followed by a \"quoted string\""));
            }
            c.files.push((name, expr::read_string(&cs, &mut i)?));
        } else {
            rest.push(cs[i]);
            i += 1;
        }
    }
    c.args = words(&rest)?;
    let rc: Vec<char> = right.chars().collect();
    let mut i = 0;
    let skip_ws = |i: &mut usize| {
        while *i < rc.len() && (rc[*i].is_whitespace() || rc[*i] == ',') {
            *i += 1;
        }
    };
    loop {
        skip_ws(&mut i);
        if i >= rc.len() {
            break;
        }
        let word: String = rc[i..].iter().take_while(|c| c.is_alphabetic()).collect();
        i += word.chars().count();
        let stream = match word.as_str() {
            "exit" => {
                skip_ws(&mut i);
                let num: String = rc[i..]
                    .iter()
                    .take_while(|c| c.is_ascii_digit() || **c == '-')
                    .collect();
                i += num.chars().count();
                c.asserts.push(Assert::Exit(
                    num.parse().map_err(|_| "exit needs a number")?,
                ));
                continue;
            }
            "out" | "stdout" => Stream::Out,
            "err" | "stderr" => Stream::Err,
            other => return Err(format!("unknown check `{other}` (use out, err or exit)")),
        };
        skip_ws(&mut i);
        let op: String = rc[i..]
            .iter()
            .take_while(|c| matches!(c, '=' | '!' | '~'))
            .collect();
        i += op.chars().count();
        skip_ws(&mut i);
        if op.is_empty() {
            let w: String = rc[i..].iter().take_while(|c| c.is_alphabetic()).collect();
            if w == "empty" {
                i += 5;
                c.asserts.push(Assert::Empty(stream));
                continue;
            }
            return Err("expected ==, !=, ~ or !~ after out/err".into());
        }
        if rc.get(i) != Some(&'"') {
            return Err(format!("`{op}` must be followed by a \"quoted string\""));
        }
        let s = expr::read_string(&rc, &mut i)?;
        c.asserts.push(match op.as_str() {
            "==" => Assert::Eq(stream, s),
            "!=" => Assert::Ne(stream, s),
            "~" => {
                Regex::new(&s).map_err(|e| format!("bad regex: {e}"))?;
                Assert::Re(stream, s)
            }
            "!~" => {
                Regex::new(&s).map_err(|e| format!("bad regex: {e}"))?;
                Assert::NotRe(stream, s)
            }
            other => return Err(format!("unknown operator `{other}`")),
        });
    }
    if c.asserts.is_empty() {
        return Err("a program case needs at least one check after `=>`".into());
    }
    Ok(c)
}

fn show(s: &str) -> String {
    Value::Str(s.to_string()).short(100)
}

/// `None` = pass, `Some(reason)` = fail.
pub fn judge(c: &Case, r: &RunResult) -> Option<String> {
    if r.timed_out {
        return Some(r.status.clone());
    }
    for a in &c.asserts {
        let text = |s: &Stream| if *s == Stream::Out { &r.out } else { &r.err };
        let label = |s: &Stream| {
            if *s == Stream::Out {
                "stdout"
            } else {
                "stderr"
            }
        };
        let fail = match a {
            Assert::Exit(code) => (r.exit != Some(*code)).then(|| {
                let base = match r.exit {
                    Some(got) => format!("exit code: expected {code}, got {got}"),
                    None => format!("exit code: expected {code}, {}", r.status),
                };
                // A wrapper (valgrind, sanitizers) explains itself on stderr.
                match r.err.lines().rev().map(str::trim).find(|l| !l.is_empty()) {
                    Some(last) => format!("{base}; stderr: {}", crate::report::truncate(last, 120)),
                    None => base,
                }
            }),
            Assert::Eq(s, want) => (text(s) != want).then(|| {
                format!(
                    "{}: expected {}, got {}",
                    label(s),
                    show(want),
                    show(text(s))
                )
            }),
            Assert::Ne(s, bad) => {
                (text(s) == bad).then(|| format!("{}: must not be {}", label(s), show(bad)))
            }
            Assert::Re(s, re) => {
                let ok = Regex::new(re).is_ok_and(|x| x.is_match(text(s)));
                (!ok).then(|| format!("{}: nothing matches /{re}/ in {}", label(s), show(text(s))))
            }
            Assert::NotRe(s, re) => {
                let bad = Regex::new(re).is_ok_and(|x| x.is_match(text(s)));
                bad.then(|| format!("{}: /{re}/ must not match {}", label(s), show(text(s))))
            }
            Assert::Empty(s) => (!text(s).is_empty())
                .then(|| format!("{}: expected nothing, got {}", label(s), show(text(s)))),
        };
        if fail.is_some() {
            return fail;
        }
    }
    None
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn case_grammar() {
        let c = parse_case(r#"-l "two words" @in.txt="a\nb\n" <<< "x\n" => out == "2 in.txt\n" err ~ "warn" exit 0"#).unwrap();
        assert_eq!(c.args, vec!["-l", "two words"]);
        assert_eq!(c.stdin.as_deref(), Some("x\n"));
        assert_eq!(c.files, vec![("in.txt".to_string(), "a\nb\n".to_string())]);
        assert_eq!(c.asserts.len(), 3);
        let c = parse_case(r#"nope.log => err~"(?i)no such" exit 2"#).unwrap();
        assert!(matches!(c.asserts[0], Assert::Re(Stream::Err, _)));
        assert!(matches!(c.asserts[1], Assert::Exit(2)));
        assert!(parse_case("a b").is_err());
        assert!(parse_case(r#"=> out == "x" bogus"#).is_err());
        let c = parse_case(r#"=> out empty, exit 1"#).unwrap();
        assert!(matches!(c.asserts[0], Assert::Empty(Stream::Out)));
    }
}
