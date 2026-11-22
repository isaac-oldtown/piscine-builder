"""`coursecrafter build`: curriculum -> course.
  pass 1: one contract per unit (and notes, and settings for unknown languages)
  pass 2: tests, random laws, scaffold, in one call; repaired in code (tests
          proctor cannot run and cases that contradict a law are dropped), then
          gated by proctor selftest on empty stubs; with --cross-check, an
          independent reading of the instructions answers every test too
Each LLM reply is cached on disk by a hash of its request, so a re-run only
pays for what changed. Nothing that fails the gate is kept."""

from __future__ import annotations

import copy
import math
import os
import re
import shutil
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from proctor.compat import (
    U64_MASK,
    byte_len,
    io_error,
    lines,
    read_text,
    regex,
    trim,
    trim_end,
    trim_start,
    trim_start_matches,
    write_text,
)
from proctor.errors import Error
from proctor.unit import LangConfig, Rule, Target, Unit, requirements_markdown

from coursecrafter import crosscheck, curriculum, llm, prompts, session
from proctor import (
    course,
    docker,
    engine,
    gens,
    journal,
    lang,
    program,
    rtests,
    scaffold,
    schema,
    selftest,
    sig,
    structure,
    yamlio,
)

if TYPE_CHECKING:
    # In the class body, `llm` names the `Builder.llm` field, not the module.
    from coursecrafter.llm import Llm, Msg

# A `yaml_serde::Mapping`: a dict in insertion order.
Mapping = dict[Any, Any]


@dataclass(kw_only=True)
class Options:
    retries: int
    tests: bool | None
    only: list[str]
    yes: bool
    # Rewrite only the teaching text (lesson, requirements, Think about).
    text_only: bool
    # Print the session log to the terminal as it is written.
    debug: bool
    # Ask an independent reading of the instructions about every test (one more call per unit).
    cross_check: bool = False


