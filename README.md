# PiscineBuilder

A personal, 42-inspired programming Piscine: structured curricula, hands-on projects, automated grading, and progressive skill development.

PiscineBuilder is a command-line system for learning programming by building real software. An LLM helps plan a course and generate its units; once built, the course runs offline with a strict, deterministic grader.

It has two tools:

- **coursecrafter** plans a curriculum with drills and multi-stage projects, then generates each unit's instructions, tests, and hidden reference solution. Units are kept only if they pass the quality checks.
- **proctor** reveals one unit at a time, grades your work, and tracks progress, schedule, and weekly reviews. It needs no LLM, API key, or network access.

This is a personal project, shared as is. It works for my courses and my machine (Windows, with Docker for Linux-specific units) and may need adjustments for yours.

## Install

Install stable Rust from [rustup.rs](https://rustup.rs). From the repository root:

```
cargo install --path proctor
cargo install --path coursecrafter
```

Depending on your course, you'll also need the relevant compilers or interpreters, such as `cc` or `python3`.

Docker Desktop is needed for Linux-specific units on Windows or macOS. `coursecrafter setup` can help install or start it.

An LLM provider is needed only to create courses: Anthropic, OpenAI, an OpenAI-compatible server, or a Claude Code login through [Meridian](https://github.com/rynfar/meridian). Students using an existing course don't need one.

Keep this repository in its current location after installation; proctor builds its Linux image from this source tree.

## Quickstart

### Try the demo course

The demo contains units in C, Bash, Rust, and Perl (an optional stretch goal), plus a two-stage Python project, planned as a one-week course of 10 hours. No LLM is required. Its plan is in `curriculum.md`; `coursecrafter show` displays it.

```
cd examples/demo-course

proctor next
cd d01-string-basics

# Implement the assignment, then:
proctor check
```

`proctor next` reveals the next unit. `proctor check` grades your work and prints a report. Once you pass, you can unlock the next unit.

Useful commands:

```
proctor status    # Course map
proctor progress  # Progress dashboard
```

### Create a course

Create a separate directory for each course, outside this repository:

```
mkdir my-course
cd my-course

coursecrafter new "learn C pointers, then write a small HTTP server" --provider meridian
coursecrafter build --tests --provider meridian

proctor next
```

`coursecrafter new` scans your environment, asks planning questions, and presents a curriculum with estimated hours, difficulty, and prerequisites. You can approve it, request changes, or edit it yourself.

`coursecrafter build` generates the course. Large courses can take a while; interrupted builds can be resumed, and completed units are retained.

### Token consumption

Rough estimates for planning and building a course with tests, with the default model (`claude-sonnet-5-5`, $2 per million input tokens and $10 per million output tokens at the time of writing):

| Course | Example | Input tokens | Output tokens | Approximate cost |
|---|---|---|---|---|
| Small: about 10 units, mostly drills | 2 weeks, one small project | 0.3 M | 50 k | $1 |
| Medium: about 25 units | 4 weeks, two projects | 1 M | 150 k | $3 to $4 |
| Large: about 50 units | 7 weeks, five projects | 2.5 M | 350 k | $8 to $10 |

Planning with `coursecrafter new` accounts for 5 to 10% of this; most of it is the build. A project stage costs about twice as much as a drill, because its request carries the previous stage's reference and the earlier stages' tests. Each rejected attempt also adds a call. The large row is based on the output of a real 51-unit Rust course; the input figures are estimates.

Things that lower the cost: the system prompt is the same for every request, so providers that cache prompts (Anthropic does) charge cached input at a fraction of the price. Replies are cached on disk, so rerunning a build or resuming it after an interruption costs nothing for finished work, and `--only` regenerates just the units you name. Changing the curriculum's `environment:` line or updating coursecrafter's prompts invalidates that cache.

With a Claude Code subscription through Meridian, there is no per-token cost, but a large build can reach the subscription's usage limit. When it does, the build stops cleanly: run the same command again later.

## Course lifecycle

1. **Plan:** `coursecrafter new` for interactive planning, or `coursecrafter prompt` followed by `coursecrafter import` using a chat assistant.
2. **Review:** Inspect and edit `curriculum.md`. The plan must be approved before it can be built.
3. **Build:** `coursecrafter build --tests` generates unit instructions, tests, and hidden reference solutions. Each unit must pass the generation quality checks.
4. **Practise:** Use `proctor next`, implement the assignment, and run `proctor check`. Project stages share a workspace and are tested together.

The curriculum validator checks prerequisites, workload against available hours, project stages, and minimum viable results. Optional stretch goals can be skipped.

## Commands

### coursecrafter

| Command | Description |
|---|---|
| `coursecrafter new "<goal>" [--from FILE] [--course DIR] [--yes]` | Plan a curriculum with an LLM. `--from` adds a syllabus or notes. |
| `coursecrafter prompt [--lang L] [--level beginner] [--hours 10]` | Print a prompt for a chat assistant. |
| `coursecrafter import <file> [--course DIR] [--yes] [--paste]` | Validate and import a curriculum. |
| `coursecrafter revise "<changes>" [--course DIR] [--yes]` | Revise the curriculum with an LLM. The previous version is retained. |
| `coursecrafter show [file] [--course DIR] [--full]` | Show units, hours, difficulty, prerequisites, and approval status. |
| `coursecrafter env` | Show detected tools and platform capabilities. |
| `coursecrafter setup [--course DIR]` | Configure Docker Desktop or WSL for Linux units. |
| `coursecrafter build [--course DIR] [--tests \| --no-tests] [--only ID]... [--retries N] [--yes]` | Generate the course. `--only` rebuilds selected units; retries default to 3. |
| `coursecrafter config` | Show the configuration file path. |

LLM commands accept `--provider anthropic|meridian|openai`, `--base-url URL`, `--model NAME`, and `--api-key-env VAR`.

Configuration comes from the `PISCINEBUILDER_*` environment variables, provider-specific API key variables, or the config file. API keys are never written to files.

### proctor

| Command | Description |
|---|---|
| `proctor next` | Reveal the next unit's instructions, notes, and workspace scaffold. |
| `proctor check [unit] [--seed HEX] [--verbose] [--no-prompt]` | Grade a unit. Exit codes: 0 pass, 1 fail, 2 course or usage error. |
| `proctor skip` | Skip the current unit if it is an optional stretch goal. |
| `proctor status` | Show passed, skipped, current, and locked units. Locked titles remain hidden. |
| `proctor progress` | Regenerate `PROGRESS.md` and print the dashboard. |
| `proctor explain [unit]` | Explain the checks run for a unit. |
| `proctor shell [unit] [--port N]` | Open a Bash shell in the Linux container for a Linux unit. |
| `proctor validate` | Validate unit specifications without running checks. |
| `proctor selftest [unit...]` | Check that reference solutions pass and required functions have tests that fail on empty stubs. |

Set `NO_COLOR` to disable colors. Set `PROCTOR_ASCII` to replace check marks.

## Grading and progress

Checks cover file layout, required function signatures, allowed functions and modules, compilation, linting, fixed tests, fuzzing against the hidden reference, invariants, and regression across earlier project stages.

Supported languages include C, Python, Rust, Bash, JavaScript, TypeScript, Go, Java, Ruby, and other languages declared through the language configuration.

After each check, proctor updates `PROGRESS.md` with completed units, attempts, active time, common failures, results by difficulty, weekly activity, and schedule adherence. Weekly reviews are written to `reviews/week-NN.md`.

## Linux units on Windows and macOS

Units marked `platform: linux` run in an Ubuntu 24.04 container when the host isn't Linux. Docker Desktop must be running; you keep editing and running `proctor check` in your usual terminal.

The image includes Rust with clippy and rustfmt, gcc, python3, dash as `/bin/sh`, and GNU coreutils. It is built on first use and rebuilt after proctor changes.

Use `proctor shell` to run your program by hand in the container. `--port N` publishes a port on `127.0.0.1`; start your server with `--bind 0.0.0.0`.

To have rust-analyzer check this code as Linux code, run `rustup target add x86_64-unknown-linux-gnu` once and set `"rust-analyzer.cargo.target": "x86_64-unknown-linux-gnu"` in your workspace settings.

## Troubleshooting

| Problem | Solution |
|---|---|
| `Docker is not running` | Start Docker Desktop and wait until it is ready, or run `coursecrafter setup`. |
| `Docker runs Windows containers` | Switch Docker Desktop to Linux containers from its tray icon. |
| The build stopped on a rate limit or quota | Run the same command again. Completed work is kept, and cached replies cost nothing. |
| A unit `FAILED` during the build | Run `coursecrafter build --tests --only <id>`. Later stages of its project wait for it. |
| `curriculum.md changed since it was approved` | Review the plan and approve it again. |
| A check fails randomly | Replay the same inputs with `proctor check --seed <hex>`, using the seed from the report. |

## Reference

- [Curriculum format and planning rules](docs/curriculum.md)
- [How `coursecrafter build` works](docs/build.md)
- [Unit spec, tests, and course layout](docs/unit-spec.md)
- [Languages and adding one](docs/languages.md)

## Repository layout

```
proctor/            The grader (library and CLI)
coursecrafter/            The course builder (CLI)
examples/demo-course/  A small course to try without an LLM
docs/                  Reference documentation
```

Run `cargo test` for both tools' tests; `cargo clippy --all-targets -- -D warnings` must stay clean. Courses are never built inside this repository, and `.gitignore` keeps them out.

## Status

Verified on the demo course and on a 51-unit Rust course built from Windows, with its Linux units checked in the container.

Not built yet: mutation testing of generated suites, batching several small drills per request, one-shot iterator inputs for Python, and Rust support files (`support:` works for C and Python).
