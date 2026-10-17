//! OpenAI-compatible chat-completions client. Works with Anthropic's
//! compatibility endpoint, OpenAI, Meridian (which fronts Claude Code's own
//! login) and any server that speaks `/chat/completions`.

use crate::config::Config;
use serde_json::{Value, json};
use std::cell::Cell;
use std::env;
use std::thread;
use std::time::Duration;

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
            .or_else(|| env_get("LEVELUP_BASE_URL"))
            .or_else(|| cfg.base_url.clone());
        let provider = c
            .provider
            .or_else(|| env_get("LEVELUP_PROVIDER"))
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
            return Err("provider `custom` needs --base-url (or LEVELUP_BASE_URL, or base_url in the config)".into());
        }
        let model = c
            .model
            .or_else(|| env_get("LEVELUP_MODEL"))
            .or_else(|| cfg.model.clone())
            .or_else(|| p.default_model.map(str::to_string))
            .ok_or_else(|| format!("provider `{provider}` has no default model; pass --model"))?;
        let key_env = c.key_env.or_else(|| cfg.api_key_env.clone());
        let key = match key_env {
            Some(name) => Some(
                env_get(&name).ok_or_else(|| format!("API key variable `{name}` is not set"))?,
            ),
            None => {
                env_get("LEVELUP_API_KEY").or_else(|| p.key_envs.iter().find_map(|k| env_get(k)))
            }
        };
        if key.is_none() && !(p.keyless_ok || is_loopback(&base_url)) {
            return Err(format!(
                "no API key for {base_url}: set LEVELUP_API_KEY (or {}), or name a variable with --api-key-env",
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
        })
    }

    /// One non-streaming completion; retries transient failures.
    pub fn chat(&self, messages: &[Msg]) -> Result<String, String> {
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
                thread::sleep(Duration::from_secs(2 << attempt));
            }
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
            return self.extract(&text);
        }
        Err(last)
    }

    fn extract(&self, text: &str) -> Result<String, String> {
        let v: Value = serde_json::from_str(text)
            .map_err(|e| format!("response is not JSON ({e}): {}", cut(text, 200)))?;
        self.calls.set(self.calls.get() + 1);
        let u = &v["usage"];
        self.prompt_tokens
            .set(self.prompt_tokens.get() + u["prompt_tokens"].as_u64().unwrap_or(0));
        self.completion_tokens
            .set(self.completion_tokens.get() + u["completion_tokens"].as_u64().unwrap_or(0));
        let cached = u["prompt_tokens_details"]["cached_tokens"]
            .as_u64()
            .unwrap_or(0);
        self.cached_tokens.set(self.cached_tokens.get() + cached);
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
        let cached = self.cached_tokens.get();
        format!(
            "{} call(s), {} input tokens{}, {} output tokens ({})",
            self.calls.get(),
            self.prompt_tokens.get(),
            if cached > 0 {
                format!(" ({cached} cached)")
            } else {
                String::new()
            },
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