@dataclass(kw_only=True)
class Builder:
    root: Path
    plan: curriculum.Plan
    llm: Llm
    opts: Options
    # Calls, cache hits, rejections and timings of this run.
    log: session.Session
    # Set when the provider cannot serve this run (no usable answer, or a
    # refused key, model or address): the build stops instead of failing
    # every remaining unit the same way.
    provider_down: bool
    # Generation rules plus the student environment: one stable system
    # prompt for the whole run, so providers can cache it.
    system: str
    # Answers the cross-check instead of `llm` (a free local model).
    checker: Llm | None = None

    @staticmethod
    def new(
        root: Path,
        plan: curriculum.Plan,
        llm: Llm,
        opts: Options,
        env_block: str,
        checker: Llm | None = None,
    ) -> Builder:
        langs = sorted({plan.language(u) for u in plan.units()})
        system = f"{prompts.gen_system(langs, opts.cross_check)}\n\n{env_block}"
        log = session.Session.start(root, "build", opts.debug)
        log.log(f"model {llm.model} at {llm.base_url}")
        if checker is not None:
            log.log(f"cross-check by {checker.model} at {checker.base_url}")
        return Builder(
            root=root,
            plan=plan,
            llm=llm,
            log=log,
            opts=opts,
            provider_down=False,
            system=system,
            checker=checker,
        )

    def explicit(self, e: curriculum.Entry) -> bool:
        """Named by `--only`: redone even if already built."""
        return bool(self.opts.only) and self.selected(e)

    def has_suite(self, id: str) -> bool:
        """A unit whose correction suite already passed the gate: its spec holds
        tests (a failed unit is restored without them)."""
        m = self.read_unit_map(id)
        if m is None:
            return False
        return "cases" in m or "rust_tests" in m or "random" in m

    def selected(self, e: curriculum.Entry) -> bool:
        return not self.opts.only or any(names(o, e) for o in self.opts.only)

    def chat(self, label: str, messages: list[Msg], via: Llm | None = None) -> str:
        """Chat (with `via`, by default the build's model) with on-disk caching
        keyed by the full request."""
        model = via or self.llm
        parts = [model.model]
        parts.extend(m.content for m in messages)
        key = fnv(parts)
        path = self.reply_cache() / f"{label}-{key}.txt"
        try:
            text = read_text(path)
        except OSError:
            pass
        else:
            self.log.cached(label)
            return text
        purpose = label.split("-")[0]
        try:
            reply = self.log.chat_as(model, label, purpose, messages)
        except llm.ChatError as e:
            if e.kind == llm.Failure.REQUEST:
                raise Error(e.msg) from None
            self.provider_down = True
            if e.kind == llm.Failure.UNAVAILABLE:
                raise Error(
                    f"{e.msg}\nThe provider gave no usable answer (stopped, quota, rate limit or an error on its side). Everything finished so far is saved: run the same command again to resume; finished replies are reused at no cost."
                ) from None
            raise Error(
                f"{e.msg}\nThe provider refused the request: check the provider, model, base URL and API key (`coursecrafter config` shows the configuration file). Everything finished so far is saved."
            ) from None
        write(path, reply)
        return reply

    def reply_cache(self) -> Path:
        return hidden(self.root) / "coursecrafter" / "cache"

    def forget(self, label: str) -> None:
        """Drops every cached reply of `label` (`tests-d03`) once its unit has
        failed, so the next run asks the model again instead of replaying the
        replies that were rejected."""
        try:
            entries = list(os.scandir(self.reply_cache()))
        except OSError:
            return
        n = 0
        for e in entries:
            if cached_reply_of(e.name, label):
                try:
                    os.remove(e.path)
                except OSError:
                    continue
                n += 1
        if n > 0:
            self.log.log(f"  dropped {n} cached reply(ies) of {label}: the next run asks again")

    def unit_path(self, id: str) -> Path:
        return hidden(self.root) / "units" / f"{id}.yaml"

    def read_unit_map(self, id: str) -> Mapping | None:
        try:
            text = read_text(self.unit_path(id))
        except OSError:
            return None
        try:
            v = yamlio.load(text)
        except Error:
            return None
        return v if isinstance(v, dict) else None

    def existing_languages(self) -> Mapping:
        p = hidden(self.root) / "course.yaml"
        try:
            text = read_text(p)
        except OSError:
            return {}
        try:
            v = yamlio.load(text)
        except Error:
            return {}
        if isinstance(v, dict):
            langs = v.get("languages")
            if isinstance(langs, dict):
                return dict(langs)
        return {}

    def write_course(self, units: list[curriculum.Entry], languages: Mapping) -> None:
        """Writes course.yaml listing `units` (those whose files exist so far)."""
        meta = self.plan.meta
        m: Mapping = {}
        m["title"] = meta.title
        if meta.language is not None:
            m["language"] = meta.language.lower()
        if meta.hours_per_week is not None:
            m["hours_per_week"] = meta.hours_per_week
        if meta.weeks is not None:
            m["weeks"] = meta.weeks
        m["rigor"] = self.rigor()
        cv = meta.conventions
        if cv is not None and trim(cv):
            m["conventions"] = trim(cv)
        m["units"] = seq([u.id for u in units if self.unit_path(u.id).is_file()])
        projects: Mapping = {}
        for p in (e for e in self.plan.entries if e.kind == curriculum.Kind.PROJECT):
            pm: Mapping = {}
            pm["title"] = p.title
            pm["goal"] = p.f.goal if p.f.goal is not None else ""
            if p.f.minimum is not None:
                pm["minimum"] = p.f.minimum
            pm["language"] = self.plan.language(p)
            if p.f.difficulty is not None:
                pm["difficulty"] = p.f.difficulty
            pm["prereqs"] = seq(self.plan.prereqs(p))
            concepts: list[str] = []
            for st in self.plan.stages(p.id):
                for c in st.f.introduces:
                    if c not in concepts:
                        concepts.append(c)
            pm["concepts"] = seq(concepts)
            projects[p.id] = pm
        m["projects"] = projects
        if languages:
            m["languages"] = dict(languages)
        text = yamlio.dump(m)
        write(hidden(self.root) / "course.yaml", text)

    def language_info(self, lang: str, languages: Mapping) -> str:
        match lang:
            case "c":
                return "c (preset): compiled with cc -std=c11 -Wall -Wextra -Werror; function target available."
            case "python":
                return 'python (preset): Python 3; function target available; program target needs run: e.g. "python -m pkg" or "python main.py".'
            case "rust":
                return "rust (preset): cargo build, cargo fmt --check, cargo clippy -D warnings; function target available (library crate). Program target runs the built binary."
            case "bash" | "sh" | "shell":
                return 'bash (preset): bash -n syntax check; program target only; set run: e.g. "bash script.sh".'
            case other:
                block = yamlio.dump(languages[other]) if other in languages else ""
                return f"{other} (course settings, program target only):\n```yaml\n{block}```"

    def run(self) -> bool:
        for o in self.opts.only:
            if not any(names(o, e) for e in self.plan.entries):
                raise Error(
                    f"--only {o}: no unit or project has that id (use `d03`, `d03-refs-slices`, `p02` or `p02-minishell`)"
                )
        h = hidden(self.root)
        for d in ["units", "notes", "scaffold", "coursecrafter/cache"]:
            try:
                (h / d).mkdir(parents=True, exist_ok=True)
            except OSError as e:
                raise Error(io_error(e)) from None
        # Reference solutions of older builds: proctor no longer uses them.
        shutil.rmtree(h / "refs", ignore_errors=True)
        languages = self.existing_languages()
        needed: set[str] = set()
        for u in self.plan.units():
            needed.add(self.plan.language(u))
        for lang_name in sorted(needed):
            if has_preset(lang_name) or lang_name in languages:
                continue
            self.log.say(f"[language] {lang_name}: writing course settings")
            block = self.language_settings(lang_name)
            languages[lang_name] = block

        # Pass 1: contracts.
        units = self.plan.units()
        no_contract: list[str] = []
        new_contracts = 0
        for i, u in enumerate(units):
            # Existing contracts are kept (rewriting one would drop its tests);
            # `--only` redoes the named units.
            changed = None if self.opts.text_only else self.run_changed(u)
            if (
                self.unit_path(u.id).is_file()
                and (not self.explicit(u) or self.opts.text_only)
                and changed is None
            ):
                self.sync_contract(u)
                self.ensure_rust_scaffold(u)
                continue
            if changed is not None:
                self.log.say(
                    f"[contract {i + 1}/{len(units)}] {u.id}: written again, its run command differs from {changed}'s"
                )
            else:
                self.log.say(f"[contract {i + 1}/{len(units)}] {u.id}")
            # A rejected contract is reported, and the other units go on.
            try:
                m = self.contract(u, units[:i], languages)
            except Error as e:
                if self.provider_down:
                    raise
                self.log.say(f"  FAILED {e.msg}")
                self.forget(f"contract-{u.id}")
                no_contract.append(u.id)
                continue
            text = yamlio.dump(m)
            write(self.unit_path(u.id), text)
            new_contracts += 1
            for n in self.plan.notes_for(u.id):
                path = h / "notes" / f"{note_slug(n.id)}.md"
                if path.is_file() and not self.explicit(u):
                    continue
                self.log.say(f"[note] {n.id}")
                body = self.note(n)
                write(path, body)
        self.write_course(units, languages)
        self.log.log(
            f"contracts ready for {len(units) - len(no_contract)} unit(s). {self.llm.usage_line()}"
        )
        self.log.notes.append(
            f"contracts   {new_contracts} written, {len(units) - new_contracts - len(no_contract)} kept, {len(no_contract)} rejected"
        )
        if no_contract:
            self.log.say(
                f"{len(no_contract)} unit(s) have no contract because it was rejected every time: {', '.join(no_contract)}. They are left out of the course for now; run `coursecrafter build --only <id>` to retry."
            )

        if self.opts.text_only:
            failed: list[str] = []
            chosen = [u for u in units if self.selected(u)]
            for u in chosen:
                if not self.unit_path(u.id).is_file() or u.f.gradable is False:
                    continue
                self.log.say(f"[text] {u.id}")
                try:
                    self.rewrite_text(u, languages)
                except Error as e:
                    if self.provider_down:
                        raise
                    self.log.say(f"  FAILED {u.id}: {e.msg}")
                    self.forget(f"text-{u.id}")
                    failed.append(u.id)
            self.log.log(self.llm.usage_line())
            self.refresh_unlocked()
            return not failed and not no_contract
        if self.opts.tests is not None:
            want_tests = self.opts.tests
        elif self.opts.yes:
            want_tests = True
        else:
            from coursecrafter import cli

            want_tests = self.log.wait_user(
                lambda: cli.ask_yes("Generate the correction suite (tests and random laws)?")
            )
        if not want_tests:
            self.log.say(
                "Skipped the correction suite: proctor will check structure, prototypes, allowed and the build only. Run `coursecrafter build --tests` later to add it."
            )
            self.refresh_unlocked()
            return not no_contract

        # Pass 2: tests, unit by unit, each gated.
        linux_pending = not sys.platform.startswith("linux") and any(
            self.selected(u)
            and self.plan.platform(u) is not None
            and (not self.has_suite(u.id) or self.explicit(u))
            for u in units
        )
        if linux_pending:
            # Linux units are gated in a container: fail early, not as rejected attempts.
            try:
                docker.image()
            except Error as e:
                raise Error(
                    f"{e.msg}\n(the Linux units are tested in a Docker container)"
                ) from None
        failed = []
        waiting: list[str] = []
        done_before = 0
        built = 0
        for i, u in enumerate(units):
            if not self.selected(u):
                continue
            if not self.unit_path(u.id).is_file():
                continue
            if u.f.gradable is False:
                self.log.say(f"[tests {i + 1}/{len(units)}] {u.id}: not gradable, skipped")
                continue
            again = ""
            if self.has_suite(u.id) and not self.explicit(u):
                if not self.opts.cross_check or self.verified(u.id):
                    done_before += 1
                    continue
                again = ": its suite was never cross-checked, built again"
            # A stage's tests build on the previous stage's: without them they
            # would be written blind and fail for nothing.
            prev = self.previous_stage(u)
            if prev is not None and prev.f.gradable is not False and not self.has_suite(prev.id):
                self.log.say(
                    f"[tests {i + 1}/{len(units)}] {u.id}: waits for {prev.id}, which has no correction suite yet"
                )
                waiting.append(u.id)
                continue
            self.log.say(f"[tests {i + 1}/{len(units)}] {u.id}{again}")
            try:
                self.tests(u, units[: i + 1], languages)
            except Error as e:
                if self.provider_down:
                    self.write_course(units, languages)
                    raise
                self.log.say(f"  FAILED {u.id}: {e.msg}")
                self.forget(f"tests-{u.id}")
                self.forget(f"verify-{u.id}")
                failed.append(u.id)
            else:
                built += 1
        self.log.notes.append(
            f"suites      {built} built, {done_before} kept, {len(failed)} failed, {len(waiting)} waiting"
        )
        if done_before > 0:
            self.log.say(
                f"{done_before} unit(s) already had a correction suite and were kept (`--only <id>` redoes one)"
            )
        self.write_course(units, languages)
        self.log.log(self.llm.usage_line())
        if failed:
            self.log.say(
                f"{len(failed)} unit(s) have no correction suite because they never passed the gate: {', '.join(failed)}. Run `coursecrafter build --only <id>` to retry."
            )
        if waiting:
            self.log.say(
                f"{len(waiting)} stage(s) were not built because an earlier stage of their project has no suite: {', '.join(waiting)}. They are built once it passes."
            )
        self.refresh_unlocked()
        return not failed and not waiting and not no_contract

    def refresh_unlocked(self) -> None:
        """Units the student already unlocked get their instructions rendered
        again from the new specs; workspaces and notes are never overwritten."""
        try:
            c = course.CourseDir.load(self.root)
        except Error:
            return
        state = c.state()
        n = 0
        for u in (u for u in c.units if u.id in state.unlocked):
            try:
                scaffold.reveal(c, u)
            except Error:
                continue
            n += 1
        if n > 0:
            self.log.say(f"refreshed the instructions of {n} unlocked unit(s) in subjects/")

    def ensure_rust_scaffold(self, u: curriculum.Entry) -> None:
        """A Rust unit with a workspace scaffold always gets a Cargo.toml, the
        default one when the model sent none. A project's stages share one
        workspace, so only its first stage needs one."""
        if self.plan.language(u) != "rust" or u.f.scaffold == "none" or u.f.gradable is False:
            return
        if self.previous_stage(u) is not None:
            return
        target = hidden(self.root) / "scaffold" / u.id / "Cargo.toml"
        if not target.is_file():
            write(target, default_cargo_toml(selftest.crate_name(u.parent or u.id)))

    def rewrite_text(self, u: curriculum.Entry, languages: Mapping) -> None:
        """New lesson, requirements and Think about for a built unit; its
        contract fields, tests and scaffold stay as they are."""
        old = self.read_unit_map(u.id)
        if old is None:
            raise Error("no contract yet")
        contract = dict(old)
        for k in [
            "lesson",
            "requirements",
            "think_about",
            "cases",
            "rust_tests",
            "random",
            "example",
        ]:
            _remove(contract, k)
        contract_text = yamlio.dump(contract)
        cases = all_cases(old)
        req = prompts.text_request(
            contract_text,
            self.entry_text(u),
            self.language_info(self.plan.language(u), languages),
            cases,
        )
        messages = [llm.Msg.system(self.system), llm.Msg.user(req)]
        last = ""
        for _ in range(self.opts.retries + 1):
            reply = self.chat(f"text-{u.id}", messages)
            m = dict(old)
            for k, tag in [("lesson", "lesson"), ("requirements", "requirements")]:
                b = llm.block(reply, tag)
                if b is not None and trim(b):
                    m[k] = trim(b)
                else:
                    _remove(m, k)
            think: list[str] = []
            tb = llm.block(reply, "think_about")
            if tb is not None:
                try:
                    v = llm.parse_yaml(tb)
                except Error:
                    v = None
                if isinstance(v, list):
                    think = _strs(v)
            problems: list[str] = []
            try:
                kept, dropped = clean_think_about(think, cases)
            except Error as e:
                problems.append(e.msg)
            else:
                for d in dropped:
                    self.log.log(f"  think_about: dropped {d}")
                if not kept:
                    _remove(m, "think_about")
                else:
                    m["think_about"] = seq(kept)
            problems.extend(self.check_contract(u, m, languages))
            if not problems:
                text = yamlio.dump(m)
                write(self.unit_path(u.id), text)
                return
            last = "\n".join(problems)
            self.log.log(f"  rejected: {last}")
            messages.append(llm.Msg.assistant(reply))
            messages.append(
                llm.Msg.user(
                    prompts.feedback(last, "the <lesson>, <requirements> and <think_about> blocks")
                )
            )
        raise Error(llm.cut(last, 400))

    def rigor(self) -> str:
        r = self.plan.meta.rigor
        match trim(r) if r is not None else None:
            case "relaxed":
                return "relaxed"
            case "standard":
                return "standard"
            case _:
                return "strict"

    def previous_stage(self, u: curriculum.Entry) -> curriculum.Entry | None:
        if u.parent is None:
            return None
        stages = self.plan.stages(u.parent)
        pos = next((i for i, x in enumerate(stages) if x.id == u.id), None)
        if pos is None:
            return None
        # A reading stage (the project's introduction) builds nothing.
        built = [x for x in stages[:pos] if x.f.gradable is not False]
        return built[-1] if built else None

    def language_settings(self, lang_name: str) -> Any:
        messages = [
            llm.Msg.system(self.system),
            llm.Msg.user(prompts.language_request(lang_name)),
        ]
        label = f"lang-{lang_name}"
        for _ in range(self.opts.retries + 1):
            reply = self.chat(label, messages)
            y = llm.yaml_block(reply, "language")
            if y is None:
                problem = "missing <language> block"
            else:
                try:
                    cfg = schema.load(LangConfig, y)
                except Error as e:
                    problem = f"not valid settings: {e.msg}"
                else:
                    if cfg.run is None:
                        problem = "settings need `run`"
                    else:
                        bad = next((p for p in cfg.imports or [] if not _regex_ok(p)), None)
                        if bad is not None:
                            problem = f"bad regex `{bad}`"
                        else:
                            return yamlio.load(y)
            messages.append(llm.Msg.assistant(reply))
            messages.append(llm.Msg.user(prompts.feedback(problem, "the <language> block")))
        self.forget(label)
        raise Error(f"could not get usable settings for {lang_name}")

    def note(self, n: curriculum.Entry) -> str:
        messages = [
            llm.Msg.system(self.system),
            llm.Msg.user(prompts.note_request(n.title, n.f.teaches, n.brief)),
        ]
        reply = self.chat(f"note-{n.id}", messages)
        body = llm.block(reply, "note")
        if body is None:
            body = reply
        return body.replace("\u2014", ":").replace("\u2013", ":") + "\n"

    def context(self, earlier: list[curriculum.Entry]) -> str:
        s = f"Course: {self.plan.meta.title}"
        if self.plan.meta.level is not None:
            s += f" (level: {self.plan.meta.level})"
        s += "\n"
        for e in earlier:
            funcs = ""
            m = self.read_unit_map(e.id)
            if m is not None and isinstance(m.get("functions"), list):
                funcs = "; ".join(
                    i["sig"]
                    for i in m["functions"]
                    if isinstance(i, dict) and isinstance(i.get("sig"), str)
                )
            s += f"- {e.id}: {e.title}. introduces: {', '.join(e.f.introduces)}{'' if not funcs else f'. functions: {funcs}'}\n"
        return s

    def entry_text(self, u: curriculum.Entry) -> str:
        f = u.f
        s = f"id: {u.id}\ntitle: {u.title}\ngoal: {f.goal if f.goal is not None else ''}\ndifficulty: {f.difficulty if f.difficulty is not None else 1}\n"
        s += f"language: {self.plan.language(u)}\n"
        for k, v in [
            ("introduces", f.introduces),
            ("assumes", f.assumes),
            ("allows", f.allows),
        ]:
            if v:
                s += f"{k}: [{', '.join(v)}]\n"
        if f.target is not None:
            s += f"target: {f.target} (fixed)\n"
        kept = self.stage_run(u)
        if kept is not None and f.target != "function":
            s += f"run: {kept[1]} (fixed by {kept[0]}: every stage of the project runs this way, so keep the files it runs)\n"
        if f.done_when is not None and trim(f.done_when):
            s += f"done_when: {f.done_when}\n"
        if f.stretch:
            s += "stretch: true (optional unit; the student may skip it)\n"
        if self.plan.platform(u) is not None:
            s += "platform: linux (built, run and tested on Linux: Ubuntu 24.04, /bin/sh is dash, GNU coreutils)\n"
        if f.gradable is False:
            s += READING_REQUEST
        else:
            s += lesson_request(
                self.plan.language(u),
                f.guided,
                f.introduces,
                f.assumes,
            )
        notes = [f"{n.title} (teaches {', '.join(n.f.teaches)})" for n in self.plan.notes_for(u.id)]
        if notes:
            s += f"revealed with notes: {'; '.join(notes)}\n"
        if u.brief:
            s += f"brief: {u.brief}\n"
        return s

    def project_text(self, u: curriculum.Entry) -> str:
        pid = u.parent
        if pid is None:
            return ""
        p = self.plan.entry(pid)
        if p is None:
            return ""
        s = f"# Project {p.id}: {p.title}\nGoal: {p.f.goal if p.f.goal is not None else ''}\nMinimum viable result (the required stages deliver exactly this): {p.f.minimum if p.f.minimum is not None else 'not stated'}\nStages:\n"
        for st in self.plan.stages(pid):
            mark = "  <- this one" if st.id == u.id else ""
            s += f"- {st.id}: {st.title}: {st.f.goal if st.f.goal is not None else ''}{mark}\n"
        s += "All stages share one workspace; each stage builds on the previous one.\n\n"
        return s

    def sync_contract(self, u: curriculum.Entry) -> None:
        """A kept contract takes the plan's fixed fields (platform, hours,
        stretch...) again, so editing those in the plan needs no regeneration."""
        old = self.read_unit_map(u.id)
        if old is None:
            return
        m = self.merge_contract(u, old)
        for k in ["cases", "rust_tests", "random", "think_about", "example"]:
            if k in old:
                m[k] = old[k]
        if not _yaml_eq(m, old):
            text = yamlio.dump(m)
            write(self.unit_path(u.id), text)

    def merge_contract(self, u: curriculum.Entry, model: Mapping) -> Mapping:
        """Fixed fields first, then the model's contract."""
        m: Mapping = {}
        m["id"] = u.id
        m["title"] = u.title
        if u.parent is not None:
            m["project"] = u.parent
        m["language"] = self.plan.language(u)
        if u.f.difficulty is not None:
            m["difficulty"] = u.f.difficulty
        if u.f.concept_difficulty is not None:
            m["concept_difficulty"] = u.f.concept_difficulty
        if u.f.hours is not None:
            m["hours"] = u.f.hours
        if u.parent is None:
            m["prereqs"] = seq(self.plan.prereqs(u))
        m["concepts"] = seq(u.f.introduces)
        m["introduces"] = seq(u.f.introduces)
        m["assumes"] = seq(u.f.assumes)
        notes = [note_slug(n.id) for n in self.plan.notes_for(u.id)]
        if notes:
            m["notes"] = seq(notes)
        m["scaffold"] = u.f.scaffold if u.f.scaffold is not None else "minimal"
        if u.f.stretch:
            m["stretch"] = True
        p = self.plan.platform(u)
        if p is not None:
            m["platform"] = p
        if u.f.guided:
            m["guided"] = True
        # Plan keys the model may echo back; they are not part of a unit spec.
        plan_only = [
            "done_when",
            "guided",
            # Written with the tests (examples come from them, Think about avoids them).
            "example",
            "think_about",
            "kind",
            "gradable",
            "allows",
            "teaches",
            "with",
            "minimum",
        ]
        fixed = [
            "platform",
            "id",
            "title",
            "project",
            "language",
            "difficulty",
            "prereqs",
            "concepts",
            "introduces",
            "assumes",
            "notes",
            "scaffold",
            "stretch",
            "concept_difficulty",
            "hours",
            "cases",
            "rust_tests",
            "random",
        ]
        if u.f.gradable is False:
            # Reading only: the goal and the text, nothing to hand in.
            m["reading"] = True
            m["target"] = "program"
            m["scaffold"] = "none"
            goal = model.get("goal")
            m["goal"] = goal if isinstance(goal, str) and trim(goal) else (u.f.goal or "")
            if "lesson" in model:
                m["lesson"] = model["lesson"]
            return m
        if u.f.target is not None:
            m["target"] = u.f.target
        for k, v in model.items():
            key = k if isinstance(k, str) else ""
            if key in fixed or key in plan_only or (key == "target" and u.f.target is not None):
                continue
            m[k] = v
        # How the program runs is decided here, not by the model: C and Rust
        # run what they built, and a project keeps its first command.
        if self.plan.language(u) in ("c", "rust"):
            _remove(m, "run")
            _remove(m, "build")
        else:
            kept = self.stage_run(u)
            if kept is not None:
                m["run"] = kept[1]
        return m

    def stage_run(self, u: curriculum.Entry) -> tuple[str, Any] | None:
        """The `run` of the first earlier stage of `u`'s project that has one,
        with that stage's id: every later stage runs the same way."""
        if u.parent is None or self.plan.language(u) in ("c", "rust"):
            return None
        for st in self.plan.stages(u.parent):
            if st.id == u.id:
                break
            fm = self.read_unit_map(st.id)
            if fm is not None and "run" in fm:
                return st.id, fm["run"]
        return None

    def run_changed(self, u: curriculum.Entry) -> str | None:
        """The stage whose run command `u`'s written contract does not keep."""
        kept = self.stage_run(u)
        m = self.read_unit_map(u.id)
        if kept is None or m is None or m.get("target") != "program":
            return None
        return kept[0] if m.get("run") != kept[1] else None

    def check_contract(self, u: curriculum.Entry, m: Mapping, languages: Mapping) -> list[str]:
        """Mechanical checks on a contract; problems go back to the model."""
        text = yamlio.dump(m)
        try:
            unit = schema.load(Unit, text)
        except Error as e:
            return [f"the <unit> YAML does not match the schema: {e.msg}"]
        lang_name = self.plan.language(u)
        p: list[str] = []
        if not trim(unit.goal):
            p.append("`goal` is empty")
        if unit.target == Target.FUNCTION and not lang.has_harness(lang_name):
            p.append(f"target: function is not available for {lang_name}; use target: program")
        for f in unit.functions:
            try:
                proto = sig.parse(lang_name, f.sig, f.name)
            except Error as e:
                p.append(f"function `{f.name}`: {e.msg}")
                continue
            if unit.target == Target.FUNCTION:
                # Rust functions the harness cannot call are tested in Rust
                # (`rust_tests`); the gate checks that once the tests exist.
                try:
                    if lang_name != "rust":
                        lang.harness_supports(lang_name, proto)
                except Error as e:
                    p.append(e.msg)
                if lang_name == "python" and "." not in f.name and unit.module is None:
                    p.append(
                        f"python function `{f.name}` needs a dotted name (module.function) or a `module:`"
                    )
        if unit.target == Target.FUNCTION and unit.functions and unit.stubs is None:
            p.append("a function target needs `stubs:` (where the empty functions are written)")
        if unit.types is not None and lang_name != "rust":
            p.append("`types` is only for Rust units: drop it")
        elif unit.types is not None and re.search(r"\bimpl\b|\bfn\b", unit.types):
            p.append(
                "`types` holds only type definitions (structs, enums, derives): methods and functions are listed in `functions`"
            )
        st = unit.stubs
        if st is not None:
            fits = not unit.files or any(
                r.rule != Rule.FORBIDDEN and structure.glob(r.path, st) for r in unit.files
            )
            if not fits:
                p.append(f"stubs path `{st}` is not allowed by the files rules")
        settings = languages.get(lang_name) if lang_name in languages else None
        lang_run = isinstance(settings, dict) and "run" in settings
        if unit.reading:
            if unit.lesson is None or sum(1 for x in lines(unit.lesson) if trim(x)) < 5:
                p.append(
                    "a reading unit needs a <lesson> block: the introduction written from the brief (see the request)"
                )
            if unit.functions or unit.files:
                p.append("a reading unit has no functions or files: only `goal` and the lesson")
        if (
            unit.target == Target.PROGRAM
            and not unit.reading
            and unit.run is None
            and not lang_run
            and lang_name not in ("c", "rust")
        ):
            p.append(f"a program target in {lang_name} needs `run:`")
        if unit.forbidden and self.allowed_so_far(u, m) is None:
            p.append(
                "`forbidden` only narrows what `allowed` permits, and neither this unit nor an earlier stage of its project has `allowed`: add one (for example `allowed: [std]`), or drop `forbidden`"
            )
        req_text = requirements_markdown(unit.requirements)
        if sum(1 for line in lines(req_text) if trim(line)) > 30:
            p.append(
                "the requirements are too long (keep them under about 25 lines): state only what the functions table, Allowed, the examples and RULES.md cannot"
            )
        what = redundant_requirement(req_text)
        if what is not None:
            p.append(
                f"the requirements restate {what}, which the page or RULES.md already shows: remove that part"
            )
        if unit.lesson is not None:
            missing = uncovered_concepts(unit.lesson, u.f.introduces)
            if missing:
                p.append(
                    f"the lesson has no ### subsection naming {', '.join(f'`{c}`' for c in missing)}: give each introduced concept its own subsection"
                )
        lesson_lines = 0 if unit.lesson is None else sum(1 for x in lines(unit.lesson) if trim(x))
        if not u.f.guided and u.f.introduces and lesson_lines < 8:
            p.append(
                "this unit introduces new concepts, so it needs a <lesson> block teaching them (see the request)"
            )
        if u.f.guided and (
            unit.lesson is None or sum(1 for x in lines(unit.lesson) if trim(x)) < 20
        ):
            p.append(
                "a guided unit needs `lesson`: a step-by-step walkthrough in markdown (see the request)"
            )
        all_text = text.lower()
        if "\u2014" in text or "\u2013" in text:
            p.append("never use long dashes; use a colon or a new sentence")
        # The student's commands may be named; the hidden parts may not. Plain
        # words such as "test harness" (Rust's own term) are fine.
        phrase = revealing_phrase(all_text)
        if phrase is not None:
            p.append(
                f"the contract says `{phrase}`: it must not reveal the hidden tests, the reference solution or how the checks work; reword that part"
            )
        r = unit.run
        if r is not None and ("target/" in r or "target\\" in r):
            p.append(
                f"`run: {r}` points into a build folder, which proctor places elsewhere: remove `run` (a Rust or C program runs as built) or run it through its interpreter"
            )
        return p

    def contract(
        self,
        u: curriculum.Entry,
        earlier: list[curriculum.Entry],
        languages: Mapping,
    ) -> Mapping:
        req = prompts.contract_request(
            self.context(earlier),
            self.entry_text(u),
            self.project_text(u),
            self.language_info(self.plan.language(u), languages),
        )
        messages = [llm.Msg.system(self.system), llm.Msg.user(req)]
        last = ""
        for _ in range(self.opts.retries + 1):
            reply = self.chat(f"contract-{u.id}", messages)
            problems: list[str]
            y = llm.yaml_block(reply, "unit")
            if y is None:
                problems = ["missing <unit> block"]
            else:
                try:
                    parsed = llm.parse_yaml(drop_fixed_keys(y))
                except Error as e:
                    problems = [f"<unit> is not valid YAML: {e.msg}"]
                else:
                    if isinstance(parsed, dict):
                        model: Mapping = parsed
                        # Markdown with code blocks does not survive inside YAML: it comes apart.
                        lesson = llm.block(reply, "lesson")
                        if lesson is not None and trim(lesson):
                            model["lesson"] = trim(lesson)
                        r = llm.block(reply, "requirements")
                        if r is not None:
                            if not trim(r):
                                _remove(model, "requirements")
                            else:
                                model["requirements"] = trim(r)
                        m = self.merge_contract(u, model)
                        for fix in repair_contract(m):
                            self.log.log(f"  repaired: {fix}")
                        p = self.check_contract(u, m, languages)
                        kept = self.stage_run(u)
                        if kept is not None and "run" in model and model["run"] != kept[1]:
                            p.append(
                                f"`run` stays `{kept[1]}` as in {kept[0]} (every stage of a project runs the same way): keep it, and the files and text that go with it"
                            )
                        if not p:
                            return m
                        problems = p
                    else:
                        problems = ["<unit> must be a YAML mapping"]
            last = "\n".join(problems)
            self.log.log(f"  rejected: {last}")
            messages.append(llm.Msg.assistant(reply))
            messages.append(llm.Msg.user(prompts.feedback(last, "the <unit> block")))
        raise Error(f"{u.id}: contract rejected after {self.opts.retries + 1} attempt(s): {last}")

    def earlier_tests(self, u: curriculum.Entry) -> list[tuple[str, str]]:
        """The functions, types and tests of each earlier stage of `u`'s project:
        they stay required, and the new tests may build on them."""
        p = u.parent
        if p is None:
            return []
        out: list[tuple[str, str]] = []
        for st in self.plan.stages(p):
            if st.id == u.id:
                break
            m = self.read_unit_map(st.id)
            if m is None:
                continue
            t: Mapping = {}
            for k in ["functions", "types", "cases", "rust_tests", "random"]:
                if k in m:
                    t[k] = m[k]
            if t:
                out.append((st.id, yamlio.dump(t)))
        return out

    def allowed_so_far(self, u: curriculum.Entry, contract: Mapping) -> list[str] | None:
        """Allowed entries of the earlier stages of `u`'s project and of `u`
        itself (as the grader merges them), or `None` when none restricts."""

        def listed(m: Mapping) -> list[str] | None:
            if "allowed" not in m:
                return None
            v = m["allowed"]
            return _strs(v) if isinstance(v, list) else None

        lists: list[list[str] | None] = []
        if u.parent is not None:
            for st in self.plan.stages(u.parent):
                if st.id == u.id:
                    break
                sm = self.read_unit_map(st.id)
                lists.append(listed(sm) if sm is not None else None)
        lists.append(listed(contract))
        if all(x is None for x in lists):
            return None
        out: list[str] = []
        for x in lists:
            for a in x or []:
                if a not in out:
                    out.append(a)
        return out

    def spec_problems(
        self, u: curriculum.Entry, upto: list[curriculum.Entry], languages: Mapping
    ) -> list[str]:
        """What proctor's validation finds wrong with `u`, with the course
        written up to it."""
        self.write_course(upto, languages)
        c = course.CourseDir.load(self.root)
        return [
            p
            for p in engine.validate(c)
            if p.startswith(u.id) and p[len(u.id) : len(u.id) + 1] in (":", " ")
        ]

    def tests(self, u: curriculum.Entry, upto: list[curriculum.Entry], languages: Mapping) -> None:
        h = hidden(self.root)
        read = self.read_unit_map(u.id)
        if read is None:
            raise Error("contract missing; run pass 1 first")
        contract = read
        _remove(contract, "cases")
        _remove(contract, "rust_tests")
        _remove(contract, "random")
        _remove(contract, "think_about")
        contract_text = yamlio.dump(contract)
        # The tests reply cannot change the contract, so a problem in it would
        # fail every attempt: stop before the first call, and drop the cached
        # contract so that `--only` asks for a new one.
        write(self.unit_path(u.id), contract_text)
        own = self.spec_problems(u, upto, languages)
        if own:
            self.forget(f"contract-{u.id}")
            raise Error(
                f"its contract has a problem no test suite can fix: {'; '.join(own)}. `coursecrafter build --tests --only {u.id.split('-')[0]}` writes a new contract"
            )
        lib_path = h / "lib.yaml"
        try:
            lib_text = read_text(lib_path)
        except OSError:
            lib_text = ""
        later: list[str] = []
        if u.parent is not None:
            stages = self.plan.stages(u.parent)
            at = next((i for i, x in enumerate(stages) if x.id == u.id), len(stages))
            later = [
                f"{x.id}: {x.f.goal if x.f.goal is not None else x.title}" for x in stages[at + 1 :]
            ]
        earlier = self.earlier_tests(u)
        allowed = self.allowed_so_far(u, contract)
        fv = contract.get("forbidden")
        forbidden = _strs(fv) if isinstance(fv, list) else []
        language = self.language_info(self.plan.language(u), languages)
        req = prompts.tests_request(
            contract_text,
            prompts.TestsContext(
                lib=lib_text,
                language=language,
                later_stages=later,
                rigor=self.rigor(),
                earlier_tests=earlier,
                allowed=allowed,
                forbidden=forbidden,
            ),
        )
        messages = [llm.Msg.system(self.system), llm.Msg.user(req)]
        draft = Draft()
        last = ""
        # The independent reading's verdict on each test seen so far, by key.
        seen: dict[str, str | None] = {}
        challenged = False
        for attempt in range(self.opts.retries + 1):
            try:
                reply = self.chat(f"tests-{u.id}", messages)
            except Error as e:
                last = e.msg
                break
            # A retry sends only what changes: merge it into what came before.
            draft.merge(reply)
            try:
                self.try_tests(u, upto, contract, draft, lib_text, languages)
            except Error as problem:
                last = problem.msg
                # Test-language rejections feed the parser's corpus of idioms.
                if "cases line" in last or "law check" in last:
                    curriculum.log_validation(
                        self.root,
                        "tests",
                        [],
                        [curriculum.Problem(line=0, id=u.id, msg=last, warning=False)],
                    )
                self.log.log(f"  attempt {attempt + 1} rejected: {last}")
                messages.append(llm.Msg.assistant(reply))
                messages.append(llm.Msg.user(prompts.tests_retry(last)))
                continue
            if not self.opts.cross_check:
                return
            try:
                disputed = self.cross_check(u, seen)
            except Error as e:
                last = e.msg
                break
            if not disputed:
                self.mark_verified(u.id)
                return
            listed = "\n".join(f"- {why}" for _, why in disputed)
            self.log.log(f"  cross-check disputes {len(disputed)} test(s):\n{listed}")
            # One round back to the writer; what is still disputed after it goes.
            if challenged or attempt == self.opts.retries:
                try:
                    self.drop_disputed(u, upto, languages, disputed)
                except Error as e:
                    last = e.msg
                    break
                self.mark_verified(u.id)
                return
            challenged = True
            messages.append(llm.Msg.assistant(reply))
            messages.append(llm.Msg.user(prompts.cross_check_retry(listed)))
        # Never keep a broken suite: restore the bare contract and the old lib.
        text = yamlio.dump(contract)
        write(self.unit_path(u.id), text)
        write(lib_path, lib_text)
        raise Error(llm.cut(last, 400))

    def try_tests(
        self,
        u: curriculum.Entry,
        upto: list[curriculum.Entry],
        contract: Mapping,
        draft: Draft,
        lib_text: str,
        languages: Mapping,
    ) -> None:
        h = hidden(self.root)
        tests_yaml = draft.tests
        if tests_yaml is None:
            raise Error("missing <tests> block")
        try:
            parsed = llm.parse_yaml(tests_yaml)
        except Error as e:
            raise Error(f"<tests> is not valid YAML: {e.msg}") from None
        if not isinstance(parsed, dict):
            raise Error("<tests> must be a YAML mapping with `cases` and/or `random`")
        tests: Mapping = parsed
        test_keys = [
            "cases",
            "rust_tests",
            "random",
            "helpers",
            "extra_flags",
            "wrap",
            "think_about",
        ]
        unit = dict(contract)
        for key in test_keys:
            if key in tests:
                unit[key] = tests[key]
        # What can be repaired here is, without asking again.
        for k in tests:
            if not (isinstance(k, str) and k in test_keys):
                self.log.log(f"  ignored the unknown key `{k}` in <tests>")
        cases_text = _as_str(tests.get("cases")) or ""
        tv = tests.get("think_about")
        think = _strs(tv) if isinstance(tv, list) else []
        try:
            think, dropped = clean_think_about(think, all_cases(tests))
        except Error as e:
            think, dropped = [], [e.msg]
        for d in dropped:
            self.log.log(f"  think_about: dropped {d}")
        if not think:
            _remove(unit, "think_about")
        else:
            unit["think_about"] = seq(think)

        def has_example(line: str) -> bool:
            line = trim(line)
            if line.startswith("#"):
                return False
            try:
                c = program.parse_case(line)
            except Error:
                return False
            return any(
                isinstance(a, program.AssertEq) and a.stream == program.Stream.OUT and a.text != ""
                for a in c.asserts
            )

        # The Examples section of a program is built from a case with an exact output.
        if contract.get("target") == "program" and not any(
            has_example(line) for line in lines(cases_text)
        ):
            self.log.log("  no case with an exact output: the instructions show no example")
        # Support files for helpers live in the hidden folder, per unit.
        support_dir = h / "support" / u.id
        shutil.rmtree(support_dir, ignore_errors=True)
        if not draft.support:
            _remove(unit, "support")
        else:
            support: list[str] = []
            for path, body in sorted(draft.support.items()):
                if ".." in path or path.startswith("/"):
                    self.log.log(f"  ignored the support file `{path}`: not a plain relative name")
                    continue
                write(support_dir / path, body)
                support.append(f"support/{u.id}/{path}")
            unit["support"] = seq(support)
        if draft.files:
            self.log.log(
                f"  ignored {len(draft.files)} <file> block(s): proctor tests without a reference solution"
            )
        given = dict(draft.scaffold)
        if self.plan.language(u) == "rust":
            given, changed = one_workspace_root(given)
            for path in changed:
                self.log.log(
                    f"  {path}: dropped its [workspace] table (the root Cargo.toml has the only one)"
                )
        for path in sorted(given):
            if path.startswith("/") or ".." in path or ":" in path:
                self.log.log(f"  ignored the scaffold file `{path}`: not relative to the workspace")
                del given[path]
        # lib.yaml: keep existing names, add new ones.
        try:
            old_lib = yamlio.load(lib_text)
        except Error:
            old_lib = None
        lib: Mapping = dict(old_lib) if isinstance(old_lib, dict) else {}
        if draft.lib is not None and trim(draft.lib):
            try:
                new = yamlio.load(draft.lib)
            except Error:
                new = None
            if isinstance(new, dict):
                for k, v in new.items():
                    if k not in lib:
                        lib[k] = v
            else:
                # The laws using its generators fail validation and are dropped.
                self.log.log("  ignored <lib>: not a YAML mapping of named generators")
        lib_out = "" if not lib else yamlio.dump(lib)
        write(h / "lib.yaml", lib_out)
        write(self.unit_path(u.id), yamlio.dump(unit))
        scaffold_dir = h / "scaffold" / u.id
        shutil.rmtree(scaffold_dir, ignore_errors=True)
        for path, body in sorted(given.items()):
            write(scaffold_dir / path, body)
        self.ensure_rust_scaffold(u)
        self.salvage(u, upto, languages)

    def salvage(
        self, u: curriculum.Entry, upto: list[curriculum.Entry], languages: Mapping
    ) -> None:
        """Repairs `u`'s suite as written, in code: the tests proctor cannot
        run (spec errors, course errors on empty stubs, Rust tests that do not
        compile) and the cases and laws that contradict each other are dropped,
        then what is left goes through the gate. Only a suite the gate still
        rejects goes back to the model."""
        m = self.read_unit_map(u.id)
        if m is None:
            raise Error("unit missing after write")
        dropped: list[str] = []

        def drop(keys: set[str], why: dict[str, str]) -> None:
            for key, label in suite_tests(m):
                if key in keys and key in why:
                    dropped.append(f"{label}: {why.pop(key)}")
            drop_tests(m, keys)
            write(self.unit_path(u.id), yamlio.dump(m))

        def only(keep: set[str]) -> None:
            one = copy.deepcopy(m)
            drop_tests(one, {k for k, _ in suite_tests(m)} - keep)
            write(self.unit_path(u.id), yamlio.dump(one))

        # Tests proctor cannot even parse, each tried on its own.
        if self.spec_problems(u, upto, languages):
            only(set())
            own = self.spec_problems(u, upto, languages)
            if own:
                write(self.unit_path(u.id), yamlio.dump(m))
                raise Error("\n".join(own))
            why: dict[str, str] = {}
            for key, _ in suite_tests(m):
                only({key})
                p = self.spec_problems(u, upto, languages)
                if p:
                    why[key] = p[0]
            drop(set(why), why)
        # Tests that fail on empty stubs whatever the student writes.
        for _ in range(3):
            c, loaded, r = self.stub_report(u)
            names = self.test_keys(c, loaded, m)
            bad = {k for n in r.wrong for k in names.get(n, [])}
            if bad:
                drop(bad, {k: "wrong whatever the student writes" for k in bad})
                continue
            rust = [k for k, label in suite_tests(m) if label.startswith("Rust test")]
            if r.error is None or not rust or not r.error.startswith("the tests do not build"):
                break
            # Tests in Rust that do not compile against the stubs, each tried on its own.
            others = {k for k, _ in suite_tests(m)} - set(rust)
            broken: dict[str, str] = {}
            for key in rust:
                only(others | {key})
                _, _, alone = self.stub_report(u)
                if alone.error is not None and alone.error.startswith("the tests do not build"):
                    broken[key] = "does not compile against the stubs"
            write(self.unit_path(u.id), yamlio.dump(m))
            if not broken:
                break
            drop(set(broken), broken)
        why = self.contradicted(u, m)
        if why:
            drop(set(why), why)
        if dropped:
            listed = "\n".join(f"    {d}" for d in dropped)
            self.log.log(f"  dropped {len(dropped)} test(s):\n{listed}")
        try:
            self.gate(u, upto, languages)
        except Error as e:
            if not dropped:
                raise
            raise Error(
                f"{e.msg}\n(after dropping {len(dropped)} test(s) proctor could not use: {'; '.join(dropped)})"
            ) from None

    def contradicted(self, u: curriculum.Entry, m: Mapping) -> dict[str, str]:
        """The tests of unit file `m` that cannot be trusted, by key, with why.
        Two cases that expect different results for the same input both go; a
        law that fails whatever the solution goes. When laws and cases disagree,
        a law contradicting two cases or more is wrong (the cases are
        independent), so is a case two laws or more contradict, and a law and a
        case that only disagree with each other both go. Leaves `m` on disk."""
        laws = {yamlio.dump(x) for x in _laws(m)}
        wrong: dict[str, str] = {}
        against: dict[str, list[tuple[str, str]]] = {}

        def found(mm: Mapping) -> list[tuple[list[str], str]]:
            write(self.unit_path(u.id), yamlio.dump(mm))
            c = course.CourseDir.load(self.root)
            now = c.unit(u.id)
            if now is None:
                raise Error("unit missing after write")
            try:
                return crosscheck.conflicts(c, now, _laws(mm))
            except Error:
                return []

        # Cases that clash with each other, with no law involved.
        bare = copy.deepcopy(m)
        drop_tests(bare, laws)
        mine = {k for k, _ in suite_tests(bare)}
        for keys, reason in found(bare):
            for k in keys:
                if k in mine:
                    wrong.setdefault(k, reason)
        # Each law alone against every case it disagrees with: it stops at its
        # first disagreement, so those cases are taken out one by one.
        for law in laws:
            sim = copy.deepcopy(m)
            drop_tests(sim, (laws - {law}) | set(wrong))
            for _ in range(len(suite_tests(m)) + 1):
                mine = {k for k, _ in suite_tests(sim)}
                gone: set[str] = set()
                for keys, reason in found(sim):
                    hit = [k for k in keys if k in mine and k != law]
                    if not hit:
                        wrong.setdefault(law, reason)
                    else:
                        against.setdefault(law, []).extend((k, reason) for k in hit)
                        gone.update(hit)
                if not gone or law in wrong:
                    break
                drop_tests(sim, gone)
        for law, cases in against.items():
            if len(cases) >= 2:
                wrong.setdefault(law, f"contradicts {len(cases)} cases: {cases[0][1]}")
        bad_laws = laws & set(wrong)
        by_case: dict[str, list[str]] = {}
        for law, cases in against.items():
            if law not in bad_laws:
                for k, reason in cases:
                    by_case.setdefault(k, []).append(reason)
        for k, reasons in by_case.items():
            if len(reasons) >= 2:
                wrong.setdefault(k, f"contradicted by {len(reasons)} laws: {reasons[0]}")
        for law, cases in against.items():
            if law in bad_laws:
                continue
            for k, reason in cases:
                if len(by_case[k]) < 2:
                    wrong.setdefault(law, reason)
                    wrong.setdefault(k, reason)
        # Whatever still disagrees once those are gone goes too.
        rest = copy.deepcopy(m)
        drop_tests(rest, set(wrong))
        mine = {k for k, _ in suite_tests(rest)}
        for keys, reason in found(rest):
            for k in keys:
                if k in mine:
                    wrong.setdefault(k, reason)
        write(self.unit_path(u.id), yamlio.dump(m))
        return wrong

    def test_keys(self, c: course.CourseDir, u: Unit, m: Mapping) -> dict[str, list[str]]:
        """proctor's test names of `u` -> their keys in unit file `m`."""
        chain = c.chain(u)
        try:
            protos = engine.protos_for(c, chain) + engine.helpers_for(c, chain)
            tests = engine.tests_of(c, u, protos)
        except Error:
            return {}
        keys = [k for k, _ in suite_tests(m)]
        out: dict[str, list[str]] = {}
        if len(tests) == len(keys):
            for t, k in zip(tests, keys, strict=True):
                out.setdefault(t.name, []).append(k)
        return out

    def stub_report(
        self, u: curriculum.Entry
    ) -> tuple[course.CourseDir, Unit, selftest.UnitReport]:
        """proctor selftest of `u` as written: its tests on empty stubs."""
        c = course.CourseDir.load(self.root)
        loaded = c.unit(u.id)
        if loaded is None:
            raise Error("unit missing after write")
        seed = gens.Rng.new(journal.now()).next_u64()
        if docker.needed(loaded):
            reports = docker.selftest(c, [loaded], seed)
            if not reports:
                raise Error("no report from the Linux container")
            return c, loaded, reports.pop()
        return c, loaded, selftest.unit(c, loaded, seed)

    def gate(self, u: curriculum.Entry, upto: list[curriculum.Entry], languages: Mapping) -> None:
        """The course up to `u`, as written, must load, parse and self-test."""
        problems = self.spec_problems(u, upto, languages)
        if problems:
            raise Error("\n".join(problems))
        c, loaded, r = self.stub_report(u)
        issues: list[str] = []
        if r.error is not None:
            issues.append(f"the tests cannot run on proctor's empty stubs: {r.error}")
        for v in r.vacuous:
            issues.append(f"weak tests: {v}; add a check that fails when the function does nothing")
        if not loaded.random.laws and laws_apply(c, loaded):
            issues.append(
                "no random tests: add `random.laws`, generated inputs checked against properties of the result (fixed cases alone are not enough)"
            )
        if issues:
            raise Error("\n".join(issues))
        caught = (
            f", every function caught on stubs ({r.vacuity_checked})" if r.vacuity_checked else ""
        )
        self.log.log(f"  gate OK: {len(loaded.random.laws)} law(s){caught}")

    def cross_check(
        self, u: curriculum.Entry, seen: dict[str, str | None]
    ) -> list[tuple[list[str], str]]:
        """The tests of `u`'s suite as written that cannot be trusted, as
        (keys, why): those the independent reading disagrees with (it answers
        only the ones not in `seen`, which records its verdicts), and the cases
        and laws that contradict each other."""
        c = course.CourseDir.load(self.root)
        loaded = c.unit(u.id)
        raw = self.read_unit_map(u.id)
        if loaded is None or raw is None:
            raise Error("unit missing after write")
        laws = _laws(raw)
        its = crosscheck.items(c, loaded, laws)
        fresh = [it for it in its if it.key not in seen]
        if fresh:
            page, earlier, rules_md = crosscheck.context(c, loaded)
            req = prompts.verify_request(page, earlier, rules_md, crosscheck.questions(fresh))
            messages = [llm.Msg.system(prompts.VERIFY_SYSTEM), llm.Msg.user(req)]
            reply = self.chat(f"verify-{u.id}", messages, self.checker)
            seen.update(crosscheck.disputes(fresh, crosscheck.answers(reply)))
        out: list[tuple[list[str], str]] = []
        for it in its:
            why = seen.get(it.key)
            if why is not None:
                out.append(([it.key], f"{it.label}: the independent reading {why}"))
        # Of tests that contradict each other, the ones the reading confirmed
        # are kept: only the others are suspect.
        for keys, why in crosscheck.conflicts(c, loaded, laws):
            suspect = [k for k in keys if seen.get(k, "") is not None]
            out.append((suspect or keys, why))
        agreed = sum(1 for it in its if seen.get(it.key, "") is None)
        self.log.log(f"  cross-check: {agreed} of {len(its)} test(s) confirmed")
        return out

    def drop_disputed(
        self,
        u: curriculum.Entry,
        upto: list[curriculum.Entry],
        languages: Mapping,
        disputed: list[tuple[list[str], str]],
    ) -> None:
        """Removes the disputed tests of `u` (keys from `cross_check`), then
        gates what is left."""
        keys = {k for ks, _ in disputed for k in ks}
        m = self.read_unit_map(u.id)
        if m is None:
            raise Error("unit missing after write")
        n = drop_tests(m, keys)
        write(self.unit_path(u.id), yamlio.dump(m))
        self.log.say(f"  dropped {n} disputed test(s)")
        try:
            self.gate(u, upto, languages)
        except Error as e:
            raise Error(
                f"after dropping {n} disputed test(s), the suite fails the gate: {e.msg}"
            ) from None

    def verified_path(self) -> Path:
        return hidden(self.root) / "coursecrafter" / "verified"

    def suite_hash(self, id: str) -> str:
        m = self.read_unit_map(id) or {}
        return fnv([yamlio.dump(m.get(k)) for k in ("cases", "rust_tests", "random")])

    def verified(self, id: str) -> bool:
        """`id`'s suite, as it is now, passed the cross-check."""
        try:
            text = read_text(self.verified_path())
        except OSError:
            return False
        return f"{id} {self.suite_hash(id)}" in lines(text)

    def mark_verified(self, id: str) -> None:
        try:
            text = read_text(self.verified_path())
        except OSError:
            text = ""
        kept = [x for x in lines(text) if x.split(" ")[0] != id]
        kept.append(f"{id} {self.suite_hash(id)}")
        write(self.verified_path(), "".join(f"{x}\n" for x in sorted(kept)))


