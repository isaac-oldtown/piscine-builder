from __future__ import annotations

from pathlib import Path

import pytest
from coursecrafter.build import (
    Draft,
    cached_reply_of,
    clean_think_about,
    drop_fixed_keys,
    fnv,
    names,
    one_workspace_root,
    redundant_requirement,
    revealing_phrase,
    scaffold_cargo_toml,
    uncovered_concepts,
)
from proctor.compat import read_text
from proctor.errors import Error

from coursecrafter import build, config, curriculum, llm
from proctor import yamlio


def test_lessons_cover_each_concept_and_think_about_spoils_nothing() -> None:
    lesson = "### Variables with `let`\ntext\n### Ownership and moves\ntext\n### Try it\n"
    intro = ["let", "mut", "ownership and moves"]
    assert uncovered_concepts(lesson, intro) == ["mut"]
    cases = 'ft_capitalize("123abc") == "123abc"\nft_repeat("ab", 1000) == 2000\ncount("the", "the cat") == 1\nlines("no newline") == 1\n'

    def clean(items: list[str]) -> tuple[list[str], list[str]]:
        return clean_think_about(items, cases)

    # Plain words that also appear in a case are not a leak (from a real build).
    kept, dropped = clean(
        [
            "text or lists with nothing in them",
            "a last line with no newline",
            "the empty string",
            "10000 repetitions",
        ]
    )
    assert (len(kept), len(dropped)) == (4, 0), dropped
    # A bullet that gives a case away is dropped; the rest is kept.
    kept, dropped = clean(
        [
            'what "123abc" gives',
            "counts like 1000",
            "a word such as 123abc",
            "a string that starts with a digit",
            "repeating a string many times",
        ]
    )
    assert len(kept) == 2
    assert len(dropped) == 3, dropped
    five = [f"case kind {i}" for i in range(5)]
    assert len(clean_think_about(five, cases)[0]) == 4
    with pytest.raises(Error):
        clean(["counts like 1000", "a string that starts with a digit"])
    assert clean([]) == ([], [])
    assert redundant_requirement("Must pass `cargo fmt`.") == "the linters"
    assert redundant_requirement("Print `LEVEL count` lines.") is None


def test_rust_test_harness_is_not_a_revealed_secret() -> None:
    ok = "the test harness collects it and runs it on its own."
    assert revealing_phrase(ok) is None
    assert revealing_phrase("compare with the reference solution") == "reference solution"
    assert revealing_phrase("the hidden tests check it") == "hidden test"


def test_a_failed_unit_forgets_only_its_own_replies() -> None:
    key = fnv(["model", "request"])
    assert cached_reply_of(f"tests-d03-x-{key}.txt", "tests-d03-x")
    assert not cached_reply_of(f"tests-d03-x-2-{key}.txt", "tests-d03-x")
    assert not cached_reply_of(f"contract-d03-x-{key}.txt", "tests-d03-x")
    assert not cached_reply_of("tests-d03-x-notes.txt", "tests-d03-x")


def test_only_accepts_short_ids_and_projects() -> None:
    plan = curriculum.parse(
        "---\ntitle: T\nlanguage: rust\n---\n\n## d03-refs-slices: R\ngoal: g.\n\n## p02-minishell: S\ngoal: g.\n\n### p02.1-repl: A\ngoal: g.\n"
    )
    d, st = plan.entries[0], plan.entries[2]
    assert names("d03", d) and names("d03-refs-slices", d) and not names("d3", d)
    assert names("p02", st) and names("p02-minishell", st) and names("p02.1", st)
    assert not names("d03", st)


def test_scaffold_cargo_toml_keeps_the_package_and_drops_dependencies() -> None:
    r = '[package]\nname = "minishell"\nedition = "2021"\n\n[dependencies]\nnix = { version = "0.29" }\n\n[dependencies.libc]\nversion = "0.2"\n\n[[bin]]\nname = "ft"\npath = "src/main.rs"\n\n[workspace]\n'
    s = scaffold_cargo_toml(r)
    assert 'name = "minishell"' in s and "[dependencies]\n" in s
    assert '[[bin]]\nname = "ft"' in s and "[workspace]" in s
    assert "nix" not in s and "libc" not in s, s


def test_member_crates_lose_their_own_workspace_table() -> None:
    # The layout of a real reply rejected with "multiple workspace roots".
    files = {
        "Cargo.toml": '[workspace]\nresolver = "2"\nmembers = ["kit", "app"]\n',
        "kit/Cargo.toml": '[package]\nname = "kit"\n\n[dependencies]\n\n[workspace]\n',
        "app/Cargo.toml": '[package]\nname = "app"\n\n[workspace]\nresolver = "2"\n\n[dependencies]\nkit = { path = "../kit" }\n',
        "app/src/main.rs": "fn main() {}\n",
    }
    out, changed = one_workspace_root(files)
    assert changed == ["app/Cargo.toml", "kit/Cargo.toml"]
    assert out["kit/Cargo.toml"] == '[package]\nname = "kit"\n\n[dependencies]\n'
    assert 'kit = { path = "../kit" }' in out["app/Cargo.toml"]
    assert "workspace" not in out["app/Cargo.toml"] and "resolver" not in out["app/Cargo.toml"]
    assert out["Cargo.toml"] == files["Cargo.toml"]
    # A single crate keeps its table.
    one = {"Cargo.toml": '[package]\nname = "a"\n\n[workspace]\n'}
    assert one_workspace_root(one) == (one, [])


