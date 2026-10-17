//! Prompt text. Stable parts are constants placed first in every request, so
//! providers with prompt caching only bill them once per run.

pub const PLAN_FORMAT: &str = r#"# levelup curriculum format

A curriculum is ONE markdown file. Output only that file.

## 1. Front matter (required)

---
title: <course title>
language: <default language: c, python, rust, bash, go, javascript, any name>
level: <beginner | intermediate | advanced>
hours_per_week: <number>
environment: <optional: where students will work if not on the build machine, e.g. "Ubuntu 24.04, gcc, valgrind, make">
---

## 2. Entries, in teaching order

Each entry is a heading `## <id>: <Title>` (`### ` for project stages), then
`key: value` lines in YAML with NO blank line inside, then an optional free-text
brief (scope, ideas, pitfalls to target) for the exercise writer.

Ids:
- `dNN-slug`   drill: small, one concept, one sitting.
- `pNN-slug`   project: app-sized, built in stages. Its stages follow it.
- `pNN.k-slug` stage k of project pNN, heading `###`, k = 1, 2, 3... The last stage is the finale.
- `note-slug`  note: half a page explaining an easy notion, revealed together with a unit.
Numbers grow in teaching order; slugs are lowercase words joined by hyphens.

Keys of a drill or stage:
- goal: one sentence, what the student practises (required)
- difficulty: 1 to 5 (required)
- introduces: [concepts and tools this unit teaches]
- assumes: [concepts and tools it needs]
- target: function | program
- scaffold: none | minimal | full
- allows: [libraries, modules or functions the student may use besides their own code]
- gradable: false   (only if no program can check the result)
- language: <only if different from the default>
Keys of a project: goal, difficulty, assumes, language.
Keys of a note: teaches: [...], with: <id of the drill or stage it is revealed with>.

## 3. Rules

- Teach before use. Every item of `assumes` must appear in the `introduces` of an
  EARLIER unit, or in the `teaches` of a note revealed with this unit or earlier.
  This covers tools too: a Makefile, a debugger, a build system, a test framework.
- A note is only for notions a short page can explain (what a Makefile is). Anything
  that needs practice gets its own drill or stage.
- target: function when the exercise is about writing given functions (the grader
  calls them with its own main; available for c, python and rust only). program when
  the whole program is graded (arguments, output, exit code), and always for other
  languages.
- scaffold: none when creating the files and folders is part of the lesson (shell or
  filesystem basics); minimal by default; full only for the very first drills. Big
  projects use minimal: the student organizes the project.
- Size: a drill or a stage fits in one sitting. Split bigger topics. Projects have
  2 to 10 stages and difficulty grows gradually across the course.
- Alternate drills and projects: a project integrates the drills before it.
- gradable: false only when nothing can be checked by a program (visual design,
  essays). Prefer exercises with checkable results.
- The student may use documentation and forums but no AI; exercises must be
  doable that way.
"#;

pub const PLAN_EXAMPLE: &str = r#"---
title: C from scratch
language: c
level: beginner
hours_per_week: 15
---

## d01-hello: Hello, compiler
goal: Compile and run a first program that prints to standard output.
difficulty: 1
introduces: [compiling with cc, main, printf]
target: program
scaffold: full

## d02-strings: Strings by hand
goal: Re-implement basic string helpers with pointers.
difficulty: 2
introduces: [pointers, NUL termination, malloc]
assumes: [compiling with cc]
target: function
allows: [malloc, free]

Focus on off-by-one errors and empty strings.

## note-make: What a Makefile is
teaches: [make]
with: p01.1-build

## p01-wc: Mini wc
goal: Build a small clone of `wc` that counts lines, words and bytes.
difficulty: 3
assumes: [pointers]

### p01.1-build: Project setup
goal: Organize the project with a Makefile and count bytes of standard input.
difficulty: 2
assumes: [make, main]
introduces: [multi-file projects, reading stdin]
target: program