def _laws(m: Mapping) -> list[Any]:
    """A unit file's laws, as written."""
    random = m.get("random")
    if isinstance(random, dict) and isinstance(random.get("laws"), list):
        return list(random["laws"])
    return []


def repair_contract(m: Mapping) -> list[str]:
    """Fixes in place what a contract gets wrong only in form, so it costs no
    retry: long dashes, a one-item list for `stubs`, keys the unit spec does
    not have. Returns what was done."""
    fixes: list[str] = []

    def undash(v: Any) -> Any:
        if isinstance(v, str):
            return v.replace("\u2014", "-").replace("\u2013", "-")
        if isinstance(v, list):
            return [undash(x) for x in v]
        if isinstance(v, dict):
            return {k: undash(x) for k, x in v.items()}
        return v

    dashed = yamlio.dump(m)
    if "\u2014" in dashed or "\u2013" in dashed:
        for k in list(m):
            m[k] = undash(m[k])
        fixes.append("long dashes replaced by hyphens")
    st = m.get("stubs")
    if isinstance(st, list) and len(st) == 1 and isinstance(st[0], str):
        m["stubs"] = st[0]
        fixes.append("`stubs` given as a one-item list")
    for _ in range(len(m)):
        try:
            schema.load(Unit, yamlio.dump(m))
        except Error as e:
            found = re.search(r"unknown field `([^`]+)`", e.msg)
            if found is None or found.group(1) not in m:
                break
            _remove(m, found.group(1))
            fixes.append(f"dropped the unknown key `{found.group(1)}`")
        else:
            break
    return fixes


