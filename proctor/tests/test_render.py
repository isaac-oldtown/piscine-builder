from __future__ import annotations

from proctor.render import in_place, split_eq


def test_splits_one_top_level_equality() -> None:
    assert split_eq('ft_repeat("a==b", 2) == "a==ba==b"') == (
        'ft_repeat("a==b", 2)',
        '"a==ba==b"',
    )
    assert split_eq("f(1) == 1 && g(2) == 2") is None
    assert split_eq("raises(f(1))") is None


def test_in_place_signatures() -> None:
    assert in_place("pub fn ft_capitalize(s: &mut String)")
    assert not in_place("pub fn ft_concat(a: String, b: &str) -> String")
    assert in_place("void str_rev(char *s)")
    assert not in_place("size_t str_len(const char *s)")
