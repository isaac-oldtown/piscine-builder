"""Prompt text. Stable parts are constants placed first in every request, so
providers with prompt caching only bill them once per run.

Every request is hashed into the key of the on-disk LLM reply cache: editing
a text makes the unbuilt units of an existing course ask again."""

from __future__ import annotations

from dataclasses import dataclass

from proctor.compat import trim, trim_end

PLAN_FORMAT = r"""# PiscineBuilder curriculum format

A curriculum is ONE markdown file. Output only that file.

Front matter (required):

---
title: <course title>
language: <default language: c, python, rust, bash, go, any name>
level: <beginner | intermediate | advanced>
hours_per_week: <number>
weeks: <number>
rigor: <optional: relaxed | standard | strict (default), how thoroughly hidden tests probe edge cases>
conventions: <optional, one paragraph of course-wide habits units rely on>
---

Then the entries, in teaching order. Each is a heading `## <id>: <Title>` (`### ` for project stages), then
`key: value` lines with NO blank line inside (quote a value that starts with a backtick or contains `: `),
then an optional free-text brief (scope, design decisions, pitfalls) for the exercise writer.

Ids (numbers grow in teaching order, slugs are lowercase words joined by hyphens):
- `dNN-slug`   drill: one skill, one sitting (1 to 4 hours).
- `pNN-slug`   project: app-sized, built in stages that follow it.
- `pNN.k-slug` stage k of project pNN, heading `###`. One to three sittings, at most about 12 hours.
- `note-slug`  note: half a page explaining an easy notion, revealed with a unit.

Keys of a drill or stage:
- goal: one sentence (required)
- concept_difficulty, difficulty: 1 to 5 (required): how hard to understand, how hard to implement and debug
- hours: honest estimate including testing and debugging (required)
- done_when: one line, the observable behaviour showing the essential skills are there
- introduces: [ONE concept per item: three ideas are three items]
- assumes: [everything it needs, basics included]
- target: function | program (function: the grader calls given functions; c, python and rust only)
- scaffold: none | minimal | full (minimal by default; full only for the first drills)
- allows: [libraries, modules or functions the student may use]
- stretch: true (optional unit), guided: true (drills: step-by-step walkthrough),
  platform: linux (only for Linux APIs: fork, pipes, signals, /proc), gradable: false (nothing checkable),
  language: <only if different from the default>
Keys of a project: goal, difficulty, assumes, language, platform, and `minimum` (required): one line, what
the project must do to count as a success. Its brief states what is in and out of scope.
Keys of a note: teaches: [...], with: <id of the unit it is revealed with>.

Rules:
- Teach before use: every `assumes` item is in the `introduces` of an EARLIER unit (or the `teaches` of a
  note revealed with it or earlier), tools included. A hard stage relies only on what was practised in a
  graded unit, never on a note alone. A note is only for notions a short page explains.
- Unless the student already writes the language, open with a guided drill (d00 or d01, 1 to 2 hours,
  difficulty 1): toolchain check, creating, building and running a project, a first program. The first
  ordinary drills introduce at most three concepts each; a stage introduces at most two.
- Before a stage needs a hard concept, a small drill checks it. Introduce the test tool within the first
  three units; project briefs then ask for tests and a regression test for every fixed bug.
- Every drill feeds a later unit. Advanced topics only when a later unit needs them.
- Hours: add half to routinely underestimated topics (ownership, parsing, unsafe, processes, concurrency).
  Required hours leave 10 to 15% of hours_per_week x weeks free; cut scope or add weeks, never squeeze.
- Each project's required stages deliver exactly its `minimum`; extras are stretch stages after the
  required finale. The first stage is never stretch. The last required unit of the course is a
  consolidation stage (5 to 10% of the budget: refactor, tests, README of known limitations).
- All stages of a project extend one program. A stage never specifies behaviour a later stage adds.
  Decide in the briefs what later stages depend on: scope, grammars, output order, edge cases.
- Use the whole 1 to 5 difficulty scale; 5 is for one or two units.
- Portable by default: `platform: linux` only where Linux APIs are needed (every later stage of a project
  then needs it too). The student may use documentation but no AI.
"""

