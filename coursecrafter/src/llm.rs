//! OpenAI-compatible chat-completions client. Works with Anthropic's
//! compatibility endpoint, OpenAI, Meridian (which fronts Claude Code's own
//! login) and any server that speaks `/chat/completions`.

use crate::config::Config;
use serde_json::{Value, json};
use std::cell::{Cell, RefCell};
use std::env;
use std::thread;
use std::time::{Duration, Instant};

pub const DEFAULT_MODEL: &str = "claude-sonnet-5-5";

#[derive(Clone)]
pub struct Msg {
    pub role: &'static str,
    pub content: String,
}

impl Msg {
    pub fn system(c: impl Into<String>) -> Msg {
        Msg {
            role: "system",
            content: c.into(),
        }
    }
    pub fn user(c: impl Into<String>) -> Msg {
        Msg {
            role: "user",
            content: c.into(),
        }
    }
    pub fn assistant(c: impl Into<String>) -> Msg {
        Msg {
            role: "assistant",
            content: c.into(),
        }
    }
}

pub struct Llm {
    pub base_url: String,
    pub model: String,
    key: Option<String>,
    agent: ureq::Agent,
    pub calls: Cell<u32>,
    pub prompt_tokens: Cell<u64>,
    pub completion_tokens: Cell<u64>,
    pub cached_tokens: Cell<u64>,
    /// Input tokens written to the provider's prompt cache (billed above the
    /// normal input price by Anthropic).
    pub cache_write_tokens: Cell<u64>,
    /// One record per `chat`, in order, failed ones included.
    pub history: RefCell<Vec<CallRecord>>,
}

/// What one `chat` cost: wall time (retries and back-off included), HTTP
/// attempts, the errors met on the way, and the tokens of the reply.
#[derive(Clone, Debug, Default)]
pub struct CallRecord {
    pub ms: u128,
    pub attempts: u32,
    pub errors: Vec<String>,
    pub ok: bool,
    pub usage: Usage,
}

/// Tokens of one reply. `input` counts every prompt token, cached or not.
#[derive(Clone, Copy, Debug, Default, PartialEq, Eq)]
pub struct Usage {
    pub input: u64,
    pub output: u64,
    pub cache_read: u64,
    pub cache_write: u64,
}

impl Usage {
    /// Reads the usage object of OpenAI-style replies (Meridian, OpenAI,
    /// Anthropic's compatibility endpoint) and of Anthropic's native one.
    /// OpenAI-style `prompt_tokens` already include cache reads and writes;
    /// Anthropic's `input_tokens` exclude them.
    pub fn parse(u: &Value) -> Usage {
        let n = |v: &Value| v.as_u64().unwrap_or(0);
        let d = &u["prompt_tokens_details"];
        let cache_read = n(&d["cached_tokens"]).max(n(&u["cache_read_input_tokens"]));
        let cache_write = n(&d["cache_write_tokens"])
            .max(n(&d["cache_creation_tokens"]))
            .max(n(&u["cache_creation_input_tokens"]));
        let input = if u["prompt_tokens"].is_u64() {
            n(&u["prompt_tokens"])
        } else {
            n(&u["input_tokens"]) + cache_read + cache_write
        };
        Usage {
            input,
            output: n(&u["completion_tokens"]).max(n(&u["output_tokens"])),
            cache_read,
            cache_write,
        }
    }

    /// Input tokens neither read from nor written to the cache.
    pub fn fresh(&self) -> u64 {
        self.input
            .saturating_sub(self.cache_read + self.cache_write)
    }
}

struct Preset {
    base_url: &'static str,
    key_envs: &'static [&'static str],
    default_model: Option<&'static str>,
    keyless_ok: bool,
}

