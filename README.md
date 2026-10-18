# levelup

Intensive, hands-on courses in any language, built once with an LLM and then
practised offline.

- **taskmaster** creates a course (curriculum, unit specs, tests, hidden
  reference solutions). It is the only part that talks to an LLM.
- **skillcheck** is what the student uses. It reveals units one at a time,
  grades the work, and keeps `PROGRESS.md`. No network, no API key, no LLM.

```
cargo build --release      # target/release/skillcheck(.exe)
cd examples/demo-course
skillcheck next            # unlock the first unit
skillcheck check           # from inside the unit's workspace
```

## Creating a course with taskmaster

Two ways to get a curriculum, one format, one validator:

```
# Browser: paste the prompt into any chat assistant, plan there, save its answer.
taskmaster prompt --lang c --level beginner --hours 10 > prompt.txt
taskmaster import curriculum.md --course my-course     # --paste: errors as a block to paste back

# Terminal: a short guided conversation (uses the LLM).
taskmaster new "learn C pointers" --from syllabus.txt --course my-course

# Change an existing curriculum.md through the planner (uses the LLM).
taskmaster revise "add a drill on structs before p01" --course my-course

# Read the whole plan: units, hours, both difficulties, prerequisites, approval (no LLM).
taskmaster show --course my-course          # --full adds briefs, Allowed lists, scaffolds

# Then generate everything (uses the LLM), and start.
taskmaster build --course my-course
cd my-course && skillcheck next
```

`new` asks the planner's questions one at a time (at most 6), as multiple
choice with a recommended option (Enter takes it, a letter picks another, any
other text is your own answer). The planner writes each question after seeing
the previous answers. The planner then reviews its first valid draft against a
checklist (prerequisites, testing introduced early, workload, finales split
into required and stretch stages, difficulty spread, design decisions written
down) and lists what it changed. Before validating, taskmaster fixes mechanical slips itself: drill and project numbers that do not grow, stages numbered out of sequence (references follow the new ids), missing `hours` (filled from the difficulty), and values YAML would misread (a title or list item containing `: `, a leading backtick, a comma inside `HashMap<K, V>`), which get quoted. Fixes and validator problems are logged to `.skillcheck/taskmaster/validator.log`. `import` and `new` show the plan as a table
with hours per unit and a workload summary against the budget, then ask to
approve, ask for changes, edit (opens `$EDITOR`) or quit. `build` refuses an
unapproved or modified curriculum.

The curriculum file is markdown: front matter (`title`, `language`, `level`,
`hours_per_week`, `weeks`), then one `## id: Title` section per drill (`dNN-slug`),
project (`pNN-slug`) or note (`note-slug`), and `### pNN.k-slug` for stages, each
with YAML keys (`goal`, `concept_difficulty` and `difficulty` for understanding and
implementation, `hours`, `done_when` as the exit criterion, `introduces`, `assumes`,
`target`, `scaffold`, `allows`, `stretch`, `gradable`; projects add `minimum`, the
minimum viable result; notes: `teaches`, `with`) and a free-text brief. `taskmaster prompt` prints the full
specification. The validator enforces:

- **Teach before use:** every `assumes` must be introduced by an earlier unit or
  taught by a note revealed by then.
- **Workload:** every unit has `hours` (planning only, never shown to the
  student) and both difficulties; required hours must fit `hours_per_week` x
  `weeks` with 10 to 15% left for debugging (warning above 88%, error above 100%).
- **Stretch goals:** `stretch: true` units are optional. Nothing required may
  assume what only a stretch goal introduces, a project's first stage is never a
  stretch goal, and no required stage follows a stretch stage.
- **Minimum viable result:** every project states its `minimum`; the required
  stages deliver it, and the project overview shows it.
- **Warnings:** a missing `done_when`, drills nothing later uses, a unit over 16
  hours, too many units at difficulty 5 or at a single level, a hard stage that
  introduces three or more concepts or relies on one only a note covers.

The planning prompt also asks for generous estimates on routinely underestimated topics,
a small drill before each hard project stage, testing time inside every estimate, and a
consolidation stage at the end (refactor, tests, documented limitations).

`build` runs in two passes:

1. **Contracts:** for each unit, the functions, files, requirements and Allowed
   list (plus notes, and settings for languages without a preset). Each one is
   checked mechanically: schema, signatures parse, harness-supported types,
   stubs fit the files rules, no long dashes.
2. **Correction suite (optional, asked):** tests, generators, the hidden reference
   and scaffold files. A unit is kept only if `skillcheck selftest` passes: the
   reference passes every check (including Allowed and warnings), and every
   function has a test that fails on an empty stub. Rejections go back to the
   model with the exact failure, up to `--retries` (default 3); a unit that never
   passes keeps its contract but no tests, and the run exits non-zero.

Token use: the model returns data only (the instructions are rendered by
skillcheck), every request starts with the same system prompt (cacheable), a unit
request carries a one-line digest of earlier units instead of their files, and
every reply is cached on disk by a hash of its request, so a re-run (or
`--only <id>`) only pays for what changed. The run ends with a token count.

**Environment:** taskmaster assumes students work on the machine that builds the course.
`taskmaster env` shows what it detected: OS, about 25 tools with versions, and facts it tests
for real (does AddressSanitizer work, is a POSIX bash available). The curriculum front matter can
declare another environment (`environment: Ubuntu 24.04, gcc, valgrind, make`). The browser
prompt, the guided planner and every generation request include it; `import` warns when a unit
relies on a tool that is missing; tests are still verified on this machine before they are kept.