def suite_tests(m: Mapping) -> list[tuple[str, str]]:
    """The tests of a unit file as (key, label), in the order proctor lists
    them: case lines, tests in Rust, then laws. The key is what identifies the
    test in the file: the case line, the Rust test's source, the law's YAML."""
    out: list[tuple[str, str]] = []
    cases = _as_str(m.get("cases"))
    for line in lines(cases or ""):
        t = trim(line)
        if t and not t.startswith("#"):
            out.append((t, f"case `{llm.cut(t, 80)}`"))
    rust = _as_str(m.get("rust_tests"))
    if rust is not None and trim(rust):
        out.extend((rt.text, f"Rust test `{rt.name}`") for rt in rtests.parse(rust))
    for i, law in enumerate(_laws(m)):
        name = law.get("name") if isinstance(law, dict) else None
        out.append(
            (yamlio.dump(law), f"law `{name}`" if isinstance(name, str) else f"law #{i + 1}")
        )
    return out


def drop_tests(m: Mapping, keys: set[str]) -> int:
    """Removes the tests of unit file `m` whose key (see `suite_tests`) is in
    `keys`; returns how many went."""
    n = 0
    cases = _as_str(m.get("cases"))
    if cases is not None:
        ls = lines(cases)
        kept: list[str] = []
        for i, line in enumerate(ls):
            t = trim(line)
            # An `# example` mark goes with the case it marks.
            after = trim(ls[i + 1]) if i + 1 < len(ls) else ""
            if t in keys or (t.startswith("#") and after in keys):
                n += 0 if t.startswith("#") else 1
                continue
            kept.append(line)
        if any(trim(x) and not trim(x).startswith("#") for x in kept):
            m["cases"] = "".join(f"{x}\n" for x in kept)
        else:
            _remove(m, "cases")
    random = m.get("random")
    if isinstance(random, dict) and isinstance(random.get("laws"), list):
        laws = [x for x in random["laws"] if yamlio.dump(x) not in keys]
        n += len(random["laws"]) - len(laws)
        if laws:
            random["laws"] = laws
        else:
            _remove(random, "laws")
            if not random:
                _remove(m, "random")
    rust = _as_str(m.get("rust_tests"))
    if rust is not None:
        for rt in rtests.parse(rust):
            if rt.text in keys:
                rust = rust.replace(rt.text, "")
                n += 1
        if trim(rust):
            m["rust_tests"] = rust
        else:
            _remove(m, "rust_tests")
    return n