PLAN_EXAMPLE = r"""---
title: C from scratch
language: c
level: beginner
hours_per_week: 15
weeks: 2
---

## d01-hello: Hello, compiler
goal: Compile and run a first program that prints to standard output.
concept_difficulty: 1
difficulty: 1
hours: 1.5
guided: true
done_when: the program prints its greeting and exits with status 0
introduces: [compiling with cc, main, printf, if statements, loops]
target: program
scaffold: full

## d02-strings: Strings by hand
goal: Re-implement basic string helpers with pointers.
concept_difficulty: 3
difficulty: 2
hours: 5
done_when: the helpers match the standard functions on empty, short and long strings
introduces: [pointers, NUL termination, assert-based tests]
assumes: [compiling with cc, if statements, loops]
target: function
allows: [assert.h]

Focus on off-by-one errors and empty strings.

## note-make: What a Makefile is
teaches: [make]
with: p01.1-build

## p01-wc: Mini wc
goal: Build a small clone of `wc` that counts lines, words and bytes.
minimum: prints line, word and byte counts of stdin and of file arguments, like `wc`
difficulty: 3
assumes: [pointers]

In scope: stdin, file arguments, the three counts. Out of scope: GNU flags, locales.

### p01.1-build: Byte count
goal: Organize the project with a Makefile and count the bytes of standard input.
concept_difficulty: 2
difficulty: 2
hours: 3.5
done_when: "`make` builds the binary and it prints the byte count of stdin"
assumes: [make, pointers, assert-based tests]
introduces: [reading stdin, multi-file projects]
target: program

Ship tests for the byte count. Decide now how input is read, the next stage reuses it.

### p01.2-finale: Words and lines
goal: Add word and line counts and file arguments.
concept_difficulty: 2
difficulty: 3
hours: 7
done_when: output matches `wc` on empty files, missing trailing newlines and several files
assumes: [reading stdin, multi-file projects]
introduces: [argv, file I/O]
target: program

### p01.3-consolidate: Consolidation
goal: Refactor the counting code, strengthen the tests and document the limits of the tool.
concept_difficulty: 1
difficulty: 2
hours: 2.5
done_when: the tests still pass and the README lists the known limitations
assumes: [file I/O, assert-based tests]
target: program

### p01.4-unicode: Characters
goal: Add a character count that handles UTF-8 input.
concept_difficulty: 3
difficulty: 4
hours: 5
stretch: true
done_when: "`-m` counts characters, not bytes, on multi-byte input"
assumes: [file I/O]
introduces: [UTF-8 decoding]
target: program
"""

# The planning review: a second look at a draft, by the planner itself.
REVIEW_CHECKLIST = r"""Review the curriculum as a demanding teacher would, then fix it:
1. Prerequisites: every `assumes` item (basics included) is introduced by an earlier unit, and practised
   in a graded unit before a hard stage relies on it.
2. First steps and progression: a guided opening drill unless the student knows the language; at most
   three concepts per first drill, two per stage; a small drill before each hard concept.
3. Testing: the test tool comes within three units; project briefs ask for tests.
4. Purpose: every drill feeds a later unit; cut, merge or mark stretch the rest.
5. Workload: honest `hours` at the course's rigor, extra time for underestimated topics, 10 to 15% of
   the budget free.
6. Every project has a `minimum` its required stages deliver exactly, stretch stages after the finale,
   and the course ends with a consolidation stage.
7. Each `done_when` names the essential skills only; both difficulties are set, with 5 kept rare.
8. Briefs state the design decisions later stages depend on. Nothing the final project needs is untaught."""