**LLM settings:** `--provider anthropic|meridian|openai`, `--base-url` (any
OpenAI-compatible server), `--model` (default `claude-sonnet-5-5`), `--api-key-env
VAR`. Defaults come from `LEVELUP_PROVIDER`, `LEVELUP_BASE_URL`, `LEVELUP_MODEL`,
`LEVELUP_API_KEY`, then the provider's usual variable (`ANTHROPIC_API_KEY`,
`OPENAI_API_KEY`), then the config file (`taskmaster config` prints its path:
`provider`, `base_url`, `model`, `api_key_env`). Keys are never stored in files.
To use a Claude Code login, run [Meridian](https://github.com/rynfar/meridian) and
pass `--provider meridian`.

## skillcheck commands

| Command | What it does |
|---------|--------------|
| `skillcheck check [unit] [--seed HEX] [--verbose] [--no-prompt]` | Live report, then `OK n/m` or `KO <item>: <reason>`. Exit 0 / 1 / 2. On a first pass, asks whether to unlock the next unit. |
| `skillcheck next` | Reveals the next unit: instructions, notes, workspace scaffold. Refuses while the current unit is not passed or skipped. |
| `skillcheck skip` | Skips the current unit if it is a stretch goal, then offers the next one. Required units cannot be skipped; a skipped unit can still be checked later. |
| `skillcheck status` | Course map: passed, skipped, current, locked (locked titles stay hidden). |
| `skillcheck progress` | Regenerates `PROGRESS.md` and prints the dashboard. `PROGRESS.md` has a Schedule section (planned hours done vs expected by now) when units carry `hours` and the course has `weeks`. When a week ends, skillcheck writes `reviews/week-NN.md`: that week's numbers and three questions (what you can do alone, what is unclear, whether the schedule holds). |
| `skillcheck explain [unit]` | Everything a check runs: generators, rounds, laws. |
| `skillcheck validate` | Parses every unit without running anything. |
| `skillcheck selftest [unit...]` | Course gate: each reference must pass, and every function must have a test that fails on an empty stub. |

`NO_COLOR` disables colours, `SKILLCHECK_ASCII` replaces the check marks.

## Course layout

```
course/
  subjects/              revealed instructions (rendered by skillcheck, never by an LLM)
  notes/                 revealed notes
  PROGRESS.md            generated after every check
  <workspace>/           one per drill, one per project (shared by its stages)
  .skillcheck/
    course.yaml          title, unit order, projects
    units/<id>.yaml      one file per unit: contract and tests together
    lib.yaml             shared generators
    refs/<id>/           hidden reference solution (a full workspace tree)
    scaffold/<id>/       files copied into a fresh workspace (never overwriting)
    notes/<name>.md      notes revealed with the units that list them
    journal.jsonl        event log; state and PROGRESS.md derive from it
    cache/               build cache (safe to delete)
```

## Unit spec

```yaml
id: d01-string-basics          # pNN.k-slug for project stages, plus `project: pNN-name`
title: String Basics
language: c                    # c, python, rust, bash, js, ts, go, java, ruby, or any name
difficulty: 2
concepts: [pointer arithmetic]
stretch: false                 # true: optional, `skillcheck skip` moves past it
target: function               # function: skillcheck's own main calls yours | program: your program is run
scaffold: minimal              # none (student creates everything) | minimal | full
stubs: src/str_basics.c        # where empty mandatory functions are written on unlock
goal: One sentence.
functions:                     # "Expected functions (minimal)" in the instructions
  - { name: str_len, sig: "size_t str_len(const char *s)", returns: bytes before the NUL }
files:                         # the soft tree; listing any rule makes other files unexpected
  - { path: include/str_basics.h, note: declares the prototypes }   # rule: required (default)
  - { path: "src/*.c", rule: one_or_more }
  - { path: "**", rule: free }                                      # allows anything else
requirements: [Behaviour only, one bullet each.]
allowed: [stdlib.h, malloc, free]   # strict whitelist; absent = unrestricted; [] = built-ins only
run: python -m logstat         # program target ({exe}, {python}, {cargo_bin} placeholders)
env: { LC_ALL: C }             # set for builds and runs, listed in the instructions
static: [{ name: lint, cmd: "ruff check ." }]
timeout_ms: 10000
cases: |
  str_len("abc") == 3                       # function target: one boolean expression per line
  raises(parse(""))
  # program target: [args] [@file="..."] [<<< "stdin"] => out == "..." err ~ "re" exit 0
random:
  rounds: 200
  fuzz:                        # compared with the hidden reference on generated inputs
    - str_len                  # generators derived from the parameter types
    - { fn: str_split, args: { s: 'string "ab," 0..30', sep: "lit ','" } }
    - { name: report, stdin: "lines(log_line) 0..40" }      # program target
  laws:                        # invariants, no reference needed
    - { name: counts agree, gen: { s: string }, check: ["word_count(s) == len(words(s))"] }
    - { roundtrip: [encode, decode] }      # decode(encode(x)) == x
    - { idempotent: normalize }            # f(f(x)) == f(x)
    - { agree: [fast_sort, slow_sort] }
```

**Generators:** `int lo..hi [wN]`, `uint`, `float lo..hi`, `bool`, `char "abc"`,
`string "alphabet" lo..hi`, `word`, `ascii`, `list(g) lo..hi`, `lines(g) lo..hi`,
`opt(g)`, `choice [..]`, `lit <value>`, templates `{ fmt: "{h}:{m}", vars: {...} }`,
and names from `lib.yaml`. Generators lean on edge cases (empty, boundaries,
two-letter alphabets), and failures are shrunk before they are reported.

**Expressions:** literals, `== != < <= > >= && || ! + - * / %`, `x.field`, `x[i]`,
student functions (dotted names for Python modules), `run(args=[..], stdin="..")`
for programs, and built-ins (`len bytes sorted reversed sum min max abs count contains
join split words lines lower upper trim chars unique keys values get all any int float
str is_sorted permutation repeat range slice starts_with ends_with matches replace
nonempty concat flatten approx raises then`; `then(a, b)` evaluates in order and returns `b`;
`all(xs, nonempty)` and `any(xs, p)` take a built-in's name as predicate). No user functions
or lambdas, so a spec cannot hide a reference solution in its tests.

Tests can be written in the habits of the course's language; they all mean the same thing:

| Written | Means |
|---|---|
| `True`, `None`, `NULL`, `nil`, `undefined`, `x is None`, `===`, `and`, `not in` | `true`, `null`, `==`, `&&`, `!contains` |
| `Ok(v)`, `Some(v)`, `Just(v)`, `String::from(v)`, `Box::new(v)` | `v` (harnesses unwrap them) |
| `f(x) == Err(..)`, `f(x).is_err()`, `f(x).is_ok()` | `raises(f(x))`, `!raises(f(x))` (the error payload is not compared) |
| `(a, b)`, `vec![..]`, `Vec::new()`, `{a: 1}`, `p.0` | lists, `[]`, maps, `p[0]` |
| `&x`, `&mut x`, `*x`, `10u8`, `2.5f64`, `10L`, `r"raw"`, `r#"raw"#` | the plain value |
| `\xHH`, `\uXXXX`, `\u{X}`, `\UXXXXXXXX`, octal `\101`, `\e` | the character |
| `s.len()`, `xs.length`, `s.to_lowercase()`, `", ".join(xs)`, `x.clone()` | `len(s)`, `lower(s)`, `join(xs, ", ")`, `x` (built-ins only) |

Names are checked before anything runs: an unknown function, a wrong argument count or
an unknown variable is reported with the closest valid name.

In-place functions: in C and Rust, a call to a function that returns nothing and
takes a writable buffer (`char *` without `const`, or `&mut`) evaluates to that
buffer after the call, so `reverse("abc") == "cba"` tests `void reverse(char *s)`.

## Languages

| | function target (harness) | program target | Allowed scanner | build / static |
|---|---|---|---|---|
| C | yes (scalar, `char *`, `char **`) | yes | headers + calls | `cc -std=c11 -Wall -Wextra -Werror` |
| Python | yes (any value; dataclasses and namedtuples become maps) | yes | imports | `compileall` |
| Rust | yes (scalars, `&str`/`String`, `Vec`/slices, `Option`, `Result`) | yes | `use`, paths, Cargo deps | `cargo build`, `fmt --check`, `clippy -D warnings` |
| Bash | no | yes | external commands | `bash -n` |
| JS/TS, Go, Java, Ruby | no | yes | imports (regex) | `build:` command |
| anything else | no | yes | `imports:` regexes in the spec | `build:` command |

On Windows, `bash` resolves to msys2 or Git Bash, never WSL's `System32\bash.exe`.

### Language settings are data

The presets for C, C++, Python, Rust, Bash, JavaScript, TypeScript, Go, Java and Ruby are a YAML
block embedded in skillcheck (`skillcheck/src/defaults.rs`). Only the function harnesses and the
dedicated Allowed scanners are code. A course overrides any preset field in its `languages:`
block, and a list set there replaces the preset's list (`static: []` removes Rust's fmt/clippy).

### Adding a language

Declare it once in `course.yaml`; every unit in that language inherits it, and
any field set on a unit wins:

```yaml
languages:
  perl:
    name: Perl 5                       # shown in the instructions
    build: perl -c main.pl             # optional
    run: perl main.pl                  # program command
    static: [{ name: lint, cmd: "perlcritic main.pl" }]
    imports: ['(?m)^\s*(?:use|require)\s+([A-Za-z][\w:]*)']   # Allowed check, group 1 = module
    extensions: [pl, pm]               # which files are source
    comment: "#"                       # line comments ignored by the scanners
    env: { PERL_UNICODE: SD }
    timeout_ms: 5000
    compiler: gcc                      # C-family builds
    flags: [-std=c11, -Wall, -Werror]  # program build and function harness
    wrap: valgrind --leak-check=full --error-exitcode=99   # prefix for every run of student code
    support: [support/common/shim.c]   # course files built in with the student's code
```

Leak checks, sanitizers and allocation-failure tests are course content, not skillcheck features:
`wrap` (valgrind) or `flags` (`-fsanitize=address`) make a run fail when the tool reports a
problem, and the harness must exit cleanly, so leaks reported at exit fail the check. Units can add
`extra_flags`, `wrap`, `support` files and `helpers` (test-only functions from support files):

```yaml
extra_flags: [-Dmalloc=sc_malloc]
support: [support/d01-string-basics/fail_alloc.c]   # defines sc_malloc and fail_alloc_after
helpers: [{ name: fail_alloc_after, sig: "void fail_alloc_after(int n)" }]
cases: |
  then(fail_alloc_after(0), str_dup("abc")) == null
```

## Status

Built and verified on the demo course (C, Bash, Rust, Perl through a
`languages:` block only, and a two-stage Python project): reveal, scaffold, every check category, fuzzing with shrinking, laws,
regression across stages, seed replay, PROGRESS.md, selftest.

Not built yet: mutant testing of generated suites, batching several small
drills per request, one-shot iterator inputs for Python, Rust support files
(`support:` is used by the C and Python harnesses).