def hidden(root: Path) -> Path:
    return root / course.HIDDEN


def fnv(parts: list[str]) -> str:
    h = 0xCBF2_9CE4_8422_2325
    for p in parts:
        for b in p.encode("utf-8") + b"\0":
            h ^= b
            h = (h * 0x0100_0000_01B3) & U64_MASK
    return f"{h:016x}"


def cached_reply_of(file: str, label: str) -> bool:
    """True if `file` in the reply cache holds a reply of `label` (`tests-d03`),
    and not of a longer label that starts the same (`tests-d03-x`)."""
    if not file.startswith(label):
        return False
    r = file[len(label) :]
    if not r.startswith("-") or not r.endswith(".txt") or len(r) < 5:
        return False
    key = r[1:-4]
    return len(key) == 16 and all(c in "0123456789abcdefABCDEF" for c in key)


def write(path: Path, body: str) -> None:
    d = path.parent
    try:
        d.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        raise Error(f"{d}: {io_error(e)}") from None
    try:
        write_text(path, body)
    except OSError as e:
        raise Error(f"{path}: {io_error(e)}") from None


def seq(v: list[str]) -> list[Any]:
    return list(v)


def has_preset(lang: str) -> bool:
    return lang in ("c", "python", "rust", "bash", "sh", "shell")


