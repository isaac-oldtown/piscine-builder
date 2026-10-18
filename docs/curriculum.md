# Curriculum format and planning rules

A curriculum is one markdown file, `curriculum.md`, in the course folder. `coursecrafter convert`
(from a plan in your own format), `coursecrafter new` and `coursecrafter import` write it, you can
edit it by hand, and `coursecrafter prompt` prints the complete specification given to the planner.

`convert` transcribes rather than plans: it keeps your units, order, scope and details (in the
briefs), uses every value you give, fills only the required keys you left out, and fixes validator
errors with the smallest change (completing `introduces` and `assumes`, adding a note). It skips
the planner's self-review, which would restructure the plan, and lists what it added, interpreted
or suggests. Planning warnings may remain: they are advice, and your choices win.

## Format

Front matter, then one section per entry:

```markdown
---
title: C from scratch
language: c
level: beginner
hours_per_week: 15
weeks: 2
environment: "Ubuntu 24.04, gcc, valgrind, make"   # optional: when students use another machine
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

Free-text brief for the exercise writer: scope, design decisions, pitfalls.
```

Entry ids:

| Id | Heading | What it is |
|---|---|---|
| `dNN-slug` | `##` | A drill: small, one concept, one sitting. |
| `pNN-slug` | `##` | A project, built in stages that follow it. |
| `pNN.k-slug` | `###` | Stage k of project pNN. The last required stage is the finale. |
| `note-slug` | `##` | Half a page explaining an easy notion, revealed with a unit. |

Keys of a drill or stage:

| Key | Meaning |
|---|---|
| `goal` | One sentence: what the student practises (required). |
| `concept_difficulty` | 1 to 5: how hard the ideas are to understand (required). |
| `difficulty` | 1 to 5: how hard it is to implement and debug (required). |
| `hours` | Honest estimate including testing and debugging (required; never shown to the student). |
| `done_when` | The exit criterion: the observable behaviour that shows the essential skills. |
| `introduces`, `assumes` | Concepts and tools taught here, and needed here. |
| `target` | `function` (the grader calls the functions; C, Python, Rust) or `program`. |
| `scaffold` | `none`, `minimal` (default) or `full`. |
| `allows` | Libraries, modules or functions the student may use besides their own code. |
| `stretch` | `true`: optional, the student may skip it. |
| `guided` | `true` (drills only): the instructions walk the student through it step by step. The first unit of the course's language should be one, unless the student already writes it; drills may start at `d00` for it. |
| `platform` | `linux`: needs Linux APIs; on other systems it runs in a container. |
| `gradable` | `false` only when no program can check the result. |
| `language` | Only when different from the course's. |

A project has `goal`, `difficulty`, `assumes`, `language`, `platform` and `minimum`
(required: what it must do to count as a success); its brief states what is in and out of
scope. A note has `teaches` and `with` (the unit it is revealed with).

## What the validator enforces

- **Teach before use:** every `assumes` is introduced by an earlier unit or taught by a
  note revealed by then.
- **Workload:** every unit has `hours` and both difficulties; required hours fit
  `hours_per_week` x `weeks` with 10 to 15% left for debugging (warning above 88%, error
  above 100%).
- **Stretch goals:** nothing required assumes what only a stretch goal introduces, a
  project's first stage is never a stretch goal, and no required stage follows a stretch
  stage.
- **Minimum viable result:** every project states its `minimum`.
- **Stages:** every earlier stage is tested again at each later one, so a stage never says
  what it does with a feature a later stage adds (a `not supported yet` message is an
  error), and once a stage needs Linux every later stage does too.
- **Warnings:** a missing `done_when`, drills nothing later uses, a unit over 16 hours, too
  many units at difficulty 5 or at one level, a hard stage that introduces three or more
  concepts or relies on one only a note covers, a unit relying on a tool this machine lacks.

Before validating, coursecrafter fixes mechanical slips itself: numbers that do not grow,
stages numbered out of sequence (references follow the new ids), a missing `hours` (filled
from the difficulty), and values YAML would misread, which get quoted. Fixes and problems
are logged to `.proctor/coursecrafter/validator.log`.

## How the planner plans

The planning prompt asks for generous estimates on routinely underestimated topics
(ownership and memory, parsing, processes and signals, concurrency), a small drill before
each hard project stage, the test tool within the first three units, testing time inside
every estimate, one program per project, portable units unless Linux is needed, and a
consolidation stage at the end (refactor, tests, documented limitations).

`coursecrafter new` asks at most six questions, one per reply, each built on the previous
answers. Its first valid draft is reviewed by the planner against a twelve-point checklist
(prerequisites, progression, testing, purpose, workload, minimum viable projects,
consolidation, exit criteria, difficulty, design, fit, missing concepts), and it lists
what it changed. You then approve the plan, ask for changes (typed directly at the menu),
edit the file yourself, or quit and keep the draft.

## Environment

coursecrafter assumes students work on the machine that builds the course. `coursecrafter env`
shows what it detected: the OS, about 25 tools with their versions, facts it tests for real
(AddressSanitizer, a POSIX bash), and Linux access (native, Docker, WSL). When students use
another machine, declare it with `environment:` in the front matter: the planner and every
generation request take it as authoritative, while tests are still verified on the build
machine (Linux units in the container).