def browser_prompt(lang: str | None, level: str | None, hours: str | None) -> str:
    wishes = ""
    if lang is not None:
        wishes += f"- Language: {lang}\n"
    if level is not None:
        wishes += f"- My level: {level}\n"
    if hours is not None:
        wishes += f"- Time: about {hours} hours per week\n"
    if not wishes:
        wishes += "- (Ask me what I want to learn, my level and my weekly time.)\n"
    return (
        "You are helping me plan an intensive, hands-on programming course: short "
        "exercises (drills) and bigger projects built in stages, each graded by an "
        "automatic checker. Ask me clarifying questions first if anything important is "
        "missing, one at a time, each with a few lettered options and the one you "
        "recommend. Then propose the curriculum and refine it with me. Before showing "
        "any version, check it against the review list below and fix what it finds. "
        "When I say it is final, output the complete curriculum in the exact format "
        f"below, as one markdown code block, with nothing else.\n\nWhat I want:\n{wishes}\n"
        f"{PLAN_FORMAT}\n## 4. Example\n\n{PLAN_EXAMPLE}\n## 5. Review list\n\n{REVIEW_CHECKLIST}\n"
    )


def revise_request(current: str, changes: str, measured: str = "") -> str:
    """`coursecrafter revise`: the current file plus the student's requested changes,
    and the time the course journal measured on the units started so far."""
    if measured:
        measured = (
            "## Measured so far\n\n"
            "Time the student spent on each unit started so far, from the course journal. It adds up the "
            "gaps of at most 45 minutes between two of the student's actions (unlock, checks), so it misses "
            "reading and thinking before them: read it as a lower bound. When my request touches hours, "
            "workload or pacing, rely on it: where the student ran well past the plan, give the similar "
            "units still ahead more hours or less scope.\n\n"
            f"{measured}\n"
        )
    return (
        f"Here is my current curriculum:\n<curriculum>\n{current}\n</curriculum>\n\n"
        f"{measured}"
        f"Revise it: {changes}\n\n"
        "Keep everything I did not ask to change. Ask a question only if my request is ambiguous. "
        "Check the result against the review list below, then reply with <changes>a short bullet "
        "list of what you changed and why</changes> followed by <curriculum>the complete revised "
        f"curriculum file</curriculum>.\n\n{REVIEW_CHECKLIST}"
    )


def convert_request(source: str) -> str:
    """`coursecrafter convert`: a curriculum made elsewhere, rewritten in the format."""
    return (
        f"I planned a course elsewhere. Here it is, in my own format:\n\n{source}\n"
        "Convert it into the curriculum format, faithfully. It is my plan: transcribe it, do not redesign it.\n"
        "- Keep every unit, in my order, with my scope, titles and details. Put my explanations, constraints, "
        "examples and edge cases in each entry's brief, nearly word for word; drop nothing.\n"
        "- Map my structure: a small single-topic exercise is a drill; a larger piece of work built in parts is a "
        "project whose parts are its stages. Material that only explains something is a note revealed with the "
        "unit that needs it. Keep my own ids when they already fit the id rules; otherwise number in my order.\n"
        "- Fill only what the format requires and I did not give: honest `hours` (including testing and "
        "debugging), `concept_difficulty`, `difficulty`, `done_when`, `introduces` and `assumes` from what each "
        "unit teaches and needs, `target`, `scaffold`, `allows` from my restrictions, a project's `minimum`. "
        "When I give a value (hours, order, language, allowed functions), use mine.\n"
        "- Front matter: `title`, `language`, `level`, `rigor` (the level I state, else strict) and, if I gave none, `hours_per_week` and `weeks` chosen "
        "so that the required hours fit with 10 to 15% to spare.\n"
        "- If the validator rejects the result, fix it with the smallest change: complete `introduces` and "
        "`assumes`, add a note for a notion I take for granted, mark a unit `stretch: true` only when I called "
        "it optional. Reorder, split or merge units only when nothing else works, and say so.\n"
        "- Mark my first unit `guided: true` if it is a first-steps walkthrough of the language. Do not add "
        "units I did not plan; if something important is missing (such as a first-steps unit for a language I "
        "have not used, or a drill before a hard stage), suggest it in <changes> instead.\n"
        "- Ask a question only for information you cannot infer from my files and that the format needs.\n"
        "Reply with <changes>a short bullet list of everything you added, interpreted or had to change, and "
        "your suggestions</changes> followed by <curriculum>the complete curriculum file</curriculum>."
    )