def note_slug(id: str) -> str:
    return trim_start_matches(id, "note-")


READING_REQUEST = (
    "gradable: false. A reading-only unit: no exercise and nothing to hand in. Send the <unit> block with only "
    "`goal` (one sentence), then a <lesson>...</lesson> block after it (plain markdown, NOT inside the YAML; "
    "headings ###, no ## headings, about 20 to 50 lines) that turns the brief into the page the student reads: "
    "what they will build, the roadmap as a short list, what is reused, the scope. Write no functions, files, "
    "requirements or tests. End with: when you have read this page, run `proctor check`.\n"
)


def lesson_request(lang: str, guided: bool, introduces: list[str], assumes: list[str]) -> str:
    """What the contract request asks of the lesson (shown before the exercise)."""
    if not guided and not introduces:
        return ""
    concepts = ", ".join(f"`{c}`" for c in introduces)
    if guided:
        s = (
            f"guided: true. The student has never used {lang}. Send a <lesson>...</lesson> block after the <unit> block "
            "(plain markdown, NOT inside the YAML; headings ###, no ## headings, about 80 to 110 lines): a walkthrough "
            "in numbered steps with the exact commands and their expected output: check the toolchain is installed "
            "(and where to get it), create the workspace and the files the contract expects, write a first program and "
            "explain each line, build and run it, read a compiler error on purpose. Then one ### subsection per "
            f"concept this unit introduces ({concepts}), then run `proctor check`. The first program may be shown in "
            "full; for the rest of the exercise, teach the syntax with other examples and leave the solution to the "
            "student.\n"
        )
    else:
        s = (
            f"This unit introduces {concepts}, which the student has not met. Send a <lesson>...</lesson> block after "
            "the <unit> block (plain markdown, NOT inside the YAML; headings ###, no ## headings, about 30 to 70 lines) "
            "that teaches them before the exercise.\n"
        )
    forms, extends, experiment = lesson_examples(lang)
    s += (
        "Lesson rules:\n"
        "- One ### subsection per introduced concept, its heading naming the concept. Never fold two concepts into one "
        "subsection.\n"
        "- Per concept: one or two sentences on what it is and why it matters; the syntax, as a small table when there "
        f"are several forms ({forms}), else a code block; ONE small runnable example that is NOT part of the solution "
        "and never uses anything the contract forbids, with its output; at most two pitfalls as bullets, the key term "
        "in **bold**.\n"
        '- Concise: no filler, no labels such as "What it is." before paragraphs. Explain every piece of syntax the '
        "exercise relies on (a format spec, a type, a macro, an operator rule such as integer division) unless an "
        "earlier unit introduced it.\n"
    )
    if assumes:
        s += (
            f"- When a concept extends one the student already knows ({', '.join(assumes)}), say in one line how they differ (for "
            f"example: {extends}).\n"
        )
    s += (
        "- End with a one-line **Docs:** pointer per concept (a chapter of the language's book or its standard "
        "library docs), then a `### Try it` subsection of one to three lines: an ungraded experiment that makes the "
        f"concept stick (such as {experiment}).\n"
    )
    return s


