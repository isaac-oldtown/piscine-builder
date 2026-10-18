# Unit spec, tests and course layout

## Course layout

```
course/
  curriculum.md          the plan (coursecrafter)
  subjects/              revealed instructions (rendered by proctor, never by an LLM)
  notes/                 revealed notes
  reviews/               weekly reviews
  PROGRESS.md            generated after every check
  <workspace>/           one per drill, one per project (shared by its stages)
  .proctor/
    course.yaml          title, unit order, projects, language settings
    units/<id>.yaml      one file per unit: contract and tests together
    lib.yaml             shared generators
    refs/<id>/           hidden reference solution (a full workspace tree)
    scaffold/<id>/       files copied into a fresh workspace (never overwriting)
    support/<id>/        test-only support files
    notes/<name>.md      notes revealed with the units that list them
    journal.jsonl        event log; state and PROGRESS.md derive from it
    cache/               build cache (safe to delete)
    coursecrafter/          approval, environment, validator log, LLM reply cache
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
lesson: |                      # optional markdown, shown as a Walkthrough after the goal (guided units)
  ### Step 1: ...
functions:                     # "Expected functions (minimal)" in the instructions
  - { name: str_len, sig: "size_t str_len(const char *s)", returns: bytes before the NUL }
files:                         # the soft tree; listing any rule makes other files unexpected
  - { path: include/str_basics.h, note: declares the prototypes }   # rule: required (default)
  - { path: "src/*.c", rule: one_or_more }
  - { path: "**", rule: free }                                      # allows anything else
requirements: [Behaviour only, one bullet each.]
allowed: [stdlib.h, malloc, free]   # strict whitelist; absent = unrestricted; [] = built-ins only
                                    # an entry covers what is inside it (`std` = all of Rust's std)
forbidden: [std::mem::swap]         # banned even though `allowed` covers it (needs `allowed`)
                                    # neither applies to the student's own tests in tests/
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
`any(xs, p)` take a built-in's name as predicate. There are no user functions or lambdas, so
a spec cannot hide a reference solution in its tests.

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