### p01.2-finale: Words and lines
goal: Add word and line counts and file arguments.
difficulty: 3
assumes: [reading stdin]
introduces: [argv, file I/O]
target: program
"#;

pub fn browser_prompt(
    lang: Option<&str>,
    level: Option<&str>,
    hours: Option<&str>,
    env: &str,
) -> String {
    let mut wishes = String::new();
    if let Some(l) = lang {
        wishes.push_str(&format!("- Language: {l}\n"));
    }
    if let Some(l) = level {
        wishes.push_str(&format!("- My level: {l}\n"));
    }
    if let Some(h) = hours {
        wishes.push_str(&format!("- Time: about {h} hours per week\n"));
    }
    if wishes.is_empty() {
        wishes.push_str("- (Ask me what I want to learn, my level and my weekly time.)\n");
    }
    format!(
        "You are helping me plan an intensive, hands-on programming course: \
         short exercises (drills) and bigger projects built in stages, each graded by \
         an automatic checker. Ask me clarifying questions first if anything important \
         is missing, then propose the curriculum and refine it with me. When I say it \
         is final, output the complete curriculum in the exact format below, as one \
         markdown code block, with nothing else.\n\nWhat I want:\n{wishes}\n## My machine (detected; plan only with tools listed as available, or tell me what to install)\n\n{env}\n{PLAN_FORMAT}\n## 4. Example\n\n{PLAN_EXAMPLE}"
    )
}

// ------------------------------------------------------------------ guided

pub const GUIDED_SYSTEM: &str = "You plan programming courses for levelup. Talk briefly. \
When important information is missing (language, level, weekly time, goal, size of the final project), \
reply with <questions>a numbered list of at most 4 short questions, each with a suggested default</questions>. \
Otherwise reply with <curriculum>the complete curriculum file</curriculum>. \
Ask questions at most twice in a conversation; after that, assume sensible defaults and write the curriculum.";

// --------------------------------------------------------------- generation

pub const GEN_SYSTEM: &str = r#"You write exercises for levelup, an intensive, hands-on course system. A grader called skillcheck checks the student's work offline: it reads a YAML spec per unit and renders the instructions from it, so you only produce data, never the instruction markdown. The student never sees your tests or reference solution, only the rendered contract.

# Unit contract (the <unit> block)

YAML with these keys (the tool adds id, title, language, difficulty, concepts, notes):
- target: function | program
    function: skillcheck compiles/imports the student's code and calls the functions itself (c, python, rust only).
    program: the student's program is run as a whole (args, stdin, stdout, stderr, exit code).
- goal: one sentence.
- functions: list of {name, sig, returns}. The MINIMAL set the student must write, as exact signatures:
    c:      "size_t str_len(const char *s)". Harness types: integers, double, bool, char, char *, char **. No structs.
    python: "def parse_line(line: str) -> dict | None"; name is dotted with the module: logstat.parse_line.
    rust:   "pub fn word_count(text: &str) -> usize"; free functions in src/lib.rs. Types: integers, f64, bool, char,
            &str/String, Vec/slices of those, Option, Result<_, E: Debug>, tuples, maps. No custom structs in signatures.
    For a program target, list functions only if their presence should be checked; usually none or `main`.
    For a project stage, list only the functions this stage adds; earlier ones stay required automatically.
- files: the "soft" expected tree: list of {path, rule, note}; rule = required (default) | one_or_more | free | forbidden.
    Globs: * inside a folder, ** across folders. Listing any rule makes every unlisted file unexpected, so for
    projects add {path: "**", rule: free} unless the layout must be exact. Build output is ignored automatically.
- requirements: at most 6 short bullets of BEHAVIOUR: edge cases, formats, errors, exit codes. Every behaviour your
    tests check must be stated here or follow directly from the functions table. Never restate the table.
