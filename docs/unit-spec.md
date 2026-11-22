# Unit spec, tests and course layout

## Course layout

```
course/
  curriculum.md          the plan (coursecrafter)
  subjects/              revealed instructions (rendered by proctor, never by an LLM)
  notes/                 revealed notes
  reviews/               weekly reviews
  RULES.md               rigor level, edge cases, output, linters, limits, fair play (generated)
  PROGRESS.md            generated after every check
  .vscode/               proctor tasks (Ctrl+Shift+B runs a check), added on the first unlock
  <workspace>/           one per drill, one per project (shared by its stages)
  .proctor/
    course.yaml          title, unit order, projects, language settings
    units/<id>.yaml      one file per unit: contract and tests together
    lib.yaml             shared generators
    scaffold/<id>/       files copied into a fresh workspace (never overwriting)
    support/<id>/        test-only support files
    notes/<name>.md      notes revealed with the units that list them
    journal.jsonl        event log; state and PROGRESS.md derive from it
    cache/               build cache (safe to delete)
    coursecrafter/          approval, environment, validator log, LLM reply cache, logs/ of planning sessions
```

## Unit spec

```yaml
id: d01-string-basics          # pNN.k-slug for project stages, plus `project: pNN-name`
title: String Basics
language: c                    # c, python, rust, bash, js, ts, go, java, ruby, or any name
difficulty: 2
concepts: [pointer arithmetic]
stretch: false                 # true: optional, `proctor skip` moves past it
platform: linux                # optional: needs Linux; elsewhere it runs in a container (Docker)
target: function               # function: proctor's own main calls yours | program: your program is run
scaffold: minimal              # none (student creates everything) | minimal | full
stubs: src/str_basics.c        # where empty mandatory functions are written on unlock
goal: One sentence.
guided: false                  # true: the lesson is a step-by-step Walkthrough
reading: false                 # true: only a page to read (a project's introduction): no files, functions or tests;
                               # proctor check passes it once unlocked
lesson: |                      # markdown after the goal: "Before you start", or "Walkthrough" when guided
  ### Step 1: ...
functions:                     # "Expected functions (minimal)" in the instructions
  - { name: str_len, sig: "size_t str_len(const char *s)", returns: bytes before the NUL }
files:                         # the soft tree; listing any rule makes other files unexpected
  - { path: include/str_basics.h, note: declares the prototypes }   # rule: required (default)
  - { path: "src/*.c", rule: one_or_more }
  - { path: "**", rule: free }                                      # allows anything else
requirements: |                # optional markdown (or a list): only what nothing else on the page says
  With `-l`, print the number of **lines** instead.
think_about: [blanks around words, no input at all]   # edge-case categories, never answers
allowed: [stdlib.h, malloc, free]   # strict whitelist; absent = unrestricted; [] = built-ins only
                                    # an entry covers what is inside it (`std` = all of Rust's std)
                                    # a stage adds to its earlier stages' lists (absent = adds nothing)
forbidden: [std::mem::swap]         # banned even though `allowed` covers it (needs `allowed` here
                                    # or in an earlier stage)
                                    # neither applies to the student's own tests in tests/
example: |                     # program target, fallback when no case has an exact `out ==`
  $ python -m logstat < app.log
  INFO 3
run: python -m logstat         # program target ({exe}, {python}, {cargo_bin} placeholders)
env: { LC_ALL: C }             # set for builds and runs, listed in the instructions
static: [{ name: lint, cmd: "ruff check ." }]
timeout_ms: 10000
cases: |
  # example                                 # the next case is shown under "Examples"
  str_len("abc") == 3                       # function target: one boolean expression per line
  raises(parse(""))
  # program target: [args] [@file="..."] [<<< "stdin"] => out == "..." err ~ "re" exit 0
rust_tests: |                  # Rust only: #[test] functions, see "Tests in Rust" below
  #[test]
  fn push_then_pop() { let mut s = Stack::new(); s.push(1); assert_eq!(s.pop(), Some(1)); }
random:
  rounds: 200
  laws:                        # properties checked on inputs generated from the run's seed
    - { name: counts agree, gen: { s: string }, check: ["word_count(s) == len(words(s))"] }
    - { name: one line per entry, gen: { log: "lines(log_line) 0..40" },
        check: ["len(lines(run(stdin=log).out)) == len(lines(log))"] }      # program target
    - { roundtrip: [encode, decode] }      # decode(encode(x)) == x
    - { idempotent: normalize }            # f(f(x)) == f(x)
    - { agree: [fast_sort, slow_sort] }
```