fn preset(name: &str) -> Result<Preset, String> {
    Ok(match name {
        "anthropic" => Preset {
            base_url: "https://api.anthropic.com/v1",
            key_envs: &["ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN"],
            default_model: Some(DEFAULT_MODEL),
            keyless_ok: false,
        },
        "meridian" => Preset {
            base_url: "http://127.0.0.1:3456/v1",
            key_envs: &["MERIDIAN_API_KEY"],
            default_model: Some(DEFAULT_MODEL),
            keyless_ok: true,
        },
        "openai" => Preset {
            base_url: "https://api.openai.com/v1",
            key_envs: &["OPENAI_API_KEY"],
            default_model: None,
            keyless_ok: false,
        },
        "custom" => Preset {
            base_url: "",
            key_envs: &["OPENAI_API_KEY"],
            default_model: None,
            keyless_ok: false,
        },
        other => {
            return Err(format!(
                "unknown provider `{other}` (anthropic | meridian | openai)"
            ));
        }
    })
}

fn is_loopback(url: &str) -> bool {
    let host = url.split("://").nth(1).unwrap_or(url);
    ["127.0.0.1", "localhost", "[::1]"]
        .iter()
        .any(|h| host.starts_with(h))
}

fn env_get(k: &str) -> Option<String> {
    env::var(k).ok().filter(|s| !s.trim().is_empty())
}

/// What the command line asked for; `None` falls back to env, then config.
#[derive(Default)]
pub struct Choice {
    pub provider: Option<String>,
    pub base_url: Option<String>,
    pub model: Option<String>,
    pub key_env: Option<String>,
}

impl Llm {
    pub fn resolve(c: Choice, cfg: &Config) -> Result<Llm, String> {
        let base_override = c
            .base_url
            .or_else(|| env_get("PISCINEBUILDER_BASE_URL"))
            .or_else(|| cfg.base_url.clone());
        let provider = c
            .provider
            .or_else(|| env_get("PISCINEBUILDER_PROVIDER"))
            .or_else(|| cfg.provider.clone())
            .unwrap_or_else(|| {
                if base_override.is_some() {
                    "custom"
                } else {
                    "anthropic"
                }
                .to_string()
            });
        let p = preset(&provider)?;
        let base_url = base_override
            .unwrap_or_else(|| p.base_url.to_string())
            .trim_end_matches('/')
            .to_string();
        if base_url.is_empty() {
            return Err("provider `custom` needs --base-url (or PISCINEBUILDER_BASE_URL, or base_url in the config)".into());
        }
        let model = c
            .model
            .or_else(|| env_get("PISCINEBUILDER_MODEL"))
            .or_else(|| cfg.model.clone())
            .or_else(|| p.default_model.map(str::to_string))
            .ok_or_else(|| format!("provider `{provider}` has no default model; pass --model"))?;
        let key_env = c.key_env.or_else(|| cfg.api_key_env.clone());
        let key = match key_env {
            Some(name) => Some(
                env_get(&name).ok_or_else(|| format!("API key variable `{name}` is not set"))?,
            ),
            None => env_get("PISCINEBUILDER_API_KEY")
                .or_else(|| p.key_envs.iter().find_map(|k| env_get(k))),
        };
        if key.is_none() && !(p.keyless_ok || is_loopback(&base_url)) {
            return Err(format!(
                "no API key for {base_url}: set PISCINEBUILDER_API_KEY (or {}), or name a variable with --api-key-env",
                p.key_envs.join(" / ")
            ));
        }
        let agent: ureq::Agent = ureq::Agent::config_builder()
            .timeout_global(Some(Duration::from_secs(900)))
            .http_status_as_error(false)
            .build()
            .into();
        Ok(Llm {
            base_url,
            model,
            key,
            agent,
            calls: Cell::new(0),
            prompt_tokens: Cell::new(0),
            completion_tokens: Cell::new(0),
            cached_tokens: Cell::new(0),
            cache_write_tokens: Cell::new(0),
            history: RefCell::new(Vec::new()),
        })
    }

    /// One non-streaming completion; retries transient failures.
    pub fn chat(&self, messages: &[Msg]) -> Result<String, String> {
        let start = Instant::now();
        let mut rec = CallRecord::default();
        let res = self.chat_inner(messages, &mut rec);
        rec.ms = start.elapsed().as_millis();
        rec.ok = res.is_ok();
        if let Err(e) = &res
            && rec.errors.last() != Some(e)
        {
            rec.errors.push(e.clone());
        }
        self.history.borrow_mut().push(rec);
        res
    }