def review_request() -> str:
    return (
        f"{REVIEW_CHECKLIST}\n\nReply with <changes>a short bullet list of what you changed and why</changes> "
        "followed by <curriculum>the complete revised curriculum file</curriculum>. If nothing needs to change, "
        "say so in <changes> and send the file unchanged."
    )


# ------------------------------------------------------------------ guided

GUIDED_SYSTEM = r"""You plan programming courses for PiscineBuilder. Talk briefly.

When important information is missing (language, current level, weekly time, number of weeks, final goal, libraries allowed), ask about it, ONE question per reply, so each question can build on the answers so far:
<questions>
[{"question": "What should the final project be?", "options": ["A Unix shell", "A multithreaded HTTP server", "A key-value store with a network protocol"], "recommended": 0}]
</questions>
That is a JSON array holding exactly one question, with 2 to 4 short, concrete options and the index of the one you recommend for this student. Never add an "other" option: the student can always type their own answer. Do not ask what the student already told you, and stop asking as soon as you can plan well.

Otherwise reply with <curriculum>the complete curriculum file</curriculum>.
Ask at most 6 questions in a conversation; after that, assume your recommended options and write the curriculum."""

# --------------------------------------------------------------- generation


def gen_system(langs: list[str]) -> str:
    """The generation rules, with the notes of the course's languages only."""
    from proctor import expr

    def has(lang: str) -> bool:
        return any(x == lang for x in langs)

    function_langs = [lang for lang in ["c", "python", "rust"] if has(lang)]
    s = GEN_INTRO
    if function_langs:
        s += GEN_FUNCTION_TESTS.replace("{langs}", ", ".join(function_langs))
    s += GEN_PROGRAM_TESTS
    s += (
        "Expressions: literals, == != < <= > >= && || ! + - * / %, x.field, x[i], the student's functions, "
        f'run(args=[...], stdin="...") for programs, and only these built-ins: {" ".join(expr.BUILTINS)}. There are no user functions or lambdas.\n'
    )
    s += GEN_RULES
    s += "\n# Language notes\n"
    for lang in langs:
        match lang:
            case "c":
                s += GEN_C
            case "python":
                s += GEN_PYTHON
            case "rust":
                s += GEN_RUST
            case "bash" | "sh" | "shell":
                s += GEN_BASH
            case _:
                s += GEN_OTHER
    if has("c") or has("python"):
        s += GEN_HELPERS
    return s