def lesson_examples(lang: str) -> tuple[str, str, str]:
    """The lesson rules' examples, in the unit's language: forms worth a
    syntax table, a concept that extends a known one, an ungraded experiment."""
    match lang:
        case "rust":
            return (
                "`{:>5}` / `{:<5}` / `{:^5}`, or `&` / `&mut` / `*`",
                "`&mut x` lends a value for changing, `let mut x` lets the variable itself change",
                "using a value after moving it and reading the compiler's message",
            )
        case "c":
            return (
                "`%d` / `%5d` / `%-5d`, or `&x` / `*p` / `p->field`",
                "`*p` reads the value a pointer points to, `&x` takes the address of a variable",
                "printing `sizeof` of an array, then of the same array inside a function it was passed to",
            )
        case "python":
            return (
                '`s[a:b]` / `s[a:]` / `s[::-1]`, or `f"{x:>5}"` / `f"{x:<5}"`',
                "`list.sort()` changes the list in place, `sorted()` returns a new one",
                "removing items from a list while looping over it and looking at what is left",
            )
        case "bash" | "sh" | "shell":
            return (
                '`$var` / `"$var"` / `${var:-default}`',
                '`"$@"` keeps each argument whole, `$*` joins them into one string',
                "storing a file name with a space in a variable and using it unquoted",
            )
        case _:
            return (
                "the forms of a format specifier, or of a loop",
                "how the new form differs from the one the student already uses",
                "breaking the example on purpose and reading the error",
            )


STOPWORDS = [
    "and",
    "the",
    "with",
    "for",
    "from",
    "into",
    "its",
    "your",
    "basic",
    "basics",
    "using",
    "first",
]


def concept_keys(concept: str) -> list[str]:
    """The words of a concept that a heading about it should contain."""
    words: list[str] = []
    cur: list[str] = []
    for c in concept.lower():
        if c.isalnum() or c in "&_!#{}:?":
            cur.append(c)
        else:
            words.append("".join(cur))
            cur = []
    words.append("".join(cur))
    out: list[str] = []
    for w in words:
        w = w.strip(":!")
        if byte_len(w) >= 2 and w not in STOPWORDS:
            out.append(w)
    return out


def uncovered_concepts(lesson: str, introduces: list[str]) -> list[str]:
    """Introduced concepts that no ### heading of the lesson names."""
    headings = [
        line.lower().replace("`", "")
        for line in lines(lesson)
        if trim_start(line).startswith("###")
    ]
    out: list[str] = []
    for c in introduces:
        keys = concept_keys(c)
        if keys and not any(any(k in h for k in keys) for h in headings):
            out.append(c)
    return out


