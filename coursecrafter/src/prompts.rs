//! Prompt text. Stable parts are constants placed first in every request, so
//! providers with prompt caching only bill them once per run.

pub const PLAN_FORMAT: &str = r#"# PiscineBuilder curriculum format

A curriculum is ONE markdown file. Output only that file.

## 1. Front matter (required)

---
title: <course title>
language: <default language: c, python, rust, bash, go, javascript, any name>
level: <beginner | intermediate | advanced>
hours_per_week: <number>
weeks: <number>
environment: <optional: where students will work if not on the build machine, e.g. "Ubuntu 24.04, gcc, valgrind, make">
---

## 2. Entries, in teaching order

Each entry is a heading `## <id>: <Title>` (`### ` for project stages), then
`key: value` lines in YAML with NO blank line inside (quote a value that starts with a backtick
or contains `: `), then an optional free-text
brief (scope, design decisions, pitfalls to target) for the exercise writer.

Ids:
- `dNN-slug`   drill: small, one concept, one sitting.
- `pNN-slug`   project: app-sized, built in stages. Its stages follow it.
- `pNN.k-slug` stage k of project pNN, heading `###`, k = 1, 2, 3... The last required stage is the finale.
- `note-slug`  note: half a page explaining an easy notion, revealed together with a unit.
Numbers grow in teaching order; slugs are lowercase words joined by hyphens.

Keys of a drill or stage:
- goal: one sentence, what the student practises (required)
- concept_difficulty: 1 to 5, how hard the ideas are to understand (required)
- difficulty: 1 to 5, how hard it is to implement and debug correctly (required)
- hours: estimated hours of implementation, testing and debugging (required; for planning only, never shown to the student)
- done_when: one line, the exit criterion: the observable behaviour showing the essential skills are there
  (the tests are written from it). Optional details are not part of it, so the student can move on once it holds.
- introduces: [concepts and tools this unit teaches]
- assumes: [concepts and tools it needs]
- target: function | program
- scaffold: none | minimal | full
- allows: [libraries, modules or functions the student may use besides their own code]
- stretch: true   (optional unit: the student may skip it)
- platform: linux   (only when the unit needs Linux APIs: fork, exec, pipes, signals, process groups,
  /proc, Unix permissions; such units are built and tested in a Linux container on other systems)
- gradable: false   (only if no program can check the result)
- language: <only if different from the default>
Keys of a project: goal, difficulty, assumes, language, platform, and minimum (required): one line, what the
project must do to count as a success. Its brief states the scope: what is in, what is out.
Keys of a note: teaches: [...], with: <id of the drill or stage it is revealed with>.

## 3. Rules

Prerequisites
- Teach before use. Every item of `assumes` must appear in the `introduces` of an
  EARLIER unit, or in the `teaches` of a note revealed with this unit or earlier.
  This covers tools too: a Makefile, a debugger, a build system, a test framework.
- `assumes` lists EVERYTHING a unit needs, including basics that are easy to forget:
  control flow, the core collection types, printing and debug output, formatting
  traits or helpers, common annotations. The first drills introduce these
  explicitly, so later units can name them.
- Covered, not just introduced: before a project stage relies on a concept, the student
  has practised it in a graded drill or an earlier stage. A note can explain an easy
  notion, but a hard stage never relies on something only read about.
- A note is only for notions a short page can explain (what a Makefile is). Anything
  that needs practice gets its own drill or stage. Principles that recur (resource
  cleanup, error propagation) are repeated in the briefs where they apply, not taught once.

Progression
- Never jump from first meeting a concept to building a major feature with it. Before
  each project stage that needs a hard concept, a small drill checks the student
  understands it. A stage introduces at most two new concepts.

Testing and debugging
- Introduce the language's unit-test tool within the first three units. From then on,
  every project stage's brief asks the student to ship tests with the code, and to add
  a regression test for every bug they fix. Testing and debugging time is part of
  every stage's `hours`, not a separate unit.

Purpose
- Every drill serves a later unit: what it introduces appears in a later `assumes`.
  A topic nothing uses is cut, folded into another unit, or marked `stretch: true`.