GEN_INTRO = r"""You write exercises for PiscineBuilder, a hands-on course system. A grader, proctor, checks the student's work offline from one YAML spec per unit and renders the instructions from it, so you produce data only. The student sees the rendered contract, never your tests or your reference solution.

# A good exercise

It practises the unit's skill on a task with a recognizable purpose (a small tool, a data structure, a file format), and its interface lets the student write the concept itself: a unit about types has the student define and use types, never encode them in strings or command lists. The difficulty lies in the concept, not in guessing a format. It builds on the units before it and prepares the ones after.

# Contract (the <unit> block)

YAML with these keys (coursecrafter adds id, title, language, difficulty, concepts, notes, platform: do not write those):
- target: function | program. function: proctor calls the listed functions itself. program: the whole program is run (arguments, stdin, stdout, stderr, exit code).
- goal: one sentence.
- functions: [{name, sig, returns}], the minimal set the student writes, as exact signatures (see the language notes). `returns` carries the exact meaning in one short phrase ("the number of bytes in s", "s with ASCII letters in uppercase"): with the signature, it is the specification. A program target lists the helper functions the unit asks for, otherwise none. A stage lists only the functions it adds.
- files: the expected tree, [{path, rule, note}] with rule = required (default) | one_or_more | free | forbidden; `*` inside a folder, `**` across folders. Listing any rule makes unlisted files unexpected, so a project adds {path: "**", rule: free} unless the layout must be exact. List only what is graded or required: never personal files (notes, scratch tests).
- requirements: the behaviour the signatures and `returns` cannot state. The tests are written later from this contract alone: the brief and the done_when are not passed on, so every decision of theirs that the tests should check (an error format, a limit, how an edge case behaves, an exit-code scheme, a protocol, an output format) must be in `returns` or here. Never paraphrase what the page already shows (signatures, the Allowed and Forbidden lists, examples from the tests) or what the course's RULES.md says (generic edge cases, exact output, linters, time limits). Send them in a <requirements>...</requirements> block after the <unit> block (markdown, NOT inside the YAML): short paragraphs or numbered steps with a sentence of context, names in **bold**, formats in `code`, a small table when there are several cases. At most about 20 lines. Only rely on what this unit's lesson or an earlier unit teaches. Send an empty <requirements></requirements> block when `returns` says it all.
- allowed: what the exercise's code may use besides its own code and the language built-ins. An entry covers everything inside it (`std::io` allows std::io::stdin and the rest of std::io). Restrict only what the brief restricts; when the brief allows the standard library, list it whole. Omit `allowed` when nothing is restricted. Earlier stages' entries carry over: a stage lists only what it adds. The student's own tests in `tests/` are never checked.
- forbidden: items banned even though `allowed` (here or in an earlier stage) covers them, when the brief bans a few things (allowed: [std], forbidden: [std::mem::swap]). Things `allowed` does not cover are banned already: never list them.
- stubs: function target only, where the empty functions are written at unlock; it must fit the files rules.
- environment: one line, only if the student must install or set something unusual.
Style: plain, short sentences, no long dashes. Never reveal the tests, the reference solution or how the checks work inside. Naming the student's commands (`proctor check`, `proctor shell`) is fine when the brief does.

# Tests (the <tests> block)

YAML with `cases` (a block string, one test per line), optional `random`, and `think_about`.
Examples: the instructions show some of your cases as examples. Put a comment line `# example` before two or three ordinary cases (typical inputs, not edge cases); they are shown to the student with their results. A program needs at least one case with an exact `out == "..."`.
think_about: a list of 2 to 4 short edge-case CATEGORIES the student should decide about ("text with no ASCII letters", "the smallest input"), with no quotes, no concrete input from your cases and no expected result. They point at what the hidden tests probe without giving the answers.
Fairness: every case and law must follow from the unit's instructions (signatures, `returns`, requirements, Allowed, examples), the rules of its rigor level below, or what follows unambiguously from them. Never test a behaviour the student could not have known.
"""

GEN_FUNCTION_TESTS = r"""Function target ({langs}), one boolean expression per line:
  str_len("abc") == 3
  parse_port("80") == 80               # a returned Ok(v) or Some(v) is v; None is null
  raises(parse_port("x"))              # the call returns Err, raises or panics
  reverse("abc") == "cba"              # a function returning nothing that modifies a `char *` or `&mut`
                                       # argument evaluates to that argument after the call
"""