    fn chat_inner(&self, messages: &[Msg], rec: &mut CallRecord) -> Result<String, String> {
        let body = json!({
            "model": self.model,
            "stream": false,
            "max_tokens": 16000,
            "messages": messages.iter().map(|m| json!({"role": m.role, "content": m.content})).collect::<Vec<_>>(),
        })
        .to_string();
        let url = format!("{}/chat/completions", self.base_url);
        let mut last = String::new();
        for attempt in 0..4u64 {
            if attempt > 0 {
                rec.errors.push(last.clone());
                thread::sleep(Duration::from_secs(2 << attempt));
            }
            rec.attempts += 1;
            let mut req = self
                .agent
                .post(&url)
                .header("content-type", "application/json");
            if let Some(k) = &self.key {
                req = req.header("authorization", &format!("Bearer {k}"));
            }
            let mut resp = match req.send(body.as_str()) {
                Ok(r) => r,
                Err(e) => {
                    last = format!("request to {url} failed: {e}");
                    continue;
                }
            };
            let status = resp.status().as_u16();
            let text = resp
                .body_mut()
                .read_to_string()
                .map_err(|e| format!("reading response: {e}"))?;
            if status == 429 || status >= 500 {
                last = format!("HTTP {status}: {}", cut(&text, 300));
                continue;
            }
            if status >= 400 {
                return Err(format!("HTTP {status}: {}", cut(&text, 500)));
            }
            return self.extract(&text, rec);
        }
        Err(last)
    }

    fn extract(&self, text: &str, rec: &mut CallRecord) -> Result<String, String> {
        let v: Value = serde_json::from_str(text)
            .map_err(|e| format!("response is not JSON ({e}): {}", cut(text, 200)))?;
        // Counted even when the content turns out unusable: the tokens are billed.
        self.calls.set(self.calls.get() + 1);
        let u = Usage::parse(&v["usage"]);
        rec.usage = u;
        let add = |c: &Cell<u64>, n: u64| c.set(c.get() + n);
        add(&self.prompt_tokens, u.input);
        add(&self.completion_tokens, u.output);
        add(&self.cached_tokens, u.cache_read);
        add(&self.cache_write_tokens, u.cache_write);
        match &v["choices"][0]["message"]["content"] {
            Value::String(s) if !s.trim().is_empty() => Ok(s.clone()),
            Value::Array(parts) => {
                let s: String = parts.iter().filter_map(|p| p["text"].as_str()).collect();
                if s.trim().is_empty() {
                    Err("empty model response".into())
                } else {
                    Ok(s)
                }
            }
            _ => Err(format!("unexpected response shape: {}", cut(text, 300))),
        }
    }

    pub fn usage_line(&self) -> String {
        let (read, write) = (self.cached_tokens.get(), self.cache_write_tokens.get());
        let cache = match (read, write) {
            (0, 0) => String::new(),
            (r, 0) => format!(" ({r} cached)"),
            (0, w) => format!(" ({w} written to cache)"),
            (r, w) => format!(" ({r} cached, {w} written to cache)"),
        };
        format!(
            "{} call(s), {} input tokens{cache}, {} output tokens ({})",
            self.calls.get(),
            self.prompt_tokens.get(),
            self.completion_tokens.get(),
            self.model
        )
    }
}

pub fn cut(s: &str, max: usize) -> String {
    if s.chars().count() <= max {
        s.to_string()
    } else {
        format!("{}...", s.chars().take(max).collect::<String>())
    }
}

/// Text between `<tag>` and `</tag>` (last closing tag), without code fences.
pub fn block(text: &str, tag: &str) -> Option<String> {
    let open = format!("<{tag}>");
    let close = format!("</{tag}>");
    let start = text.find(&open)? + open.len();
    let end = start + text[start..].rfind(&close)?;
    Some(strip_fences(&text[start..end]))
}