def revealing_phrase(text: str) -> str | None:
    """A phrase (in lowercase text) that would tell the student about the hidden
    parts of the grading."""
    return next(
        (
            p
            for p in [
                "reference solution",
                "hidden reference",
                "hidden test",
                "proctor's harness",
                "proctor harness",
                "grading harness",
                "harness of proctor",
            ]
            if p in text
        ),
        None,
    )


def redundant_requirement(text: str) -> str | None:
    """Requirements that restate what the page or RULES.md already says."""
    t = text.lower()
    return next(
        (
            what
            for k, what in [
                ("cargo fmt", "the linters"),
                ("clippy", "the linters"),
                ("-wall", "the compiler flags"),
                ("proctor check", "how to check"),
                ("allowed list", "the Allowed list"),
                ("is not allowed", "the Allowed list"),
                ("off limits", "the Allowed list"),
                ("forbidden", "the Allowed list"),
                ("time limit", "the time limit"),
            ]
            if k in t
        ),
        None,
    )


_STRINGS = re.compile(r'"((?:[^"\\]|\\.)*)"')
_NUMBERS = re.compile(r"\b\d{3,}\b")


def case_literals(cases: str) -> list[str]:
    """Literals of the test cases that would give a case away if a Think about
    bullet repeated them: numbers of three digits or more, and strings with
    something besides lowercase letters and spaces. Plain words ("the", "no
    newline") are ordinary vocabulary, not a leak."""
    out = [
        c[1]
        for c in _STRINGS.finditer(cases)
        if len(trim(c[1])) >= 3 and any(not ("a" <= ch <= "z" or ch == " ") for ch in c[1])
    ]
    out.extend(m[0] for m in _NUMBERS.finditer(cases))
    return out


def contains_whole(hay: str, needle: str) -> bool:
    """`needle` occurs in `hay` as a whole: not inside a longer word or number."""

    def word(c: str) -> bool:
        return c != "" and c.isalnum()

    i = hay.find(needle)
    while i != -1:
        end = i + len(needle)
        before, after = hay[i - 1 : i], hay[end : end + 1]
        if not (word(needle[:1]) and word(before)) and not (word(needle[-1:]) and word(after)):
            return True
        i = hay.find(needle, end)
    return False


def think_about_leak(item: str, literals: list[str]) -> str | None:
    """Why a Think about bullet gives a case or its answer away, if it does."""
    if '"' in item or "==" in item or "->" in item or "=>" in item:
        return "gives a concrete case or result"
    lit = next((lit for lit in literals if contains_whole(item, lit)), None)
    return None if lit is None else f"quotes `{lit}` from the tests"


def clean_think_about(items: list[str], cases: str) -> tuple[list[str], list[str]]:
    """A Think about list cleaned in code: bullets that give a case away are
    dropped and the list is cut to four, so a stray bullet costs no retry.
    Returns the kept bullets and what was dropped; raises when fewer than two
    of a non-empty list remain (the model writes it again)."""
    literals = case_literals(cases)
    kept: list[str] = []
    dropped: list[str] = []
    for it in items:
        why = think_about_leak(it, literals)
        if why is not None:
            dropped.append(f"`{it}` {why}")
        elif len(kept) < 4:
            kept.append(it)
        else:
            dropped.append(f"`{it}` is beyond the fourth bullet")
    if items and len(kept) < 2:
        raise Error(
            f'`think_about` needs 2 to 4 bullets that name categories only ("text with no letters"), never a case or its answer; dropped: {"; ".join(dropped)}'
        )
    return kept, dropped


def all_cases(m: Mapping) -> str:
    """A unit's cases, then its tests in Rust: what its teaching text must
    agree with and must not give away."""
    parts = [_as_str(m.get(k)) or "" for k in ("cases", "rust_tests")]
    return "\n".join(p for p in parts if p)


def names(only: str, e: curriculum.Entry) -> bool:
    """`--only` names a unit by its id or its short id (`d03`, `p02.1`), or a
    whole project by its id or short id (`p02-minishell`, `p02`)."""

    def short(id: str) -> str:
        return id.split("-")[0]

    return (
        only == e.id
        or only == short(e.id)
        or (e.parent is not None and (only == e.parent or only == short(e.parent)))
    )


def default_cargo_toml(name: str) -> str:
    """The Cargo.toml of a Rust scaffold that came without one."""
    return f'[package]\nname = "{name}"\nversion = "0.1.0"\nedition = "2024"\n\n[dependencies]\n\n[workspace]\n'


def laws_apply(c: course.CourseDir, u: Unit) -> bool:
    """Random laws can test `u`: a program that reads input (a case gives it
    arguments, stdin or files), or a function the harness calls (Rust methods
    and types are tested in `rust_tests` instead)."""
    if u.target == Target.PROGRAM:
        for _, line in u.case_lines():
            try:
                case = program.parse_case(line)
            except Error:
                return True
            if case.args or case.stdin is not None or case.files:
                return True
        return False
    language = c.language(u)
    if not lang.has_harness(language):
        return False
    for p in engine.protos_for(c, [u]):
        try:
            lang.harness_supports(language, p)
        except Error:
            continue
        return True
    return False


def one_workspace_root(files: dict[str, str]) -> tuple[dict[str, str], list[str]]:
    """A Rust scaffold with one workspace root: Cargo rejects a member crate
    with its own `[workspace]` table ("multiple workspace roots"), which a
    model adds by habit to every crate when the root already has one. Returns
    the files and the paths it changed."""

    def is_ws(t: str) -> bool:
        return t == "[workspace]" or t.startswith("[workspace.")

    root = files.get("Cargo.toml")
    root_ws = root is not None and any(is_ws(trim(line)) for line in lines(root))
    changed: list[str] = []
    out: dict[str, str] = {}
    for path, body in sorted(files.items()):
        if (
            not root_ws
            or not path.endswith("/Cargo.toml")
            or not any(is_ws(trim(line)) for line in lines(body))
        ):
            out[path] = body
            continue
        kept = ""
        skip = False
        for line in lines(body):
            t = trim(line)
            if t.startswith("["):
                skip = is_ws(t)
            if not skip:
                kept += line + "\n"
        changed.append(path)
        out[path] = f"{trim_end(kept)}\n"
    return out, changed


def drop_fixed_keys(yaml: str) -> str:
    """One-line keys coursecrafter sets itself: a model that repeats them can
    break the YAML (`concepts: [formatting with {}]`) for values that are
    replaced anyway, so they are removed before parsing."""
    keys = [
        "id",
        "title",
        "project",
        "language",
        "difficulty",
        "concept_difficulty",
        "hours",
        "concepts",
        "introduces",
        "assumes",
        "prereqs",
        "notes",
        "platform",
        "stretch",
        "guided",
        "done_when",
    ]
    out: list[str] = []
    for line in lines(yaml):
        k, sep, v = line.partition(":")
        if not sep or not (k in keys and trim(v)):
            out.append(line)
    return "\n".join(out)


@dataclass(kw_only=True)
class Draft:
    """One unit's suite as the model has sent it so far. The first reply holds
    everything; a retry sends only what changes, merged in here, so fixing one
    error cannot break what already worked."""

    tests: str | None = None
    lib: str | None = None
    # The three maps are BTreeMaps in Rust: iterate them sorted.
    files: dict[str, str] = field(default_factory=dict)
    support: dict[str, str] = field(default_factory=dict)
    scaffold: dict[str, str] = field(default_factory=dict)

    def merge(self, reply: str) -> None:
        t = llm.yaml_block(reply, "tests")
        if t is not None:
            self.tests = t
        lib = llm.block(reply, "lib")
        if lib is not None:
            self.lib = lib
        for tag, map in [
            ("file", self.files),
            ("support", self.support),
            ("scaffold", self.scaffold),
        ]:
            for path, body in llm.files(reply, tag):
                # An empty block deletes the file.
                if not trim(body):
                    map.pop(path, None)
                else:
                    map[path] = body


# ---------------------------------------------------------------- yaml helpers

_NOT_FOUND = object()


def _remove(m: Mapping, k: str) -> None:
    """`Mapping::remove`, which is `swap_remove` in yaml_serde: the last entry
    takes the removed one's place. Files written after a removal keep the key
    order the Rust tools gave them."""
    if k not in m:
        return
    keys = list(m)
    i = keys.index(k)
    if i == len(keys) - 1:
        del m[k]
        return
    items = list(m.items())
    items[i] = items[-1]
    items.pop()
    m.clear()
    m.update(items)


def _yaml_eq(a: Any, b: Any) -> bool:
    """`Yaml` equality: mappings compare without regard to order, and values of
    different kinds (`1`, `1.0`, `true`) are never equal."""
    if isinstance(a, dict):
        return (
            isinstance(b, dict)
            and len(a) == len(b)
            and all(k in b and _yaml_eq(v, b[k]) for k, v in a.items())
        )
    if isinstance(a, list):
        return (
            isinstance(b, list)
            and len(a) == len(b)
            and all(_yaml_eq(x, y) for x, y in zip(a, b, strict=True))
        )
    if type(a) is not type(b):
        return False
    if isinstance(a, float) and math.isnan(a) and math.isnan(b):
        return True
    return bool(a == b)


def _as_str(v: Any) -> str | None:
    """`Yaml::as_str`."""
    return v if isinstance(v, str) else None


def _strs(v: list[Any]) -> list[str]:
    """The string items of a YAML sequence (`filter_map(as_str)`)."""
    return [x for x in v if isinstance(x, str)]


def _regex_ok(p: str) -> bool:
    try:
        regex(p)
    except re.error:
        return False
    return True
