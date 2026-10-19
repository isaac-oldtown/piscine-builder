# AGENTS.md

PiscineBuilder: a Cargo workspace (edition 2024, let-chains used freely) with two crates.

- `proctor/`: the offline grader, a library plus the `proctor` CLI. Never uses the network or an LLM.
- `coursecrafter/`: the course builder CLI, the only part that calls an LLM. It depends on the `proctor`
  library and reuses its parsing, rendering and self-test (`proctor::selftest`, `proctor::render`).
- `docs/` is the reference (curriculum format, build, unit spec, languages); keep it and `README.md`
  in sync with behaviour changes.

## Commands

The agent shell on this machine may not have cargo on PATH. Load it first (PowerShell):

```powershell
$env:CARGO_HOME=[Environment]::GetEnvironmentVariable('CARGO_HOME','User'); $env:RUSTUP_HOME=[Environment]::GetEnvironmentVariable('RUSTUP_HOME','User'); $env:Path=[Environment]::GetEnvironmentVariable('Path','Machine')+';'+[Environment]::GetEnvironmentVariable('Path','User')
```

- Verify every change with `cargo fmt; cargo clippy --all-targets -q -- -D warnings; cargo test -q`.
  Clippy is strict (`-D warnings`), and recent lints fire (for example `chunks_exact_to_as_chunks`,
  `regex_creation_in_loops`, `items_after_test_module`: keep `mod tests` last in a file).
- Focused tests: `cargo test -q -p proctor expr`, `cargo test -q -p coursecrafter build`.
- End-to-end grader check with no LLM: in `examples/demo-course`, run
  `..\..\target\debug\proctor.exe validate` and `..\..\target\debug\proctor.exe selftest`, then delete
  `examples/demo-course/.proctor/cache`. To play the demo (`next`, `check`), copy it to a temp folder first.
- The user runs installed binaries: tell them to `cargo install --path proctor` and
  `cargo install --path coursecrafter` after changes.

## Courses and generated state

- Never build or store a course inside this repo; `.gitignore` keeps course files out. The user's real course
  is `..\rust`: do not modify it unless asked.
- A course keeps its hidden data in `.proctor/` (`proctor::course::HIDDEN`), with coursecrafter's state
  (approval hash, LLM reply cache, validator log, `logs/`) in `.proctor/coursecrafter/`. Old `.skillcheck/`
  and `.skillchecker/` folders are migrated on load by `proctor::course::migrate`.
- Student-facing markdown (`subjects/`, `RULES.md`) is rendered by `proctor/src/render.rs` and
  `proctor/src/rules.rs` from unit YAML, never written by the LLM.
- `coursecrafter build` refuses a `curriculum.md` whose hash differs from `.proctor/coursecrafter/approved`.

## LLM and cost

- Local provider: Meridian at `http://127.0.0.1:3456` (`--provider meridian`, health check `/health`).
  Real builds cost calls and can hit the subscription's rate limit; ask before running large ones.
- Replies are cached by a hash of the full request, so editing any prompt text (`coursecrafter/src/prompts.rs`,
  request builders in `build.rs`, the curriculum's `environment:` line) makes every unbuilt unit pay again.
- Contract and test-suite checks live in `coursecrafter/src/build.rs` (`check_contract`, `try_tests`);
  prefer deciding mechanical things in code over adding prompt rules.

## Linux units

`platform: linux` units run in Docker on Windows (`proctor/src/docker.rs`). The image is built from proctor's
own source (`CARGO_MANIFEST_DIR`) and tagged by a hash of it, so any proctor change rebuilds it on the next
Linux check (a few minutes). Inside the container `PROCTOR_IN_CONTAINER` prevents re-delegation.

## Conventions

- Text in docs, prompts and generated instructions uses no long dashes (contracts containing them are rejected).
- Line endings are LF (`.gitattributes`); PowerShell `-replace` and quoted here-strings break easily on Rust
  source with quotes or apostrophes, so prefer the edit tool for code changes.
- Commit with the repo-local identity already configured, one descriptive subject line. Pushing to `origin main`
  is allowed.