- Advanced or low-level topics (unsafe code, FFI, concurrency, metaprogramming) only
  when a later unit needs them, and scoped to that need.
- The aim is a student who can explain, debug and extend their code, not one who
  ticked every feature. Goals and briefs name skills, not feature counts.

Workload
- Estimate `hours` honestly for this student, including testing and debugging. Topics
  that are routinely underestimated get more time or a higher difficulty: memory and
  ownership models, data-structure and AST design, parsing, unsafe code and FFI,
  processes, pipes and signals, concurrency. For those, take your first guess and add
  half to all of it.
- Required hours (stretch goals excluded) leave at least 10 to 15% of
  hours_per_week x weeks free, for debugging and revisiting concepts. If the scope does
  not fit, reduce the required scope or suggest more weeks; never squeeze estimates.
- Students progress unevenly. Do not plan a schedule where every hour must go to plan.
- A finale is ONE milestone. If it covers two hard topics, split it into a required
  stage and a stretch stage.
- Size: a drill or a stage fits in one to two sittings (at most about 12 hours).

Minimum viable project and stretch goals
- Every project states its `minimum`: what it must do to count as a success. The
  required stages deliver exactly that and nothing more; extra features are stretch
  stages. This prevents feature creep and is the fallback when time runs short.
- Stretch stages come after the required finale and are attempted only once the
  required work is stable. They are not counted in the budget. Nothing required may
  assume what only a stretch goal introduces. The first stage of a project is never a stretch goal.
- Plan for consolidation: the last required unit of the course is a consolidation stage
  of about 5 to 10% of the budget: refactor, improve the tests, document the known
  limitations in a README, and revisit the concepts that caused the most friction.
  Its done_when: the tests still pass and the README lists the limitations.
- Weekly review checkpoints are added automatically by the grader; do not add units for them.

Difficulty
- Track both scales. concept_difficulty is how hard the idea is to understand;
  difficulty is how hard it is to implement and debug. They often differ: spawning a
  process is easy to understand (concept 2) but a correct pipeline is hard to build
  (difficulty 4).
- Use the whole 1 to 5 scale so it carries information. 5 is rare: one or two units
  in a whole course.
Design decisions
- Look ahead. When a later stage depends on a representation chosen earlier, say so in
  the earlier stage's brief (for example: tokens keep their quoting, because a later
  expansion stage needs it).
- All stages of a project build one program (or library) that each stage extends: the
  tests of every earlier stage run again at each later stage. Separate small programs are
  drills. A stage never specifies what it does with a feature a later stage adds (no
  `not supported yet` messages): that behaviour is left untested until the stage adding it.
- Decide in the brief whatever drives the design: the project scope, the grammar of a
  parser or protocol (written before the parser stage), the output order of concurrent
  programs, and the edge cases (empty input, missing trailing newline, unreadable
  files, invalid encodings).

Fit
- target: function when the exercise is about writing given functions (the grader
  calls them with its own main; available for c, python and rust only). program when
  the whole program is graded (arguments, output, exit code), and always for other
  languages.
- scaffold: none when creating the files and folders is part of the lesson (shell or
  filesystem basics); minimal by default; full only for the very first drills. Big
  projects use minimal: the student organizes the project.
- Operating-system features only when the student's environment provides them; if
  they don't, choose an alternative or make the unit a stretch goal.
- Portable by default: only units that need Linux APIs get `platform: linux` (set it on a
  project when all its stages need it). Once a stage of a project needs Linux, every later
  stage does too, since earlier stages are tested again with it.
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
weeks: 2
---

## d01-hello: Hello, compiler
goal: Compile and run a first program that prints to standard output.
concept_difficulty: 1
difficulty: 1
hours: 1.5
done_when: the program prints its greeting and exits with status 0
introduces: [compiling with cc, main, printf, if and loops]
target: program
scaffold: full

## d02-strings: Strings by hand
goal: Re-implement basic string helpers with pointers.
concept_difficulty: 3
difficulty: 2
hours: 5
done_when: the helpers match the standard functions on empty, short and long strings
introduces: [pointers, NUL termination, malloc, assert-based tests]
assumes: [compiling with cc, if and loops]
target: function
allows: [malloc, free]