- allowed: strict whitelist of what the student may use besides their own code. Language built-ins are implicit
    and never listed: C keywords and operators (NOT headers or libc functions: list each header and each function),
    Python built-in functions (list every module, e.g. sys, re, collections), Rust prelude (list std paths like
    std::collections::HashMap and any crate). Stages include the previous stage's list. Omit `allowed` only if
    the exercise genuinely has no restriction.
- stubs: path where skillcheck writes empty bodies of the functions when the unit unlocks (function target). Must
    fit the files rules. python: the module file, e.g. logstat/__init__.py. rust: src/lib.rs. c: e.g. src/str.c.
- run: program target only, when the language has no default: how to run it, e.g. "python -m logstat", "bash wc.sh".
- module: python only, if function names are not dotted.
- environment: one line, only if the student must install or set something unusual.
- wrap: prefix for every run of the student's code, e.g. "valgrind --leak-check=full --error-exitcode=42" for leak checks.
    Only when the student environment has the tool. A run whose wrapper reports a problem fails.
- extra_flags: compiler flags added for this unit (C), e.g. [-fsanitize=address] when the environment says it works,
    or [-Dmalloc=sc_malloc] together with an allocation shim (see helpers).
Writing style: plain, short sentences. Never use long dashes. Never mention tests, the grader's internals, or AI.

# Tests (the <tests> block)

YAML with `cases` (a block string, one test per line) and optional `random`.

Function target, one boolean expression per line:
  str_len("abc") == 3
  logstat.parse_line("x") == null
  raises(parse(""))                     # the call must raise / return Err / panic
  reverse("abc") == "cba"               # C/Rust: a function returning nothing that modifies a `char *`
                                        # (not const) or `&mut` argument evaluates to that argument after the call
Program target, one run per line:  [args] [@file="content"] [<<< "stdin"] => checks
  checks: out == "..."  out ~ "regex"  out !~ "regex"  out empty  err == / err ~ / err empty  exit N
  <<< "a b\n" => out == "2\n" exit 0
  missing.txt => err ~ "(?i)no such file" exit 2
Strings use \n \t \" escapes. Keep fixed cases small and obviously correct by inspection.

random:
  rounds: 200                 # default; use 20-40 for program targets (each round runs processes)
  fuzz:                       # compare with your reference solution on generated inputs
    - str_len                 # generators derived from parameter types
    - { fn: str_split, args: { s: 'string "ab," 0..30', sep: "lit ','" } }
    - { name: report, stdin: "lines(log_line) 0..40", argv: 'lit ["-c"]' }   # program target
  laws:                       # invariants that need no reference
    - { name: counts agree, gen: { s: 'string "ab \n" 0..40' }, check: ["word_count(s) == len(words(s))"] }
    - { roundtrip: [encode, decode] }    # decode(encode(x)) == x
    - { idempotent: normalize }          # f(f(x)) == f(x)
    - { name: padding is ignored, gen: { log: "lines(log_line) 0..20" }, check: ['run(stdin=log).out == run(stdin="\n" + log).out'] }

Generators: int lo..hi [wN zero-padded], uint, float lo..hi, bool, char "abc", string "alphabet" lo..hi, word, ascii,
list(g) lo..hi, lines(g) lo..hi, opt(g), choice ["a", "b"], lit <value>, and templates in the <lib> block:
  log_line: { fmt: "{h}:{m} {lvl} {msg}", vars: { h: int 0..23 w2, m: int 0..59 w2, lvl: 'choice ["INFO","WARN"]', msg: word } }
Expressions: literals, == != < <= > >= && || ! + - * / %, x.field, x[i], the student's functions, run(args=[...], stdin="...")
for programs, and only these built-ins: len bytes sorted reversed sum min max abs count contains join split words lines lower
upper trim chars unique keys values get all any int float str is_sorted permutation repeat range slice starts_with ends_with
matches replace nonempty concat flatten approx raises then. `then(a, b)` evaluates a, then b, and returns b.
There are no user functions or lambdas.

