# PiscineBuilder

Intensive, hands-on programming courses in any language: planned and generated once
with an LLM, then practised offline with an automatic grader.

I made PiscineBuilder for myself, to learn by building real projects with immediate, honest
feedback. It is shared as is: it works for my courses and my machine (Windows, with Docker
for the Linux parts), and may need adjusting for yours.

PiscineBuilder has two tools:

- **taskmaster** creates a course: it plans the curriculum with you, then writes every
  unit's instructions, tests and a hidden reference solution, and keeps only what passes
  its quality gate. It is the only part that talks to an LLM.
- **skillcheck** is what the student uses: it reveals units one at a time, grades the work
  on the spot and tracks progress. No network, no API key, no LLM.

## Features

**Planning (taskmaster)**
- A short guided conversation in the terminal (`taskmaster new`): one multiple-choice
  question at a time, a recommended answer for each, your own answer always possible.
  Or plan in any chat assistant with a prompt taskmaster prints, then import the result.
- The planner sees your machine: installed tools and versions, what really works
  (AddressSanitizer, a POSIX shell), and whether Linux APIs are native or reachable
  through Docker or WSL. It prefers portable projects and asks when a goal could go
  either way.
- A validator checks every plan: each concept is taught before it is used, the workload
  fits your weekly hours with a 10 to 15% buffer, every project states its minimum viable
  result, stretch goals stay optional, project stages build one program. Mechanical slips
  are fixed automatically.
- The planner reviews its own first draft against a teaching checklist before you see it,
  and `taskmaster revise` changes an existing plan through the planner.

**Generation (taskmaster)**
- Every unit's tests and reference solution must pass skillcheck's self-test before they
  are kept: the reference passes every check, and every function has a test that fails on
  an empty stub. Rejections go back to the model with the exact error, and retries ask for
  the fix only.
- Interrupted builds resume where they stopped; every reply is cached, so a re-run only
  pays for what changed. Units that already passed are kept.
- Units that need Linux (fork, signals, process groups) are verified in an Ubuntu container,
  so a Linux course can be built from Windows or macOS.

**Practice (skillcheck)**
- One unit at a time, with instructions, notes and a workspace scaffold revealed on unlock.
- A live report for every check: file layout, expected prototypes, allowed functions and
  modules, build and lint, fixed cases, fuzzing against the hidden reference with shrunk
  counterexamples, invariants, and regression on earlier project stages.
- Languages: C, Python and Rust (functions called directly, or whole programs), Bash,
  JavaScript, TypeScript, Go, Java, Ruby, and any other language declared as data.
- `PROGRESS.md` with a dashboard, a schedule against the planned hours, and a weekly review.
- Stretch goals that can be skipped, and `skillcheck shell` to run Linux units by hand.

## Install