GEN_PROGRAM_TESTS = r"""Program target, one run per line:  [args] [@file="content"] [<<< "stdin"] => checks
  checks: out == "..."  out ~ "regex"  out !~ "regex"  out empty  err == / err ~ / err empty  exit N
  <<< "a b\n" => out == "2\n" exit 0
  missing.txt => err ~ "(?i)no such file" exit 2
Strings use \n \t \" escapes. Keep fixed cases small and obviously correct by inspection.

random:
  rounds: 200                 # default; use 20 to 40 for program targets (each round runs processes)
  fuzz:                       # compared with your reference on generated inputs
    - str_len                 # generators derived from the parameter types
    - { fn: str_split, args: { s: 'string "ab," 0..30', sep: "lit ','" } }
    - { name: report, stdin: "lines(log_line) 0..40", argv: 'lit ["-c"]' }   # program target
  laws:                       # invariants that need no reference
    - { name: counts agree, gen: { s: 'string "ab \n" 0..40' }, check: ["word_count(s) == len(words(s))"] }
    - { roundtrip: [encode, decode] }    # decode(encode(x)) == x
    - { idempotent: normalize }          # f(f(x)) == f(x)
Generators: int lo..hi [wN zero-padded], uint, float lo..hi, bool, char "abc", string "alphabet" lo..hi, word, ascii,
list(g) lo..hi, lines(g) lo..hi, opt(g), choice ["a", "b"], lit <value>, and named templates in a <lib> block:
  log_line: { fmt: "{h}:{m} {lvl} {msg}", vars: { h: int 0..23 w2, m: int 0..59 w2, lvl: 'choice ["INFO","WARN"]', msg: word } }
"""

GEN_RULES = r"""Test rules:
- Fuzz what the requirements fully specify; prefer laws for structural truths; generate only valid inputs unless the requirements say how invalid ones behave.
- Every function needs a test that fails when the function is an empty stub.
- Deterministic: no timing-dependent checks, no network, no files outside the working directory.
- For a stage, test only what it adds: the earlier stages' tests run again, unchanged, against this stage's program.

# Reference solution (<file path="..."> blocks)

The complete workspace that passes every test, using only what the contract allows, warning-free under the course's checks (see the language notes). For a stage: the whole project at this stage, built on the previous stage's reference, and still passing the earlier stages' tests.
Optional <scaffold path="..."> blocks: files the student receives at unlock. Never solution code.
"""

GEN_C = r"""C: signatures like "size_t str_len(const char *s)"; harness types: integers, double, bool, char, char *, char **, no structs. stubs: e.g. src/str.c. Built with cc -std=c11 -Wall -Wextra -Werror. `allowed` lists each header and each libc function (keywords and operators are built-ins). Optional `wrap` (a prefix for every run, e.g. "valgrind --leak-check=full --error-exitcode=99", only when the environment has the tool) and `extra_flags` (e.g. [-fsanitize=address] when the environment says it works).
"""

GEN_PYTHON = r"""Python: signatures like "def parse_line(line: str) -> dict | None", named with their module (logstat.parse_line) or with a `module:` key. stubs: the module file, e.g. logstat/__init__.py. `allowed` lists every imported module (built-in functions are implicit). A program target needs `run:`, e.g. "python -m logstat": one command for a whole project.
"""

GEN_RUST = r"""Rust: signatures like "pub fn word_count(text: &str) -> usize" in src/lib.rs (stubs: src/lib.rs). Cases call free functions whose types are integers, f64, bool, char, &str/String, Vec and slices of those, Option, Result<_, E: Debug>, tuples, maps. Anything else (the student's own structs, enums, methods, traits, generics, closures) is tested in Rust: list methods under their type, {name: "Stack::push", sig: "pub fn push(&mut self, x: i32)"} and {name: "Stack::new", sig: "pub fn new() -> Self"}, state the types and their public fields in the requirements, and give in <tests> a `rust_tests` block string of top-level #[test] functions, compiled against the student's crate, which they reach through `use student::*;` (already at the top), with assert!/assert_eq! and no printing. Put a line `// example` above two or three ordinary ones: they are shown to the student. Never encode a type in strings or command lists to suit cases. The prelude is implicit in `allowed`; list std modules (or `std` whole) and crates. A program target runs the built binary: never set `run` or `build`. Reference: the root Cargo.toml is the graded package, with a `[workspace]` table: empty for a single crate; for a project split into several crates, the root package lists the member folders in it (members = ["kit"]) and the member crates have no `[workspace]` of their own. Clean under cargo clippy -D warnings (formatting is applied for you). Crates: nix 0.29 (features such as process, signal, fs, poll, term, user; there is no `unistd` feature, nix::unistd is always there) and libc 0.2.
"""