def test_repeated_fixed_keys_cannot_break_the_contract() -> None:
    y = 'id: d01\nconcepts: [integer types, formatting with {} and {:?}]\ntarget: program\nrequirements:\n  - "id: kept, it is inside a list"\n'
    with pytest.raises(Error):
        yamlio.load(y)
    fixed = drop_fixed_keys(y)
    m = yamlio.load(fixed)
    assert "concepts" not in m and "id" not in m
    assert m.get("target") == "program"
    assert "id: kept" in fixed


def test_a_retry_replaces_only_what_it_sends() -> None:
    d = Draft()
    d.merge(
        '<tests>\ncases: |\n  f(1) == 1\n</tests>\n<file path="src/main.rs">\nfn main() {}\n</file>\n<file path="Cargo.toml">\n[package]\n</file>\n'
    )
    d.merge(
        '<file path="src/main.rs">\nfn main() { println!(); }\n</file>\n<file path="old.rs">\n</file>\n'
    )
    assert d.tests is not None and "f(1) == 1" in d.tests
    assert "println" in d.files["src/main.rs"]
    assert "Cargo.toml" in d.files
    assert "old.rs" not in d.files


def test_cache_keys_match_the_rust_tool() -> None:
    # Values printed by the Rust `fnv`: existing reply caches keep hitting.
    assert fnv(["model", "request"]) == "423502b9c439e619"
    assert fnv([]) == "cbf29ce484222325"
    assert fnv([""]) == "af63bd4c8601b7df"
    assert fnv(["claude-sonnet-5-5", "système é ✓ 😀", "x\0y"]) == "61560aeb6b56308a"
    assert fnv(["fake-model", "a\nb", "\r\n"]) == "f0e984f37f262726"


def test_removing_a_key_moves_the_last_one_into_its_place() -> None:
    # yaml_serde's `Mapping::remove` is a swap_remove; outputs from the Rust tool.
    cases = [
        ("a: 1\nb: 2\nc: 3\nd: 4\n", ["b"], "a: 1\nd: 4\nc: 3\n"),
        ("a: 1\nb: 2\nc: 3\n", ["c"], "a: 1\nb: 2\n"),
        ("a: 1\nb: 2\nc: 3\nd: 4\ne: 5\n", ["a", "c", "zz"], "e: 5\nb: 2\nd: 4\n"),
    ]
    for text, keys, want in cases:
        m = yamlio.load(text)
        for k in keys:
            build._remove(m, k)
        assert yamlio.dump(m) == want


def test_yaml_equality_ignores_order_but_not_kinds() -> None:
    def eq(a: str, b: str) -> bool:
        return build._yaml_eq(yamlio.load(a), yamlio.load(b))

    assert eq("a: 1\nb: 2\n", "b: 2\na: 1\n")
    assert eq("a: {x: 1, y: 2}\n", "a: {y: 2, x: 1}\n")
    assert eq("a: .nan\n", "a: .nan\n")
    assert not eq("a: 1\n", "a: 1.0\n")
    assert not eq("a: true\n", "a: 1\n")
    assert not eq("a: [1, 2]\n", "a: [2, 1]\n")
    assert not eq("a: '1'\n", "a: 1\n")


def test_replies_are_cached_by_the_full_request(tmp_path: Path) -> None:
    plan = curriculum.parse("---\ntitle: T\nlanguage: python\n---\n\n## d01-a: A\ngoal: g.\n")
    dead = llm.Llm.resolve(
        llm.Choice(provider="custom", base_url="http://127.0.0.1:9/v1", model="m"),
        config.Config(),
    )
    opts = build.Options(retries=0, tests=False, only=[], yes=True, text_only=False, debug=False)
    b = build.Builder.new(tmp_path, plan, dead, opts, "ENV")
    messages = [llm.Msg.system("s"), llm.Msg.user("u")]
    cache = tmp_path / ".proctor" / "coursecrafter" / "cache"
    cache.mkdir(parents=True)
    (cache / f"contract-d01-a-{fnv(['m', 's', 'u'])}.txt").write_bytes(b"cached reply")
    (cache / f"contract-d01-a-x-{fnv(['m', 's', 'u'])}.txt").write_bytes(b"other unit")
    assert b.chat("contract-d01-a", messages) == "cached reply"
    assert b.log.cache_hits == 1 and dead.calls == 0
    b.forget("contract-d01-a")
    assert [p.name for p in cache.iterdir()] == [f"contract-d01-a-x-{fnv(['m', 's', 'u'])}.txt"]
    assert "dropped 1 cached reply(ies) of contract-d01-a" in read_text(b.log.path)