You need [Rust](https://rustup.rs) (stable). From this folder:

```
cargo install --path skillcheck
cargo install --path taskmaster
```

Also needed, depending on the course:

- The compilers or interpreters of the course's languages (for example `cc`, `python3`).
- **Docker Desktop**, only for units marked `platform: linux` on Windows or macOS.
  `taskmaster setup` can install or start it for you.
- **An LLM**, only to create courses: an Anthropic or OpenAI API key, any
  OpenAI-compatible server, or a Claude Code login through
  [Meridian](https://github.com/rynfar/meridian). Students never need one.

Keep this folder where it is after installing: skillcheck builds its Linux image from
this source.

## Quickstart

### Try a course (no LLM)

The demo course has small units in C, Bash, Rust, Perl and a two-stage Python project:

```
cd examples/demo-course
skillcheck next              # unlock the first unit: subjects/d01-string-basics.md
cd d01-string-basics         # your workspace
# ...write the code the instructions ask for...
skillcheck check             # grade it: a live report, then OK n/m or KO <reason>
```

When a unit passes, skillcheck offers the next one. `skillcheck status` shows the
course map and `skillcheck progress` the dashboard.

### Create your own course

Use a folder of its own for each course, outside this repository:

```
mkdir my-course && cd my-course
taskmaster new "learn C pointers, then write a small HTTP server" --provider meridian
taskmaster build --tests --provider meridian
skillcheck next
```

`new` scans your machine, asks a few questions, shows the plan as a table with hours and
difficulties, and lets you approve it, ask for changes or edit it yourself. If the plan has
Linux units and your machine is not Linux, it offers to set up Docker Desktop or WSL.
`build` then writes the course. It can take a while for a big course, and it can be
stopped and resumed at any time.

## User manual

### The course lifecycle

1. **Plan:** `taskmaster new` (terminal), or `taskmaster prompt` then `taskmaster import`
   (any chat assistant). The result is `curriculum.md`: a readable markdown plan you can
   edit by hand, change with `taskmaster revise`, and inspect with `taskmaster show`.
2. **Approve:** taskmaster only builds a plan you approved; editing it asks again.
3. **Build:** `taskmaster build` writes the unit specs and notes, then (with `--tests`, or
   when you say yes) the correction suite: tests and hidden references, each gated.
4. **Practise:** `skillcheck next`, work in the unit's workspace, `skillcheck check`,
   repeat. Project stages share one workspace and are tested together.

### taskmaster commands

| Command | What it does |
|---|---|
| `taskmaster new "<goal>" [--from FILE]... [--course DIR] [--yes]` | Plan a curriculum in a short conversation (LLM). `--from` adds a syllabus or notes. |
| `taskmaster prompt [--lang L] [--level beginner] [--hours 10]` | Print a planning prompt for any chat assistant. |
| `taskmaster import <file> [--course DIR] [--yes] [--paste]` | Validate a curriculum, fix mechanical slips, show it, approve it. `--paste` prints the errors as a block to paste back into the chat. |
| `taskmaster revise "<changes>" [--course DIR] [--yes]` | Have the planner rewrite `curriculum.md` (LLM). The old version is kept in `.skillcheck/taskmaster/curriculum.previous.md`. |
| `taskmaster show [file] [--course DIR] [--full]` | The plan: units, hours, both difficulties, prerequisites, approval. `--full` adds briefs and Allowed lists. |
| `taskmaster env` | What taskmaster detected on this machine (tools, facts, Linux access). |
| `taskmaster setup [--course DIR]` | For a plan with Linux units: install or start Docker Desktop, or install WSL Ubuntu. Asks before installing anything. |
| `taskmaster build [--course DIR] [--tests \| --no-tests] [--only ID]... [--retries N] [--yes]` | Write the course (LLM). `--only` redoes the named units or projects; `--retries` defaults to 3. |
| `taskmaster config` | Path of the config file. |

**LLM options** (`new`, `revise`, `build`): `--provider anthropic|meridian|openai`,
`--base-url URL` (any OpenAI-compatible server), `--model NAME` (default
`claude-sonnet-5-5`), `--api-key-env VAR`. Defaults come from `PISCINEBUILDER_PROVIDER`,
`PISCINEBUILDER_BASE_URL`, `PISCINEBUILDER_MODEL` and `PISCINEBUILDER_API_KEY`, then the provider's usual
variable (`ANTHROPIC_API_KEY`, `OPENAI_API_KEY`), then the config file (`provider`,
`base_url`, `model`, `api_key_env`). Keys are never written to files.

### skillcheck commands

| Command | What it does |
|---|---|
| `skillcheck next` | Reveal the next unit: instructions, notes, workspace scaffold. Refuses while the current unit is not passed or skipped. |
| `skillcheck check [unit] [--seed HEX] [--verbose] [--no-prompt]` | Grade the current unit (or the one whose workspace you are in). Exit 0 OK, 1 KO, 2 course or usage error. `--seed` replays a failing random run. |
| `skillcheck skip` | Skip the current unit if it is a stretch goal. |
| `skillcheck status` | Course map: passed, skipped, current, locked (locked titles stay hidden). |
| `skillcheck progress` | Regenerate `PROGRESS.md` and print the dashboard. |
| `skillcheck explain [unit]` | Exactly what a check runs: generators, rounds, laws. |
| `skillcheck shell [unit] [--port N]` | Linux units on another system: a bash terminal in the Linux container, in the unit's workspace. `--port N` publishes that port on `127.0.0.1` (start your server with `--bind 0.0.0.0`). |
| `skillcheck validate` | Course authors: parse every unit without running anything. |
| `skillcheck selftest [unit...]` | Course authors: each reference must pass, and every function must have a test that fails on an empty stub. |

`NO_COLOR` disables colours; `SKILLCHECK_ASCII` replaces the check marks.

### Progress and schedule

After every check, skillcheck rewrites `PROGRESS.md`: units passed, checks per unit,
first-try rate, active time, the check each unit failed most, results by difficulty, weekly
activity and, when the plan has hours and weeks, a schedule comparing the planned hours
done with where you should be by now. When a week ends it writes
`reviews/week-NN.md` with that week's numbers and three questions: what you can do on
your own, what is still unclear, whether the schedule holds.

### Linux units on Windows and macOS

A unit (or a whole project) marked `platform: linux` needs Linux APIs. On another system,
skillcheck runs it in an Ubuntu 24.04 container by itself: you keep editing and running
`skillcheck check` in your usual terminal, Docker Desktop just has to be running. The image
(Rust with clippy and rustfmt, gcc, python3, dash as `/bin/sh`, GNU coreutils, and
skillcheck) is built the first time it is needed, and again after skillcheck changes. Use
`skillcheck shell` to run your program by hand there. For your editor to check such code
as Linux code with rust-analyzer, add the target once
(`rustup target add x86_64-unknown-linux-gnu`) and set
`"rust-analyzer.cargo.target": "x86_64-unknown-linux-gnu"` in the workspace settings.

### Troubleshooting

| Problem | What to do |
|---|---|
| `Docker is not running` | Start Docker Desktop and wait until it is ready, or run `taskmaster setup`. |
| `Docker runs Windows containers` | Docker Desktop tray icon: "Switch to Linux containers". |
| The build stopped on a rate limit or quota | Run the same command again: finished work is kept, cached replies cost nothing. |
| A unit `FAILED` in the build | `taskmaster build --tests --only <id>` retries it. Later stages of its project wait for it. |
| `curriculum.md changed since it was approved` | Review and approve it again: taskmaster shows the plan and asks. |
| A check fails randomly | The report prints the seed: `skillcheck check --seed <hex>` replays the same inputs. |

## Reference

- [Curriculum format and planning rules](docs/curriculum.md)
- [How `taskmaster build` works](docs/build.md)
- [Unit spec, tests and course layout](docs/unit-spec.md)
- [Languages and adding one](docs/languages.md)

## Repository layout

```
skillcheck/          the grader (library and CLI)
taskmaster/          the course builder (CLI)
examples/demo-course a small course to try skillcheck without an LLM
docs/                reference documentation
```

Development: `cargo test` runs both tools' tests; `cargo clippy --all-targets -- -D warnings`
must stay clean. Courses are never built inside this folder: `.gitignore` keeps them out.

## Status

Verified on the demo course (every check category, fuzzing with shrinking, laws,
regression across stages, seed replay, PROGRESS.md, self-test) and on a 51-unit Rust
course built from Windows, with its Linux units verified in the container.

Not built yet: mutant testing of generated suites, batching several small drills per
request, one-shot iterator inputs for Python, Rust support files (`support:` works for C
and Python).
