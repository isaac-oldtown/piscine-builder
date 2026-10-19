//! The log of one coursecrafter run (`new`, `revise`, `convert`, `build`):
//! every LLM call with its purpose, time and tokens, cache hits, rejections,
//! autofixes, and a closing summary. It goes to
//! `.proctor/coursecrafter/logs/<command>-<date>-<time>.log`; `--debug` also
//! prints it to the terminal as it is written.

use crate::llm::{CallRecord, Llm, Msg, Usage};
use std::collections::BTreeMap;
use std::fs::{self, OpenOptions};
use std::io::Write;
use std::path::{Path, PathBuf};
use std::time::{Duration, Instant};

pub struct Session {
    pub path: PathBuf,
    debug: bool,
    start: Instant,
    llm_time: Duration,
    user_time: Duration,
    /// Calls per purpose: (count, tokens).
    purposes: BTreeMap<String, (u32, Usage)>,
    calls: Vec<(String, CallRecord)>,
    /// Replies read from the on-disk cache instead of asking the model.
    pub cache_hits: u32,
    /// Extra summary lines (the command's own counts).
    pub notes: Vec<String>,
    pub questions: usize,
    pub drafts: u32,
    pub rejections: u32,
    pub autofixes: usize,
    pub revisions: u32,
}

/// `YYYY-MM-DD` and `HH:MM:SS` (UTC) of a Unix time.
fn civil(t: u64) -> (String, String) {
    let (days, secs) = ((t / 86_400) as i64, t % 86_400);
    // Howard Hinnant's days-to-civil algorithm.
    let z = days + 719_468;
    let era = z.div_euclid(146_097);
    let doe = z - era * 146_097;
    let yoe = (doe - doe / 1460 + doe / 36_524 - doe / 146_096) / 365;
    let doy = doe - (365 * yoe + yoe / 4 - yoe / 100);
    let mp = (5 * doy + 2) / 153;
    let d = doy - (153 * mp + 2) / 5 + 1;
    let m = if mp < 10 { mp + 3 } else { mp - 9 };
    let y = yoe + era * 400 + i64::from(m <= 2);
    (
        format!("{y:04}-{m:02}-{d:02}"),
        format!("{:02}:{:02}:{:02}", secs / 3600, secs / 60 % 60, secs % 60),
    )
}

fn secs(d: Duration) -> String {
    let s = d.as_secs_f64();
    if s >= 60.0 {
        format!("{}m{:02}s", (s / 60.0) as u64, s as u64 % 60)
    } else {
        format!("{s:.1}s")
    }
}

fn tokens(u: &Usage) -> String {
    format!(
        "in {} (fresh {}, cache read {}, cache write {}), out {}",
        u.input,
        u.fresh(),
        u.cache_read,
        u.cache_write,
        u.output
    )
}

fn add(a: &mut Usage, b: &Usage) {
    a.input += b.input;
    a.output += b.output;
    a.cache_read += b.cache_read;
    a.cache_write += b.cache_write;
}

impl Session {
    pub fn start(course: &Path, command: &str, debug: bool) -> Session {
        let (date, time) = civil(proctor::journal::now());
        let dir = course
            .join(proctor::course::HIDDEN)
            .join("coursecrafter")
            .join("logs");
        let _ = fs::create_dir_all(&dir);
        let path = dir.join(format!("{command}-{date}-{}.log", time.replace(':', "")));
        let s = Session {
            path,
            debug,
            start: Instant::now(),
            llm_time: Duration::ZERO,
            user_time: Duration::ZERO,
            purposes: BTreeMap::new(),
            calls: Vec::new(),
            cache_hits: 0,
            notes: Vec::new(),
            questions: 0,
            drafts: 0,
            rejections: 0,
            autofixes: 0,
            revisions: 0,
        };
        s.log(&format!(
            "coursecrafter {command}, started {date} {time} UTC"
        ));
        s
    }

    /// One line, prefixed with the time since the start.
    pub fn log(&self, msg: &str) {
        let at = secs(self.start.elapsed());
        let text: String = msg.lines().map(|l| format!("[{at:>7}] {l}\n")).collect();
        if let Ok(mut f) = OpenOptions::new()
            .create(true)
            .append(true)
            .open(&self.path)
        {
            let _ = f.write_all(text.as_bytes());
        }
        if self.debug {
            eprint!("{text}");
        }
    }

    /// A line for the user: on the terminal, and in the log.
    pub fn say(&self, msg: &str) {
        self.log(msg);
        if !self.debug {
            eprintln!("{msg}");
        }
    }

