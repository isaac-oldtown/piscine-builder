# AGENTS.md

PiscineBuilder: a uv workspace (Python 3.12, typed, checked by mypy) with two packages.

- `proctor/`: the offline grader, a library plus the `proctor` CLI. Never uses the network or an LLM.
- `coursecrafter/`: the course builder CLI, the only part that calls an LLM. It depends on the `proctor`
  library and reuses its parsing, rendering and self-test (`proctor.selftest`, `proctor.render`).
- `docs/` is the reference (curriculum format, build, unit spec, languages); keep it and `README.md`
  in sync with behaviour changes.
- The code is a port of the Rust version, archived on branch `rust` (tag `rust-final`) and no longer
  developed.

## Commands

- Verify every change with `uv run ruff format; uv run ruff check; uv run mypy; uv run pytest`.
  mypy is strict (untyped definitions are errors). Its incremental cache can go stale when several agents
  work at once and report a missing module attribute that is not missing: rerun with
  `--cache-dir` pointing to a fresh folder, or `--no-incremental`.
- Focused tests: `uv run pytest proctor/tests/test_expr.py`, `uv run pytest coursecrafter/tests/test_build.py`.
  The root `conftest.py` imports both packages first; keep `--import-mode=importlib`. Integration tests
  share scratch-course helpers through `proctor/tests/common.py` (on pytest's `pythonpath`);
  `test_grader.py` holds grader behaviour on edited copies of the demo course.
- End-to-end grader check with no LLM: `uv run pytest proctor/tests/test_demo_course.py` (part of the suite,
  about 15 s). It copies the demo course to the temp folder, runs `validate` and the build gate's selftest on
  every unit, checks that the known-good solutions in `proctor/tests/demo_solutions/` (test fixtures only:
  courses have no reference solution) pass under several seeds, and that runaway output and print loops are
  stopped; units whose toolchain is missing are skipped. By hand: in `examples/demo-course`, run
  `uv run proctor validate` and `uv run proctor selftest`, then delete `examples/demo-course/.proctor/cache`.
  To play the demo (`next`, `check`), copy it to a temp folder first.
- `uv run` puts `.venv\Scripts` on PATH; both CLIs drop it at start (`compat.leave_own_venv`), so the
  programs they find (the student's `python`) and coursecrafter's environment block, hence the reply cache
  key, match the installed tools. Code that probes tools outside `main()` (tests) still sees the venv.
- CI (`.github/workflows/ci.yml`) runs ruff, mypy and pytest on Ubuntu and Windows at every push. It is the
  only place the code under `sys.platform != "win32"` runs; to try it locally, run the same commands in a
  Docker container with uv. Put the commands in a script in a folder and mount the folder (Docker Desktop
  turns a single-file mount into an empty directory), and note that the agent shell's `$env:TEMP` is msys2's
  `tmp`, not `AppData\Local\Temp`. Its GCC 14 rejects what Ubuntu's GCC 13 only warns about (implicit
  declarations under `-std=c11`).
- PowerShell redirection rewrites output as CRLF and inline `python -c` with quotes breaks easily: write
  scratch scripts to a file, and capture output from Python when bytes matter.
- The user runs installed tools: `uv tool install --editable ./proctor` and
  `uv tool install --editable ./coursecrafter`. They are editable, so changes need no reinstall, except
  after a change to a `pyproject.toml` (dependencies, scripts): then reinstall with `--force`.

## Courses and generated state

- Never build or store a course inside this repo; `.gitignore` keeps course files out. The user's real course
  is `..\rust`: do not modify it unless asked.
- A course keeps its hidden data in `.proctor/` (`proctor.course.HIDDEN`), with coursecrafter's state
  (approval hash, LLM reply cache, validator log, `logs/`) in `.proctor/coursecrafter/`.
- Student-facing markdown (`subjects/`, `RULES.md`) is rendered by `proctor/src/proctor/render.py` and
  `proctor/src/proctor/rules.py` from unit YAML, never written by the LLM.
- `coursecrafter build` refuses a `curriculum.md` whose hash differs from `.proctor/coursecrafter/approved`.

## LLM and cost

- Local provider: Meridian at `http://127.0.0.1:3456` (`--provider meridian`, health check `/health`).
  `Llm.wake` pings `/health` once per run before the first call to a loopback provider, because an idle
  Meridian refuses the first request.
  Real builds cost calls and can hit the subscription's rate limit; ask before running large ones.
  Meridian serves both `/chat/completions` and Anthropic's `/messages` but strips `cache_control` (Claude
  Code caches on its own), so prompt-cache marks only matter for `--provider anthropic` (`llm.Api.MESSAGES`).
- Replies are cached by a hash of the full request, so editing any prompt text
  (`coursecrafter/src/coursecrafter/prompts.py`, request builders in `build.py`, the curriculum's
  `environment:` line) makes every unbuilt unit pay again. A unit that fails drops its cached replies
  (`Builder.forget`), so the next run asks again.
- `llm.Failure` decides what a failed call does: `UNAVAILABLE` (network, 408, 429, 5xx, unusable reply) and
  `SETUP` (401 to 404) stop the build; `REQUEST` (other 4xx, a reply cut off at `MAX_TOKENS`, an empty reply)
  fails only that unit.
- Contract and test-suite checks live in `coursecrafter/src/coursecrafter/build.py` (`check_contract`,
  `try_tests`, `salvage` (drops what it cannot use instead of asking again), `cross_check` behind
  `--cross-check`); the cross-check's questions, answer parsing and case/law consistency are in
  `crosscheck.py`. Prefer deciding mechanical things in code over adding prompt rules.

## Linux units

`platform: linux` units run in Docker on Windows (`proctor/src/proctor/docker.py`). The image is built from
proctor's own package folder and tagged by a hash of it, so any proctor change rebuilds it on the next Linux
check (a few minutes). Inside the container `PROCTOR_IN_CONTAINER` prevents re-delegation.

## Conventions

- Text in docs, prompts and generated instructions uses no long dashes (contracts containing them are rejected).
- Line endings are LF (`.gitattributes`); PowerShell `-replace` and quoted here-strings break easily on
  source with quotes or apostrophes, so prefer the edit tool for code changes.
- Commit with the repo-local identity already configured, one descriptive subject line. Pushing to
  `origin main` is allowed; never commit to the archived `rust` branch.
