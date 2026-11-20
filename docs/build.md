# How `coursecrafter build` works

`coursecrafter build` turns an approved `curriculum.md` into a course in the hidden
`.proctor/` folder. It runs in two passes.

## Pass 1: contracts

For each unit, the model writes the contract: the functions to write, the expected files,
the behaviour requirements and what the code may use. Notes are written with the unit they
are revealed with, and course settings for languages without a preset. Each contract is
checked mechanically (schema, signatures, harness-supported types, stubs that fit the file
rules, nothing that reveals the hidden tests) and sent back with the problems when it fails.

Contracts teach as well as specify. A unit that introduces concepts gets a lesson, shown as "Before you start" (30 to 70 lines): one `###` subsection per introduced concept, with the syntax (a table when there are several forms), one small example that is not the solution and its output, at most two pitfalls, a one-line link to the official docs, and how it differs from a concept it extends (`&mut` after `mut`). It ends with an ungraded **Try it**. A `guided` unit gets a step-by-step walkthrough instead. A lesson whose headings miss an introduced concept is sent back.

Requirements hold the behaviour the signatures and their `returns` cannot state: the tests are written later from the contract alone, so every decision of the plan's brief that the tests check (program arguments, exit codes, error formats, limits, protocols) must be in the contract. They never repeat what the page already shows (Allowed and Forbidden, examples) or `RULES.md`; a requirement that restates the linters or the Allowed list is sent back. A `forbidden` list with no `allowed` in the unit or an earlier stage is sent back too.

The tests come with two more pieces of text. Cases preceded by a `# example` comment line are shown as **Examples** with their results, so examples always agree with the grading (a program needs one case with an exact output). `think_about` lists two to four edge-case categories: coursecrafter drops a bullet that gives a case or its answer away (a quoted input, a result, a number of three digits or more or a distinctive string from the cases) and any beyond the fourth, and logs what it dropped; the list is sent back only when fewer than two bullets remain. Every test must follow from the instructions, the examples or the rules of the course's rigor level, which the test request states.

In Rust, a unit about types, methods or traits lists methods under their type (`Stack::push`) and gets its tests as `rust_tests`, `#[test]` functions compiled against the student's crate, instead of encoding the type in strings to suit cases; the contract check leaves Rust functions the harness cannot call to the gate, which rejects a suite whose cases call one. Tests marked `// example` are shown like example cases. The types those tests build (structs with public fields, enums) come with the contract, in `types`: the student gets them as written and the empty stubs hold them, so the tests compile before any code exists.

Fields that come from the plan (hours, difficulty, stretch, platform...) are copied by
coursecrafter, not by the model; editing them in the plan updates the existing contracts on the
next build, with no LLM call.

## Pass 2: the correction suite

With `--tests` (or when you answer yes), each unit gets its tests, generators and scaffold
files. No reference solution is written or run: fixed cases hold small inputs whose results
are obvious, and random tests are laws, generated inputs checked against properties every
correct result keeps (an independent built-in expression, two functions that agree, a
roundtrip, an invariant). proctor draws those inputs from a new seed at every check.

Before the first call, the contract goes through `proctor validate`: a problem no test suite
can fix stops the unit there and drops its cached contract, so that `--tests --only <id>` asks
for a new one. A unit is kept only if `proctor selftest` passes: the tests build and run on
empty stubs without a course error (a wrong name, a type error in a law), and every function
has a test that fails there. The gate also rejects a suite with no laws when the harness can
call one of the unit's functions, or when the unit is a program. Linux units are self-tested in
the Linux container.

Each request carries what the model cannot guess:

- the unit's contract and its language's notes;
- what the student's code may use at this stage, earlier stages' Allowed entries merged;
- for a stage: the earlier stages' functions, types and tests (the tests run again against
  the new program), and the later stages (so it never tests behaviour a later stage changes).

What is mechanical is done by coursecrafter: a Rust unit with a workspace scaffold always gets a `Cargo.toml` (a default one-crate manifest named after the unit or project when the model sends none), and a member crate's own `[workspace]` table is dropped when the root `Cargo.toml` has one (Cargo rejects two workspace roots); C and
Rust programs run as built (a project's other languages keep their first stage's `run`
command), and a `run` pointing into a build folder is rejected.

When the gate rejects a unit, the exact failure goes back to the model, which sends only the
parts it changes: the rest of its earlier reply is kept, so fixing one error cannot break
what already worked. After `--retries` attempts (default 3) the unit is marked FAILED: its
contract stays, without tests, and the build exits non-zero. The following stages of that
project are not generated, since they build on it; they wait for the next run. The cached
replies of a FAILED unit are dropped, so the next run asks the model again instead of
replaying the replies that were rejected.

A reply cut off at the output limit (16000 tokens), or a request the provider rejects on its
own (HTTP 400, 413), fails only that unit. When the provider gives no usable answer (network,
rate limit, quota, server error) or refuses the setup (HTTP 401 to 404: key, permission, model
name, address), the build stops, since every remaining unit would fail the same way.

After a build, the instructions of units the student already unlocked are rendered again from the new specs (workspaces are never touched), so a rebuilt unit shows up without unlocking it again.

## Rewriting the teaching text only

`coursecrafter build --text-only [--only ID]...` rewrites the lesson, the requirements and the Think about list of built units, about one call per unit, and keeps their tests and scaffold. The same contract checks apply.

## What you see and what is logged

The terminal shows the progress (one line per unit and pass), failures, and what to do at the end. Everything else goes to `.proctor/coursecrafter/logs/build-<date>-<time>.log`: each call with its duration and tokens (fresh, cache read, cache write, output), each cached reply, every rejected attempt in full, gate results, and a closing summary. `--debug` prints the log as it is written; its path is shown when a build does not complete.

## Resuming and tokens

A build can be stopped at any time, including by a rate limit or a quota: run the same
command again. Existing contracts and notes are kept, units with a suite are skipped, and
every reply is cached on disk by a hash of its request, so a re-run only pays for what
changed (and for the units that failed). `--only <id>` redoes a unit (or every stage of a
project) from scratch.

The model returns data only (proctor renders the instructions), every request starts with
the same system prompt (cacheable by the provider) holding only the notes for the course's
languages, and earlier units are summarized in one line each. The run ends with a token count.
With `--provider anthropic` the requests use Anthropic's Messages API and mark the system
prompt and the end of the conversation for its prompt cache, so later calls read the system
prompt back at a tenth of the price and a retry reads back the request it repeats. Meridian
ignores such marks and caches on its own.