Focus on off-by-one errors and empty strings. Write assert-based tests for each helper.

## d03-read-loop: Reading input
goal: Read standard input byte by byte and handle end of file.
concept_difficulty: 2
difficulty: 2
hours: 2
done_when: the program echoes its input exactly, including a missing trailing newline
introduces: [reading stdin, EOF]
assumes: [main, if and loops]
target: program

A small check before the project: the first stage builds on this read loop.

## note-make: What a Makefile is
teaches: [make]
with: p01.1-build

## p01-wc: Mini wc
goal: Build a small clone of `wc` that counts lines, words and bytes.
minimum: prints line, word and byte counts of stdin and of file arguments, like `wc`
difficulty: 3
assumes: [pointers, reading stdin]

In scope: stdin, file arguments, the three counts. Out of scope: GNU flags, locales.

### p01.1-build: Project setup
goal: Organize the project with a Makefile and count bytes of standard input.
concept_difficulty: 2
difficulty: 2
hours: 3.5
done_when: "`make` builds the binary and it prints the byte count of stdin"
assumes: [make, reading stdin, assert-based tests]
introduces: [multi-file projects]
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

Revisit pointers and EOF handling, the concepts that caused the most friction.

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

Only once the required stages are stable.
"#;
/// The planning review: a second look at a draft, by the planner itself.
pub const REVIEW_CHECKLIST: &str = r#"Review the curriculum as a demanding teacher would, then fix it:
1. Prerequisites: for every unit, list what it really needs, basics included (control
   flow, collections, printing and debug output, formatting, annotations). Anything not
   introduced earlier is a gap. Anything a hard stage relies on must have been practised
   in a graded unit, not only introduced or explained in a note.
2. Progression: before each project stage that needs a hard concept, is there a small
   drill that checks the concept first? Does any stage introduce more than two new concepts?
3. Testing: is the test tool introduced in the first three units, do project briefs ask
   for tests and regression tests, and do the `hours` include testing and debugging?
4. Purpose: does every drill feed a later unit? Cut, merge or mark stretch the ones that don't.
5. Workload: are the `hours` honest for this student? Are the routinely underestimated
   topics (memory and ownership, AST and data-structure design, parsing, unsafe and FFI,
   processes, pipes, signals, concurrency) given enough time? Do required hours leave
   10 to 15% of the budget free? If not, cut required scope or suggest more weeks.
6. Minimum viable projects: does every project state its `minimum`, and do its required
   stages deliver exactly that? Are extra features stretch stages placed after the
   required finale?
7. Consolidation: does the course end with a consolidation stage (refactor, tests,
   documented limitations, revisiting the hardest concepts)?
8. Exit criteria: does each `done_when` describe the essential skills only, so the
   student can move on while optional details remain?
9. Difficulty: are concept_difficulty and difficulty both set and honest, with the scale
   spread out and 5 kept for one or two units?
10. Design: for every project, are the scope, the grammar or format, the edge cases and
    the decisions later stages depend on written in the briefs?
11. Fit: does every unit only need what the student's environment provides?
12. Missing concepts: what will the final project need that no unit teaches?"#;
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
        "You are helping me plan an intensive, hands-on programming course: short \
         exercises (drills) and bigger projects built in stages, each graded by an \
         automatic checker. Ask me clarifying questions first if anything important is \
         missing, one at a time, each with a few lettered options and the one you \
         recommend. Then propose the curriculum and refine it with me. Before showing \
         any version, check it against the review list below and fix what it finds. \
         When I say it is final, output the complete curriculum in the exact format \
         below, as one markdown code block, with nothing else.\n\nWhat I want:\n{wishes}\n\
         ## My machine (detected; plan only with tools listed as available, or tell me what to install)\n\n{env}\n\
         {PLAN_FORMAT}\n## 4. Example\n\n{PLAN_EXAMPLE}\n## 5. Review list\n\n{REVIEW_CHECKLIST}\n"
    )
}

