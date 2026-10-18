//! Longest word of a text.

/// Longest word of `text`; the first one wins on ties.
pub fn longest_word(text: &str) -> Option<&str> {
    let mut best: Option<&str> = None;
    for w in text.split_whitespace() {
        if best.is_none_or(|b| w.chars().count() > b.chars().count()) {
            best = Some(w);
        }
    }
    best
}

/// Number of whitespace-separated words.
pub fn word_count(text: &str) -> usize {
    text.split_whitespace().count()
}