/// The YAML inside `<tag>...</tag>`. Models sometimes put more inside the
/// block than asked (a fenced YAML block followed by other tagged blocks):
/// keep the first fenced block if there is one, else stop at the first line
/// that opens another tag.
pub fn yaml_block(text: &str, tag: &str) -> Option<String> {
    let body = block(text, tag)?;
    let lines: Vec<&str> = body.lines().collect();
    if let Some(open) = lines.iter().position(|l| l.trim_start().starts_with("```")) {
        let close = lines[open + 1..]
            .iter()
            .position(|l| l.trim_start().starts_with("```"))
            .map_or(lines.len(), |c| open + 1 + c);
        return Some(lines[open + 1..close].join("\n"));
    }
    let end = lines
        .iter()
        .position(|l| {
            let mut c = l.chars();
            c.next() == Some('<') && c.next().is_some_and(|c| c.is_ascii_alphabetic())
        })
        .unwrap_or(lines.len());
    Some(lines[..end].join("\n"))
}

/// A cleanly quoted YAML scalar: `"..."` with only escaped inner quotes, or
/// `'...'` with only doubled inner quotes.
fn cleanly_quoted(v: &str) -> bool {
    let v = v.trim();
    if v.len() < 2 {
        return false;
    }
    if let Some(inner) = v.strip_prefix('"').and_then(|r| r.strip_suffix('"')) {
        let mut escaped = false;
        for c in inner.chars() {
            match c {
                '\\' if !escaped => escaped = true,
                '"' if !escaped => return false,
                _ => escaped = false,
            }
        }
        return !escaped;
    }
    if let Some(inner) = v.strip_prefix('\'').and_then(|r| r.strip_suffix('\'')) {
        return !inner.replace("''", "").contains('\'');
    }
    false
}

fn requote(v: &str) -> String {
    format!("\"{}\"", v.replace('\\', "\\\\").replace('"', "\\\""))
}

/// Re-quotes scalars YAML rejects (`returns: "ok " followed by the port`,
/// a list item starting with a backtick). Block scalars (`cases: |`) are
/// left alone.
pub fn repair_yaml(text: &str) -> String {
    let keyed = regex::Regex::new(r"^(\s*(?:- )?[A-Za-z_][\w]*:[ \t]+)(.+?)\s*$").expect("regex");
    let item = regex::Regex::new(r"^(\s*- )(.+?)\s*$").expect("regex");
    let mut out = Vec::new();
    let mut block_indent: Option<usize> = None;
    for line in text.lines() {
        let indent = line.len() - line.trim_start().len();
        if let Some(b) = block_indent {
            if line.trim().is_empty() || indent > b {
                out.push(line.to_string());
                continue;
            }
            block_indent = None;
        }
        let bad = |v: &str| {
            let first = v.chars().next().unwrap_or(' ');
            matches!(first, '"' | '\'' | '`' | '@' | '%') && !cleanly_quoted(v)
        };
        let fixed = if let Some(c) = keyed.captures(line) {
            let v = &c[2];
            if v.starts_with('|') || v.starts_with('>') {
                block_indent = Some(indent);
                None
            } else if bad(v) {
                Some(format!("{}{}", &c[1], requote(v)))
            } else {
                None
            }
        } else if let Some(c) = item.captures(line) {
            bad(&c[2]).then(|| format!("{}{}", &c[1], requote(&c[2])))
        } else {
            None
        };
        out.push(fixed.unwrap_or_else(|| line.to_string()));
    }
    let mut s = out.join("\n");
    if text.ends_with('\n') {
        s.push('\n');
    }
    s
}

/// Parses YAML, retrying once after `repair_yaml`; the first error is kept.
pub fn parse_yaml(text: &str) -> Result<yaml_serde::Value, String> {
    match yaml_serde::from_str(text) {
        Ok(v) => Ok(v),
        Err(e) => yaml_serde::from_str(&repair_yaml(text)).map_err(|_| e.to_string()),
    }
}