/// `coursecrafter revise`: the current file plus the student's requested changes.
pub fn revise_request(current: &str, changes: &str) -> String {
    format!(
        "Here is my current curriculum:\n<curriculum>\n{current}\n</curriculum>\n\n\
         Revise it: {changes}\n\n\
         Keep everything I did not ask to change. Ask a question only if my request is ambiguous. \
         Check the result against the review list below, then reply with <changes>a short bullet \
         list of what you changed and why</changes> followed by <curriculum>the complete revised \
         curriculum file</curriculum>.\n\n{REVIEW_CHECKLIST}"
    )
}

pub fn review_request() -> String {
    format!(
        "{REVIEW_CHECKLIST}\n\nReply with <changes>a short bullet list of what you changed and why</changes> \
         followed by <curriculum>the complete revised curriculum file</curriculum>. If nothing needs to change, \
         say so in <changes> and send the file unchanged."
    )
}

// ------------------------------------------------------------------ guided

pub const GUIDED_SYSTEM: &str = r#"You plan programming courses for PiscineBuilder. Talk briefly.

When important information is missing (language, current level, weekly time, number of weeks, final goal, libraries allowed), ask about it, ONE question per reply, so each question can build on the answers so far:
<questions>
[{"question": "What should the final project be?", "options": ["A Unix shell", "A multithreaded HTTP server", "A key-value store with a network protocol"], "recommended": 0}]
</questions>
That is a JSON array holding exactly one question, with 2 to 4 short, concrete options and the index of the one you recommend for this student. Never add an "other" option: the student can always type their own answer. Do not ask what the student already told you, and stop asking as soon as you can plan well.

Otherwise reply with <curriculum>the complete curriculum file</curriculum>.
Ask at most 6 questions in a conversation; after that, assume your recommended options and write the curriculum."#;

// --------------------------------------------------------------- generation

/// The generation rules, with the notes of the course's languages only.
pub fn gen_system(langs: &[String]) -> String {
    let has = |l: &str| langs.iter().any(|x| x == l);
    let function_langs: Vec<&str> = ["c", "python", "rust"]
        .into_iter()
        .filter(|l| has(l))
        .collect();
    let mut s = String::from(GEN_INTRO);
    if !function_langs.is_empty() {
        s.push_str(&GEN_FUNCTION_TESTS.replace("{langs}", &function_langs.join(", ")));
    }
    s.push_str(GEN_PROGRAM_TESTS);
    s.push_str(&format!(
        "Expressions: literals, == != < <= > >= && || ! + - * / %, x.field, x[i], the student's functions, \
         run(args=[...], stdin=\"...\") for programs, and only these built-ins: {}. There are no user functions or lambdas.\n",
        proctor::expr::BUILTINS.join(" ")
    ));
    s.push_str(GEN_RULES);
    s.push_str("\n# Language notes\n");
    for l in langs {
        s.push_str(match l.as_str() {
            "c" => GEN_C,
            "python" => GEN_PYTHON,
            "rust" => GEN_RUST,
            "bash" | "sh" | "shell" => GEN_BASH,
            _ => GEN_OTHER,
        });
    }
    if has("c") || has("python") {
        s.push_str(GEN_HELPERS);
    }
    s
}

const GEN_INTRO: &str = r#"You write exercises for PiscineBuilder, a hands-on course system. A grader, proctor, checks the student's work offline from one YAML spec per unit and renders the instructions from it, so you produce data only. The student sees the rendered contract, never your tests or your reference solution.

# Contract (the <unit> block)

YAML with these keys (coursecrafter adds id, title, language, difficulty, concepts, notes, platform):
- target: function | program. function: proctor calls the listed functions itself. program: the whole program is run (arguments, stdin, stdout, stderr, exit code).
- goal: one sentence.
- functions: [{name, sig, returns}], the minimal set the student writes, as exact signatures (see the language notes). A program target usually lists none. A stage lists only the functions it adds.
- files: the expected tree, [{path, rule, note}] with rule = required (default) | one_or_more | free | forbidden; `*` inside a folder, `**` across folders. Listing any rule makes unlisted files unexpected, so a project adds {path: "**", rule: free} unless the layout must be exact.
- requirements: at most 6 short bullets of behaviour: edge cases, formats, errors, exit codes. Everything the tests check is stated here or follows from the functions. With a done_when line, the requirements and the tests together establish it.
- allowed: what the exercise's code may use besides its own code and the language built-ins. An entry covers everything inside it (`std::io` allows std::io::stdin and the rest of std::io). Restrict only what the brief restricts; when the brief allows the standard library, list it whole. Omit `allowed` when nothing is restricted. Earlier stages' entries carry over: a stage lists only what it adds. The student's own tests in `tests/` are never checked.
- forbidden: items banned even though `allowed` covers them, when the brief bans a few things (allowed: [std], forbidden: [std::mem::swap]).
- stubs: function target only, where the empty functions are written at unlock; it must fit the files rules.
- environment: one line, only if the student must install or set something unusual.
Style: plain, short sentences, no long dashes. Never reveal the tests, the reference solution or how the checks work inside. Naming the student's commands (`proctor check`, `proctor shell`) is fine when the brief does.