There is no reference solution: every expected value comes from the test itself. Laws are
the random tests. Each check draws a new seed (`proctor check` prints it on its first line,
`--seed HEX` replays it), each law gets its own stream of that seed, and a failing round is
shrunk to a small input and reported with the command that replays it. A law must generate
something and use every variable it generates: `proctor validate` rejects one that would run
the same fixed check every round.

A C or Rust program target runs the built binary (for Rust, the only `[[bin]]` or the
package name); other languages need `run`.

## What a check runs

In order: the expected files, the prototypes of the expected functions, the Allowed list,
the build and the language's linters, then the tests of the current unit and, for a project
stage, the tests of every earlier stage (regression). A step that fails stops the steps that
depend on it, and the first failure is the one reported.

## Generators

`int lo..hi [wN]`, `uint`, `float lo..hi`, `bool`, `char "abc"`, `string "alphabet" lo..hi`,
`word`, `ascii`, `list(g) lo..hi`, `lines(g) lo..hi`, `opt(g)`, `choice [..]`, `lit <value>`,
templates `{ fmt: "{h}:{m}", vars: {...} }`, and names from `lib.yaml`. Generators lean on
edge cases (empty, boundaries, two-letter alphabets), and failures are shrunk before they
are reported.

## Expressions

Literals, `== != < <= > >= && || ! + - * / %`, `x.field`, `x[i]`, the student's functions
(dotted names for Python modules), `run(args=[..], stdin="..")` for programs, and the
built-ins `len bytes sorted reversed sum min max abs count contains join split words lines
lower upper trim chars unique keys values get all any int float str is_sorted permutation
repeat range slice starts_with ends_with matches replace nonempty concat flatten approx
raises then`. `then(a, b)` evaluates in order and returns `b`; `all(xs, nonempty)` and
`any(xs, p)` take a built-in's name as predicate. Comprehensions state a property of every
element: `[len(w) for w in words(s) if w != ""]`, and as the only argument of a call,
`all(len(w) <= len(longest(s)) for w in words(s))`. There are no user functions or lambdas, so
a spec cannot hide a solution of its own in its tests.

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

Names are checked before anything runs: an unknown function, a wrong argument count or an
unknown variable is reported with the closest valid name.

In-place functions: in C and Rust, a call to a function that returns nothing and takes a
writable buffer (`char *` without `const`, or `&mut`) evaluates to that buffer after the
call, so `reverse("abc") == "cba"` tests `void reverse(char *s)`.

## Tests in Rust

Cases can only call free functions with plain types. What they cannot reach (the student's
own structs, enums, methods, traits, generics) is tested by `rust_tests`: top-level
`#[test]` functions that proctor compiles against the student's library crate, which they
see through `use student::*;` (added at the top of the file). Each one is a test item of the
unit, run in its own process with the unit's time limit, so a panic, a crash or a hang fails
that test only; a failure shows libtest's message on one line, with its location when it is
in the student's code. A test preceded by a `// example` line is shown under "Examples".

Methods and associated functions are listed under their type, and the prototype check looks
for them in an `impl` block of that type (`Self` and the type's name are the same):

```yaml
functions:
  - { name: "Stack::new", sig: "pub fn new() -> Self", returns: an empty stack }
  - { name: "Stack::push", sig: "pub fn push(&mut self, x: i32)" }
```

At unlock the stubs declare an empty `pub struct Stack {}` with these methods in an `impl`
(a later stage only adds an `impl` block); the student gives it its fields, or turns it into
the enum the requirements describe. When the tests build the type or read its fields, the
unit gives it in `types`, Rust definitions without `impl` blocks:

```yaml
types: |
  #[derive(Debug, Clone, Copy, PartialEq)]
  pub struct Point {
      pub x: i32,
      pub y: i32,
  }
```

The instructions show them under "Expected types", and the stubs (at unlock and in
`proctor selftest`) start with them, so the tests compile before any code exists. A function
that cases call must still suit the harness.