/// Every `<tag path="...">...</tag>` block, in order.
pub fn files(text: &str, tag: &str) -> Vec<(String, String)> {
    let re = regex::Regex::new(&format!(r#"(?s)<{tag}\s+path="([^"]+)"\s*>(.*?)</{tag}>"#))
        .expect("regex");
    re.captures_iter(text)
        .map(|c| (c[1].trim().replace('\\', "/"), strip_fences(&c[2]) + "\n"))
        .collect()
}

pub fn strip_fences(s: &str) -> String {
    let t = s.trim_matches(|c| c == '\n' || c == '\r');
    let lines: Vec<&str> = t.lines().collect();
    match (lines.first(), lines.last()) {
        (Some(a), Some(b))
            if lines.len() >= 2 && a.trim_start().starts_with("```") && b.trim() == "```" =>
        {
            lines[1..lines.len() - 1].join("\n")
        }
        _ => t.trim_end().to_string(),
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn usage_counts_cache_reads_and_writes_once() {
        // Meridian, first and second call with the same prompt.
        let w = Usage::parse(&json!({"prompt_tokens": 4658, "completion_tokens": 4,
            "prompt_tokens_details": {"cached_tokens": 0, "cache_write_tokens": 4656}}));
        assert_eq!((w.input, w.cache_write, w.fresh()), (4658, 4656, 2));
        let r = Usage::parse(&json!({"prompt_tokens": 4658, "completion_tokens": 4,
            "prompt_tokens_details": {"cached_tokens": 4656, "cache_write_tokens": 0}}));
        assert_eq!(
            (r.input, r.cache_read, r.output, r.fresh()),
            (4658, 4656, 4, 2)
        );
        // Anthropic's native shape: input_tokens exclude the cache.
        let a = Usage::parse(&json!({"input_tokens": 2, "output_tokens": 4,
            "cache_read_input_tokens": 4656, "cache_creation_input_tokens": 0}));
        assert_eq!(a, r);
        assert_eq!(Usage::parse(&Value::Null), Usage::default());
    }

    #[test]
    fn yaml_block_ignores_extra_blocks() {
        let r = "<unit>\n```yaml\ngoal: x\n```\n\n<tests>\ncases: |\n  f() == 1\n</tests>\n<file path=\"a\">b</file>\n</unit>";
        assert_eq!(yaml_block(r, "unit").as_deref(), Some("goal: x"));
        let r = "<unit>\ngoal: x\n\n<tests>\ncases: y\n</tests>\n</unit>";
        assert_eq!(yaml_block(r, "unit").as_deref(), Some("goal: x\n"));
    }

    #[test]
    fn repair_requotes_broken_scalars_only() {
        let bad = "functions:\n  - name: f\n    returns: \"ok \" followed by the port\nrequirements:\n  - `f` never panics\n  - \"fine\"\ncases: |\n  f(\"a\") == \"b\"\n  \"x\" ok\n";
        assert!(yaml_serde::from_str::<yaml_serde::Value>(bad).is_err());
        let v = parse_yaml(bad).unwrap();
        assert_eq!(
            v["functions"][0]["returns"].as_str(),
            Some("\"ok \" followed by the port")
        );
        assert_eq!(v["requirements"][0].as_str(), Some("`f` never panics"));
        assert_eq!(v["requirements"][1].as_str(), Some("fine"));
        assert_eq!(v["cases"].as_str(), Some("f(\"a\") == \"b\"\n\"x\" ok\n"));
    }

    #[test]
    fn blocks_and_files() {
        let r = "<unit>\n```yaml\ngoal: x\n```\n</unit>\n<file path=\"src/a.c\">\nint a;\n</file>\n<file path='bad'>x</file>";
        assert_eq!(block(r, "unit").as_deref(), Some("goal: x"));
        assert_eq!(
            files(r, "file"),
            vec![("src/a.c".to_string(), "int a;\n".to_string())]
        );
        assert!(is_loopback("http://127.0.0.1:3456/v1"));
    }
}