# Tests (the <tests> block)

YAML with `cases` (a block string, one test per line) and optional `random`.
"#;

const GEN_FUNCTION_TESTS: &str = r#"Function target ({langs}), one boolean expression per line:
  str_len("abc") == 3
  parse_port("80") == 80               # a returned Ok(v) or Some(v) is v; None is null
  raises(parse_port("x"))              # the call returns Err, raises or panics
  reverse("abc") == "cba"              # a function returning nothing that modifies a `char *` or `&mut`
                                       # argument evaluates to that argument after the call
"#;

const GEN_PROGRAM_TESTS: &str = r#"Program target, one run per line:  [args] [@file="content"] [<<< "stdin"] => checks
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
"#;

const GEN_RULES: &str = r#"Test rules:
- Fuzz what the requirements fully specify; prefer laws for structural truths; generate only valid inputs unless the requirements say how invalid ones behave.
- Every function needs a test that fails when the function is an empty stub.
- Deterministic: no timing-dependent checks, no network, no files outside the working directory.
- For a stage, test only what it adds: the earlier stages' tests run again, unchanged, against this stage's program.

# Reference solution (<file path="..."> blocks)

The complete workspace that passes every test, using only what the contract allows, warning-free under the course's checks (see the language notes). For a stage: the whole project at this stage, built on the previous stage's reference, and still passing the earlier stages' tests.
Optional <scaffold path="..."> blocks: files the student receives at unlock. Never solution code.
"#;

const GEN_C: &str = r#"C: signatures like "size_t str_len(const char *s)"; harness types: integers, double, bool, char, char *, char **, no structs. stubs: e.g. src/str.c. Built with cc -std=c11 -Wall -Wextra -Werror. `allowed` lists each header and each libc function (keywords and operators are built-ins). Optional `wrap` (a prefix for every run, e.g. "valgrind --leak-check=full --error-exitcode=99", only when the environment has the tool) and `extra_flags` (e.g. [-fsanitize=address] when the environment says it works).
"#;

const GEN_PYTHON: &str = r#"Python: signatures like "def parse_line(line: str) -> dict | None", named with their module (logstat.parse_line) or with a `module:` key. stubs: the module file, e.g. logstat/__init__.py. `allowed` lists every imported module (built-in functions are implicit). A program target needs `run:`, e.g. "python -m logstat": one command for a whole project.
"#;

const GEN_RUST: &str = r#"Rust: signatures like "pub fn word_count(text: &str) -> usize", free functions in src/lib.rs (stubs: src/lib.rs). Types: integers, f64, bool, char, &str/String, Vec and slices of those, Option, Result<_, E: Debug>, tuples, maps. No custom structs, generic parameters, trait bounds or closures in signatures, since the grader calls concrete types: to practise generics, have the student write them and expose concrete wrappers (fn largest_i32(v: &[i32]) -> Option<i32>). The prelude is implicit in `allowed`; list std modules (or `std` whole) and crates. A program target runs the built binary: never set `run` or `build`. Reference: a Cargo.toml whose package name matches and that has an empty `[workspace]` table, clean under cargo clippy -D warnings (formatting is applied for you). Crates: nix 0.29 (features such as process, signal, fs, poll, term, user; there is no `unistd` feature, nix::unistd is always there) and libc 0.2.
"#;

const GEN_BASH: &str = r#"Bash: program target only; `run:` e.g. "bash wc.sh" (one command for a whole project); `allowed` lists the external commands.
"#;