    /// `llm.chat`, timed and logged under `purpose`.
    pub fn chat(&mut self, llm: &Llm, purpose: &str, messages: &[Msg]) -> Result<String, String> {
        self.chat_as(llm, purpose, purpose, messages)
    }

    /// Like `chat`; `label` names the call in its log line (`contract-d02`),
    /// `purpose` groups it in the summary (`contract`).
    pub fn chat_as(
        &mut self,
        llm: &Llm,
        label: &str,
        purpose: &str,
        messages: &[Msg],
    ) -> Result<String, String> {
        let chars: usize = messages.iter().map(|m| m.content.len()).sum();
        self.log(&format!(
            "call {} ({label}): {} message(s), {chars} chars",
            self.calls.len() + 1,
            messages.len()
        ));
        let res = llm.chat(messages);
        let rec = llm.history.borrow().last().cloned().unwrap_or_default();
        self.llm_time += Duration::from_millis(rec.ms as u64);
        for e in &rec.errors {
            self.log(&format!("  error: {e}"));
        }
        let speed = if rec.ms > 0 {
            rec.usage.output as f64 * 1000.0 / rec.ms as f64
        } else {
            0.0
        };
        self.log(&format!(
            "  {} in {}{}: {}, {speed:.0} output tokens/s",
            if rec.ok { "answered" } else { "FAILED" },
            secs(Duration::from_millis(rec.ms as u64)),
            if rec.attempts > 1 {
                format!(" after {} attempts", rec.attempts)
            } else {
                String::new()
            },
            tokens(&rec.usage)
        ));
        let e = self.purposes.entry(purpose.to_string()).or_default();
        e.0 += 1;
        add(&mut e.1, &rec.usage);
        self.calls.push((purpose.to_string(), rec));
        res
    }

    /// A reply served from the on-disk cache.
    pub fn cached(&mut self, label: &str) {
        self.cache_hits += 1;
        self.log(&format!("cached reply ({label}): no call"));
    }

    /// Runs `f` (a question or a menu) and counts its time as the user's.
    pub fn wait_user<T>(&mut self, f: impl FnOnce() -> T) -> T {
        let t = Instant::now();
        let out = f();
        self.user_time += t.elapsed();
        out
    }

    /// The closing summary; `outcome` says how the conversation ended.
    pub fn finish(&self, llm: &Llm, outcome: &str) {
        let total = self.start.elapsed();
        let local = total.saturating_sub(self.llm_time + self.user_time);
        let mut sum = Usage::default();
        for (_, r) in &self.calls {
            add(&mut sum, &r.usage);
        }
        let failed = self.calls.iter().filter(|(_, r)| !r.ok).count();
        let retries: u32 = self
            .calls
            .iter()
            .map(|(_, r)| r.attempts.saturating_sub(1))
            .sum();
        let mut s = format!("summary: {outcome}\n");
        s.push_str(&format!(
            "  time        {} total: {} waiting for the model, {} for you, {} local work\n",
            secs(total),
            secs(self.llm_time),
            secs(self.user_time),
            secs(local)
        ));
        s.push_str(&format!(
            "  calls       {} ({failed} failed, {retries} HTTP retries) to {} at {}\n",
            self.calls.len(),
            llm.model,
            llm.base_url
        ));
        for (p, (n, u)) in &self.purposes {
            s.push_str(&format!("    {p:<12} {n} call(s): {}\n", tokens(u)));
        }
        s.push_str(&format!("  tokens      {}\n", tokens(&sum)));
        if let Some((p, r)) = self.calls.iter().max_by_key(|(_, r)| r.ms) {
            s.push_str(&format!(
                "  slowest     {} ({p}), average {} per call\n",
                secs(Duration::from_millis(r.ms as u64)),
                secs(self.llm_time / self.calls.len().max(1) as u32)
            ));
        }
        if self.cache_hits > 0 {
            s.push_str(&format!(
                "  cache       {} reply(ies) reused from disk at no cost\n",
                self.cache_hits
            ));
        }
        if self.drafts > 0 || self.questions > 0 {
            s.push_str(&format!(
                "  planning    {} question(s), {} draft(s), {} validator rejection(s), {} autofix(es), {} revision(s) asked\n",
                self.questions, self.drafts, self.rejections, self.autofixes, self.revisions
            ));
        }
        for n in &self.notes {
            s.push_str(&format!("  {n}\n"));
        }
        self.log(s.trim_end());
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn civil_dates() {
        assert_eq!(civil(0), ("1970-01-01".into(), "00:00:00".into()));
        assert_eq!(
            civil(1_792_345_022),
            ("2026-10-18".into(), "17:37:02".into())
        );
        assert_eq!(civil(951_782_400).0, "2000-02-29");
    }
}