GEN_BASH = r"""Bash: program target only; `run:` e.g. "bash wc.sh" (one command for a whole project); `allowed` lists the external commands.
"""

GEN_OTHER = r"""Other languages: program target only, built and run by the course settings shown with each request; set `run:` only to override them (one command for a whole project).
"""

GEN_HELPERS = r"""Test helpers (function target, C or Python): test-only functions the student never writes or sees. Declare them in <tests> as `helpers: [{name, sig}]` and give their code in <support path="file.c"> blocks (compiled with the test program; Python: importable modules). C allocation failures, support file:
  static int n = -1; void fail_alloc_after(int k) { n = k; }
  void *sc_malloc(size_t s) { if (n == 0) { n = -1; return NULL; } if (n > 0) n--; return malloc(s); }
with `extra_flags: [-Dmalloc=sc_malloc]` in <tests>, then: then(fail_alloc_after(0), str_dup("x")) == null
"""


def contract_request(context: str, entry: str, project: str, language: str) -> str:
    return f"# Course so far\n{context}\n{project}# Language\n{language}\n\n# Unit to write\n{entry}\n\nReply with only the <unit>...</unit> block holding the contract YAML (plus the <lesson> block when the unit asks for one, and the <requirements> block). Do NOT write tests, reference files or scaffolds now: they are requested separately, once the contract is accepted."


@dataclass(kw_only=True)
class TestsContext:
    """What the tests request carries besides the contract."""

    # Not a pytest test class, despite its name.
    __test__ = False

    previous_ref: str
    lib: str
    language: str
    later_stages: list[str]
    # The earlier stages' tests, as (stage id, YAML).
    earlier_tests: list[tuple[str, str]]
    # Everything the code may use at this stage, earlier stages included.
    allowed: list[str] | None
    forbidden: list[str]
    rigor: str


def text_request(contract: str, entry: str, language: str, cases: str) -> str:
    """`build --text-only`: new teaching text for a unit whose tests stay as they are."""
    return (
        f"# Language\n{language}\n\n# Unit\n{entry}\n# Its contract (tests and reference are final and stay as they are)\n```yaml\n{contract}\n```\n\n"
        f"# Its test cases\n```\n{cases}\n```\n\n"
        "Rewrite only the teaching text of this unit, following the contract rules: "
        "a <lesson>...</lesson> block (as the unit asks; empty if it introduces nothing), "
        "a <requirements>...</requirements> block (markdown: what the test cases check that the functions table, "
        "Allowed, the examples and RULES.md cannot say; empty when there is nothing), and a <think_about>...</think_about> block holding a YAML list of 2 "
        "to 4 edge-case categories from the cases above, without quoting any case or result. Everything must agree "
        "with the test cases. Reply with only these three blocks."
    )


def rigor_rules(rigor: str) -> str:
    """What the hidden tests must cover at each rigor level (matches RULES.md)."""
    match rigor:
        case "relaxed":
            return "relaxed: test the behaviour the unit states and the cases its examples suggest; random inputs stay within the stated ranges."
        case "standard":
            return "standard: beyond the stated behaviour, test the usual edge cases (empty input, one element, zero, a missing trailing newline), the invalid input the unit describes, and random inputs against the reference."
        case _:
            return "strict: beyond the stated behaviour, test every edge case (empty input, one element, zero, the limits of the types involved, text outside ASCII, a missing trailing newline), adversarial input (very long text, large counts, malformed input: a program must never crash, panic or hang), random inputs against the reference, and, where a needlessly slow algorithm is the likely mistake, a law with `time_ms` on large input."


