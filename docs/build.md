# How `coursecrafter build` works

`coursecrafter build` turns an approved `curriculum.md` into a course in the hidden
`.proctor/` folder. It runs in two passes.

## Pass 1: contracts

For each unit, the model writes the contract: the functions to write, the expected files,
the behaviour requirements and what the code may use. Notes are written with the unit they
are revealed with, and course settings for languages without a preset. Each contract is
checked mechanically (schema, signatures, harness-supported types, stubs that fit the file
rules, nothing that reveals the hidden tests) and sent back with the problems when it fails.

Contracts teach as well as specify. A unit that introduces concepts gets a lesson, shown as "Before you start": each concept explained with a small example that is not the solution, its output, the usual pitfall and a pointer to the official docs. A `guided` unit gets a step-by-step walkthrough instead. Requirements describe the result in words and may only rely on what the lesson or an earlier unit teaches; functions they mention are named in the functions table; a program target shows its exact output under "Example". A contract missing any of these is sent back.

Fields that come from the plan (hours, difficulty, stretch, platform...) are copied by
coursecrafter, not by the model; editing them in the plan updates the existing contracts on the
next build, with no LLM call.

## Pass 2: the correction suite

With `--tests` (or when you answer yes), each unit gets its tests, generators, hidden
reference solution and scaffold files. A unit is kept only if `proctor selftest` passes:
the reference passes every check, including Allowed and the linters, and every function has
a test that fails on an empty stub. Linux units are self-tested in the Linux container.

Each request carries what the model cannot guess:

- the unit's contract and its language's notes;
- what the code may use at this stage, earlier stages' Allowed entries merged;
- for a stage: the previous stage's reference to build on, the earlier stages' tests (they
  run again against the new program), and the later stages (so it never tests behaviour a
  later stage changes).

What is mechanical is done by coursecrafter: a Rust unit with a workspace scaffold always gets a `Cargo.toml`, derived from the reference (same package and targets, no dependency entries) when the model sends none; Rust references are run through `cargo fmt`, C and
Rust programs run as built (a project's other languages keep their first stage's `run`
command), and a `run` pointing into a build folder is rejected.

When the gate rejects a unit, the exact failure goes back to the model, which sends only the
files it changes: the rest of its earlier reply is kept, so fixing one error cannot break
what already worked. After `--retries` attempts (default 3) the unit is marked FAILED: its
contract stays, without tests, and the build exits non-zero. The following stages of that
project are not generated, since they build on it; they wait for the next run.

After a build, the instructions of units the student already unlocked are rendered again from the new specs (workspaces are never touched), so a rebuilt unit shows up without unlocking it again.

## Resuming and tokens

A build can be stopped at any time, including by a rate limit or a quota: run the same
command again. Existing contracts and notes are kept, units with a suite are skipped, and
every reply is cached on disk by a hash of its request, so a re-run only pays for what
changed. `--only <id>` redoes a unit (or every stage of a project) from scratch.

The model returns data only (proctor renders the instructions), every request starts with
the same system prompt (cacheable by the provider) holding only the notes for the course's
languages, and earlier units are summarized in one line each. The run ends with a token count.
