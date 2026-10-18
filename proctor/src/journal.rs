//! Append-only event log (`.proctor/journal.jsonl`). PROGRESS.md and the
//! course state are both derived from it.

use serde::{Deserialize, Serialize};
use std::fs::{self, OpenOptions};
use std::io::Write;
use std::path::Path;
use std::time::{SystemTime, UNIX_EPOCH};

#[derive(Serialize, Deserialize, Debug, Clone, Default)]
pub struct Event {
    /// Unix seconds.
    pub t: u64,
    /// `unlock`, `check` or `pass`.
    pub ev: String,
    pub unit: String,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub ok: Option<bool>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub passed: Option<usize>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub total: Option<usize>,
    /// Check category of the first failure (`structure`, `tests`, ...).
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub cat: Option<String>,
    /// Name of the first failing item.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub item: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub msg: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub seed: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub ms: Option<u64>,
    /// Compiler / linter warnings seen in the build step.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub warnings: Option<usize>,
}

pub fn now() -> u64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|d| d.as_secs())
        .unwrap_or(0)
}

fn path(root: &Path) -> std::path::PathBuf {
    root.join(crate::course::HIDDEN).join("journal.jsonl")
}

pub fn read(root: &Path) -> Vec<Event> {
    let Ok(text) = fs::read_to_string(path(root)) else {
        return Vec::new();
    };
    text.lines()
        .filter_map(|l| serde_json::from_str(l).ok())
        .collect()
}

pub fn append(root: &Path, e: &Event) -> Result<(), String> {
    let p = path(root);
    let line = serde_json::to_string(e).map_err(|e| e.to_string())?;
    let mut f = OpenOptions::new()
        .create(true)
        .append(true)
        .open(&p)
        .map_err(|e| format!("{}: {e}", p.display()))?;
    writeln!(f, "{line}").map_err(|e| format!("{}: {e}", p.display()))
}

/// (year, month, day) for days since 1970-01-01 (proleptic Gregorian).
pub fn civil(days: i64) -> (i64, u32, u32) {
    let z = days + 719_468;
    let era = z.div_euclid(146_097);
    let doe = z.rem_euclid(146_097);
    let yoe = (doe - doe / 1460 + doe / 36_524 - doe / 146_096) / 365;
    let y = yoe + era * 400;
    let doy = doe - (365 * yoe + yoe / 4 - yoe / 100);
    let mp = (5 * doy + 2) / 153;
    let d = (doy - (153 * mp + 2) / 5 + 1) as u32;
    let m = if mp < 10 { mp + 3 } else { mp - 9 } as u32;
    (if m <= 2 { y + 1 } else { y }, m, d)
}

pub fn day_of(t: u64) -> i64 {
    (t / 86_400) as i64
}

pub fn date(t: u64) -> String {
    let (y, m, d) = civil(day_of(t));
    format!("{y:04}-{m:02}-{d:02}")
}

pub fn datetime(t: u64) -> String {
    let s = t % 86_400;
    format!("{} {:02}:{:02}", date(t), s / 3600, (s / 60) % 60)
}

/// Day number of the Monday starting `t`'s week.
pub fn week_start(t: u64) -> i64 {
    let d = day_of(t);
    // 1970-01-01 was a Thursday: Monday-based weekday = (d + 3) mod 7.
    d - (d + 3).rem_euclid(7)
}

pub fn duration(secs: u64) -> String {
    let h = secs / 3600;
    let m = (secs / 60) % 60;
    match (h, m) {
        (0, 0) if secs > 0 => "< 1 min".into(),
        (0, m) => format!("{m} min"),
        (h, 0) => format!("{h} h"),
        (h, m) => format!("{h} h {m:02}"),
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn calendar() {
        assert_eq!(date(0), "1970-01-01");
        assert_eq!(date(1_792_195_200), "2026-10-17");
        // 2026-10-17 is a Saturday; its week starts Monday 2026-10-12.
        assert_eq!(
            date(week_start(1_792_195_200) as u64 * 86_400),
            "2026-10-12"
        );
        assert_eq!(duration(5400), "1 h 30");
    }
}
