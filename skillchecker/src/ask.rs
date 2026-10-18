//! Multiple-choice questions in the terminal, one at a time:
//!
//! ```text
//! ? What should the final project be?
//!   a) A Unix shell (recommended)
//!   b) A multithreaded HTTP server
//!   c) Something else: type your own answer
//! Choice [a]:
//! ```
//!
//! Enter takes the recommended option; a letter picks one; any other text is
//! taken as a free answer. Without a terminal the recommended option is used.

use std::io::{BufRead, IsTerminal, Write};

pub enum Answer {
    /// Index into the options.
    Pick(usize),
    /// Free text typed instead of a letter.
    Other(String),
}

impl Answer {
    /// The chosen option's label, or the free text.
    pub fn text(&self, options: &[String]) -> String {
        match self {
            Answer::Pick(i) => options.get(*i).cloned().unwrap_or_default(),
            Answer::Other(s) => s.clone(),
        }
    }
}

pub fn interactive() -> bool {
    std::io::stdin().is_terminal()
}

fn letter(i: usize) -> char {
    (b'a' + (i as u8).min(25)) as char
}

fn read_line(prompt: &str) -> Option<String> {
    print!("{prompt}");
    let _ = std::io::stdout().flush();
    let mut line = String::new();
    match std::io::stdin().lock().read_line(&mut line) {
        Ok(0) | Err(_) => None,
        Ok(_) => Some(line.trim().to_string()),
    }
}

/// Asks one question. `other` is the label of the free-answer line (for
/// example "Something else: type your own answer"), or `None` when only the
/// listed options make sense.
pub fn choose(
    question: &str,
    options: &[String],
    recommended: usize,
    other: Option<&str>,
) -> Answer {
    ask(question, options, recommended, other, other.is_some())
}

/// Like `choose` without a free-answer line, but text typed instead of a
/// letter is still taken as the answer (for menus where an option already
/// asks for text).
pub fn choose_or_type(question: &str, options: &[String], recommended: usize) -> Answer {
    ask(question, options, recommended, None, true)
}

fn ask(
    question: &str,
    options: &[String],
    recommended: usize,
    other: Option<&str>,
    accept_text: bool,
) -> Answer {
    let recommended = recommended.min(options.len().saturating_sub(1));
    if !interactive() || options.is_empty() {
        return Answer::Pick(recommended);
    }
    println!("\n? {question}");
    for (i, o) in options.iter().enumerate() {
        let tag = if i == recommended {
            " (recommended)"
        } else {
            ""
        };
        println!("  {}) {o}{tag}", letter(i));
    }
    let other_letter = letter(options.len());
    if let Some(label) = other {
        println!("  {other_letter}) {label}");
    }
    loop {
        let Some(input) = read_line(&format!("Choice [{}]: ", letter(recommended))) else {
            return Answer::Pick(recommended);
        };
        let lower = input.to_lowercase();
        let picked = match lower.as_str() {
            "" => Some(recommended),
            s if s.chars().count() == 1 => {
                let c = s.chars().next().unwrap_or('?');
                (c as usize)
                    .checked_sub('a' as usize)
                    .filter(|i| *i < options.len())
            }
            _ => None,
        };
        if let Some(i) = picked {
            println!("  -> {}", options[i]);
            return Answer::Pick(i);
        }
        if other.is_some() && lower == other_letter.to_string() {
            match read_line("Your answer: ") {
                Some(t) if !t.is_empty() => {
                    println!("  -> {t}");
                    return Answer::Other(t);
                }
                _ => continue,
            }
        }
        if accept_text && input.chars().count() > 1 {
            println!("  -> {input}");
            return Answer::Other(input);
        }
        let last = if other.is_some() {
            other_letter
        } else {
            letter(options.len() - 1)
        };
        let text = if accept_text && other.is_none() {
            ", your own text"
        } else {
            ""
        };
        println!("  Type a letter from a to {last}{text}, or press Enter for the recommended one.");
    }
}

/// A yes/no question as two options, "yes" recommended unless `default_no`.
pub fn yes_no(question: &str, yes: &str, no: &str, default_no: bool) -> bool {
    let options = [yes.to_string(), no.to_string()];
    matches!(
        choose(question, &options, usize::from(default_no), None),
        Answer::Pick(0)
    )
}
