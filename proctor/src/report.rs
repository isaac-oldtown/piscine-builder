//! The live terminal report.

use crate::engine::{Item, Phase, Reporter, Status, Verdict};
use crate::unit::Unit;
use std::io::{IsTerminal, Write};

pub fn truncate(s: &str, max: usize) -> String {
    let one_line: String = s.replace(['\n', '\r'], " ");
    if one_line.chars().count() <= max {
        one_line
    } else {
        let cut: String = one_line.chars().take(max).collect();
        format!("{cut}...")
    }
}

pub struct Style {
    color: bool,
    ascii: bool,
}

impl Style {
    pub fn detect() -> Style {
        Style {
            color: std::io::stdout().is_terminal() && std::env::var_os("NO_COLOR").is_none(),
            ascii: std::env::var_os("PROCTOR_ASCII").is_some(),
        }
    }
    fn paint(&self, code: &str, s: &str) -> String {
        if self.color {
            format!("\x1b[{code}m{s}\x1b[0m")
        } else {
            s.to_string()
        }
    }
    pub fn green(&self, s: &str) -> String {
        self.paint("32", s)
    }
    pub fn red(&self, s: &str) -> String {
        self.paint("31", s)
    }
    pub fn yellow(&self, s: &str) -> String {
        self.paint("33", s)
    }
    pub fn dim(&self, s: &str) -> String {
        self.paint("2", s)
    }
    pub fn bold(&self, s: &str) -> String {
        self.paint("1", s)
    }
    fn ok_mark(&self) -> String {
        self.green(if self.ascii { "[ok]" } else { "✔" })
    }
    fn ko_mark(&self) -> String {
        self.red(if self.ascii { "[KO]" } else { "✘" })
    }
    fn skip_mark(&self) -> String {
        self.dim(if self.ascii { "[--]" } else { "·" })
    }
    fn run_mark(&self) -> String {
        if self.ascii { ">".into() } else { "▸".into() }
    }
}

pub struct Term {
    pub style: Style,
    pub verbose: bool,
    pub quiet: bool,
}

impl Term {
    fn out(&self, s: &str) {
        if !self.quiet {
            println!("{s}");
            let _ = std::io::stdout().flush();
        }
    }
}

const NAME_W: usize = 11;

impl Reporter for Term {
    fn start(&mut self, unit: &Unit, seed: u64, against: &str) {
        let s = &self.style;
        self.out(&format!(
            "{} {}  {}",
            s.bold("proctor"),
            s.bold(&unit.id),
            s.dim(&format!("seed {seed:016x}  against {against}"))
        ));
    }

    fn phase_begin(&mut self, name: &str, streamed: bool) {
        if streamed {
            let line = format!(" {} {name}", self.style.run_mark());
            self.out(&line);
        }
    }

    fn item(&mut self, item: &Item) {
        let s = &self.style;
        let mark = if item.ok { s.ok_mark() } else { s.ko_mark() };
        let detail = if item.detail.is_empty() {
            String::new()
        } else if item.ok {
            format!("  {}", s.dim(&item.detail))
        } else if item.course_error {
            format!("  {}", s.yellow(&truncate(&item.detail, 220)))
        } else {
            format!("  {}", s.red(&truncate(&item.detail, 220)))
        };
        self.out(&format!("   {mark} {}{detail}", truncate(&item.name, 72)));
    }

    fn phase_end(&mut self, p: &Phase) {
        let s = &self.style;
        let mark = match p.status {
            Status::Pass => s.ok_mark(),
            Status::Fail => s.ko_mark(),
            Status::Skip => s.skip_mark(),
        };
        let summary = match p.status {
            Status::Fail if !p.streamed => s.red(&truncate(&p.summary, 200)),
            Status::Skip => s.dim(&p.summary),
            _ => p.summary.clone(),
        };
        self.out(&format!(" {mark} {:<NAME_W$} {summary}", p.name));
        if p.status == Status::Fail && !p.streamed {
            let failing: Vec<&Item> = p.items.iter().filter(|i| !i.ok).collect();
            if failing.len() > 1 {
                for i in failing.iter().take(8) {
                    self.out(&format!(
                        "   {} {}  {}",
                        s.ko_mark(),
                        i.name,
                        s.red(&truncate(&i.detail, 200))
                    ));
                }
                if failing.len() > 8 {
                    self.out(&format!(
                        "   {}",
                        s.dim(&format!("... and {} more", failing.len() - 8))
                    ));
                }
            }
        }
    }

    fn log(&mut self, text: &str) {
        if self.verbose && !text.trim().is_empty() {
            self.out(&self.style.dim(text.trim_end()));
        }
    }
}

/// `OK 17/17` or `KO <item>: <reason>`.
pub fn verdict_line(v: &Verdict, seed: u64, style: &Style) -> String {
    match &v.first_fail {
        None if v.ok => style.green(&format!("OK {}/{}", v.passed, v.total)),
        None => style.red("KO"),
        Some((cat, item)) => {
            let mut line = match cat.as_str() {
                "tests" | "regression" => format!(
                    "KO {}: {}",
                    truncate(&item.name, 60),
                    truncate(&item.detail, 240)
                ),
                "build" if item.name == "compile" => {
                    format!("KO build: {}", truncate(&item.detail, 240))
                }
                "build" => format!("KO {}: {}", item.name, truncate(&item.detail, 240)),
                _ => format!(
                    "KO {cat}: {} {}",
                    truncate(&item.name, 60),
                    truncate(&item.detail, 240)
                ),
            };
            if item.random {
                line.push_str(&format!("   (replay: proctor check --seed {seed:016x})"));
            }
            style.red(&line)
        }
    }
}