def tests_request(unit_yaml: str, cx: TestsContext) -> str:
    previous_ref = cx.previous_ref
    lib = cx.lib
    language = cx.language
    later_stages = cx.later_stages
    s = f"# Language\n{language}\n\n# Unit contract\n```yaml\n{unit_yaml}\n```\n"
    if cx.allowed is not None:
        lst = cx.allowed
        inside = "nothing more" if not lst else ", ".join(f"`{a}`" for a in lst)
        forbidden = (
            ""
            if not cx.forbidden
            else f" Forbidden even so: {', '.join(f'`{a}`' for a in cx.forbidden)}."
        )
        s += f"\n# What the reference may use\nIn its code (not in `tests/`): its own code, the language built-ins and anything inside {inside}. Nothing else.{forbidden}\n"
    s += f"\n# Rigor\n{rigor_rules(cx.rigor)}\nThe contract wins over these defaults: when its requirements or lesson relax one (a slow algorithm is expected, an input is guaranteed valid), follow the contract.\n"
    if cx.earlier_tests:
        s += "\n# Tests of the earlier stages\nThey run again, unchanged, against this stage's program, which must still pass them all:\n"
        for id, yaml in cx.earlier_tests:
            s += f"## {id}\n```yaml\n{trim_end(yaml)}\n```\n"
    if later_stages:
        listed = "\n".join(f"- {stage}" for stage in later_stages)
        s += f"\n# Later stages\nThese tests run again, unchanged, at every later stage of this project, against the later program:\n{listed}\nSo test only behaviour that stays true in all of them. Never test that a feature is missing or unsupported, the output for input a later stage gives a meaning to, or a usage message a later stage may change.\n"
    if trim(lib):
        s += f"\n# Named generators already in lib.yaml\n```yaml\n{lib}\n```\n"
    if previous_ref:
        s += f"\n# Reference of the previous stage (build on it)\n{previous_ref}\n"
    s += '\nReply with the <tests> block (cases, random, think_about, and optional rust_tests / helpers / extra_flags / wrap), an optional <lib> block (new named generators only), every reference <file path="..."> block, optional <support path="..."> blocks for helpers, and optional <scaffold path="..."> blocks. Nothing else.'
    return s


def language_request(lang: str) -> str:
    return (
        f"proctor has no preset for `{lang}`. Write its course-level settings as YAML inside <language>...</language>:\n"
        "name: display name\nbuild: optional command that compiles or syntax-checks the program\nrun: command that runs the program\n"
        "imports: [regexes whose group 1 captures an imported module or library name]\nextensions: [source file extensions, no dot]\n"
        "comment: line comment marker\n"
        "Use one entry-file convention for the whole course (for example main.{ext}) in build and run. Commands run in the "
        "student's project folder without a shell. Reply with only the block."
    )


def note_request(title: str, teaches: list[str], brief: str) -> str:
    return (
        f'Write the note "{title}" for a course student. It teaches: {", ".join(teaches)}. {brief}\n'
        f"Markdown, at most 25 lines, starting with `# Note: {title}`, concrete with one small example. "
        "Never use long dashes. Reply with only <note>...</note>."
    )


def feedback(problems: str, what: str) -> str:
    return f"The quality gate rejected your answer:\n\n{problems}\n\nFix every point and send {what} again, complete, in the same format."


def tests_retry(problems: str) -> str:
    """After a gate failure: fix the error, change nothing else."""
    return (
        f"The quality gate rejected it:\n\n{problems}\n\nFix exactly this and change nothing else. Send only what changes: "
        'each corrected file, complete, in its <file path="..."> block (files you do not send stay as they are; '
        "an empty block deletes a file), and <tests>, <lib>, <support> or <scaffold> only if that part itself must change."
    )