Test helpers (function target, C or Python): test-only functions the student never writes or sees. Declare them in
the <tests> block as `helpers: [{name, sig}]` and give their code in <support path="file.c"> blocks (compiled with the
test program; Python: importable modules). Typical C use, allocation failures: support file
  static int n = -1; void fail_alloc_after(int k) { n = k; }
  void *sc_malloc(size_t s) { if (n == 0) { n = -1; return NULL; } if (n > 0) n--; return malloc(s); }
with `extra_flags: [-Dmalloc=sc_malloc]` in the unit, then: then(fail_alloc_after(0), str_dup("x")) == null

Test rules:
- Fuzz what the requirements fully specify; prefer laws for structural truths; generate only valid inputs
  unless the requirements say how invalid ones behave.
- Every function needs at least one test that fails when the function is an empty stub (returns 0, NULL, None, or panics).
- Deterministic: no timing-dependent checks, no network, no files outside the working directory.
- For a stage, test only what this stage adds; earlier stages are re-run automatically.

# Reference solution (<file path="..."> blocks)

The complete workspace that passes every test: every file the files rules require, using only the allowed list,
warning-free under the course's checks (C: -std=c11 -Wall -Wextra -Werror; Rust: rustfmt-formatted, clippy -D warnings
clean, with a Cargo.toml whose package name matches and that contains an empty `[workspace]` table). For a stage,
the whole project at this stage, building on the previous stage's reference.
Optional <scaffold path="..."> blocks: files the student receives at unlock (e.g. a Rust Cargo.toml). Never put
solution code in a scaffold.
"#;

pub fn contract_request(context: &str, entry: &str, project: &str, language: &str) -> String {
    format!(
        "# Course so far\n{context}\n{project}# Language\n{language}\n\n# Unit to write\n{entry}\n\nReply with only the <unit>...</unit> block."
    )
}

pub fn tests_request(unit_yaml: &str, previous_ref: &str, lib: &str, language: &str) -> String {
    let mut s = format!("# Language\n{language}\n\n# Unit contract\n```yaml\n{unit_yaml}\n```\n");
    if !lib.trim().is_empty() {
        s.push_str(&format!(
            "\n# Named generators already in lib.yaml\n```yaml\n{lib}\n```\n"
        ));
    }
    if !previous_ref.is_empty() {
        s.push_str(&format!(
            "\n# Reference of the previous stage (build on it)\n{previous_ref}\n"
        ));
    }
    s.push_str("\nReply with the <tests> block (cases, random, and optional helpers / extra_flags / wrap), an optional <lib> block (new named generators only), every reference <file path=\"...\"> block, optional <support path=\"...\"> blocks for helpers, and optional <scaffold path=\"...\"> blocks. Nothing else.");
    s
}

pub fn language_request(lang: &str) -> String {
    format!(
        "skillcheck has no preset for `{lang}`. Write its course-level settings as YAML inside <language>...</language>:\n\
         name: display name\nbuild: optional command that compiles or syntax-checks the program\nrun: command that runs the program\n\
         imports: [regexes whose group 1 captures an imported module or library name]\nextensions: [source file extensions, no dot]\n\
         comment: line comment marker\n\
         Use one entry-file convention for the whole course (for example main.{{ext}}) in build and run. Commands run in the \
         student's project folder without a shell. Reply with only the block."
    )
}

pub fn note_request(title: &str, teaches: &[String], brief: &str) -> String {
    format!(
        "Write the note \"{title}\" for a course student. It teaches: {}. {brief}\n\
         Markdown, at most 25 lines, starting with `# Note: {title}`, concrete with one small example. \
         Never use long dashes. Reply with only <note>...</note>.",
        teaches.join(", ")
    )
}

pub fn feedback(problems: &str, what: &str) -> String {
    format!(
        "The quality gate rejected your answer:\n\n{problems}\n\nFix every point and send {what} again, complete, in the same format."
    )
}