const GEN_OTHER: &str = r#"Other languages: program target only, built and run by the course settings shown with each request; set `run:` only to override them (one command for a whole project).
"#;

const GEN_HELPERS: &str = r#"Test helpers (function target, C or Python): test-only functions the student never writes or sees. Declare them in <tests> as `helpers: [{name, sig}]` and give their code in <support path="file.c"> blocks (compiled with the test program; Python: importable modules). C allocation failures, support file:
  static int n = -1; void fail_alloc_after(int k) { n = k; }
  void *sc_malloc(size_t s) { if (n == 0) { n = -1; return NULL; } if (n > 0) n--; return malloc(s); }
with `extra_flags: [-Dmalloc=sc_malloc]` in <tests>, then: then(fail_alloc_after(0), str_dup("x")) == null
"#;

pub fn contract_request(context: &str, entry: &str, project: &str, language: &str) -> String {
    format!(
        "# Course so far\n{context}\n{project}# Language\n{language}\n\n# Unit to write\n{entry}\n\nReply with only the <unit>...</unit> block holding the contract YAML. Do NOT write tests, reference files or scaffolds now: they are requested separately, once the contract is accepted."
    )
}

/// What the tests request carries besides the contract.
pub struct TestsContext<'a> {
    pub previous_ref: &'a str,
    pub lib: &'a str,
    pub language: &'a str,
    pub later_stages: &'a [String],
    /// The earlier stages' tests, as (stage id, YAML).
    pub earlier_tests: &'a [(String, String)],
    /// Everything the code may use at this stage, earlier stages included.
    pub allowed: Option<&'a [String]>,
    pub forbidden: &'a [String],
}

pub fn tests_request(unit_yaml: &str, cx: &TestsContext) -> String {
    let TestsContext {
        previous_ref,
        lib,
        language,
        later_stages,
        ..
    } = *cx;
    let mut s = format!("# Language\n{language}\n\n# Unit contract\n```yaml\n{unit_yaml}\n```\n");
    if let Some(list) = cx.allowed {
        s.push_str(&format!(
            "\n# What the reference may use\nIn its code (not in `tests/`): its own code, the language built-ins and anything inside {}. Nothing else.{}\n",
            if list.is_empty() {
                "nothing more".to_string()
            } else {
                list.iter()
                    .map(|a| format!("`{a}`"))
                    .collect::<Vec<_>>()
                    .join(", ")
            },
            if cx.forbidden.is_empty() {
                String::new()
            } else {
                format!(
                    " Forbidden even so: {}.",
                    cx.forbidden
                        .iter()
                        .map(|a| format!("`{a}`"))
                        .collect::<Vec<_>>()
                        .join(", ")
                )
            }
        ));
    }
    if !cx.earlier_tests.is_empty() {
        s.push_str("\n# Tests of the earlier stages\nThey run again, unchanged, against this stage's program, which must still pass them all:\n");
        for (id, yaml) in cx.earlier_tests {
            s.push_str(&format!("## {id}\n```yaml\n{}\n```\n", yaml.trim_end()));
        }
    }
    if !later_stages.is_empty() {
        s.push_str(&format!(
            "\n# Later stages\nThese tests run again, unchanged, at every later stage of this project, against the later program:\n{}\nSo test only behaviour that stays true in all of them. Never test that a feature is missing or unsupported, the output for input a later stage gives a meaning to, or a usage message a later stage may change.\n",
            later_stages
                .iter()
                .map(|l| format!("- {l}"))
                .collect::<Vec<_>>()
                .join("\n")
        ));
    }
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
        "proctor has no preset for `{lang}`. Write its course-level settings as YAML inside <language>...</language>:\n\
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

/// After a gate failure: fix the error, change nothing else.
pub fn tests_retry(problems: &str) -> String {
    format!(
        "The quality gate rejected it:\n\n{problems}\n\nFix exactly this and change nothing else. Send only what changes: \
         each corrected file, complete, in its <file path=\"...\"> block (files you do not send stay as they are; \
         an empty block deletes a file), and <tests>, <lib>, <support> or <scaffold> only if that part itself must change."
    )
}
