# The Python port of PiscineBuilder

The tools were first written in Rust (archived on branch `rust`, tag `rust-final`) and
translated module by module to Python, which replaced them on `main`. The rules below made the translated modules fit together
and still describe how the code is organised: follow them when changing it.

- Each Rust `proctor/src/X.rs` and `coursecrafter/src/X.rs` became
  `proctor/src/proctor/X.py` and `coursecrafter/src/coursecrafter/X.py`. Each Rust `main.rs`
  became `cli.py` (with `def main() -> int`). `lib.rs` is `__init__.py`.
- Python 3.12, uv workspace. Dependencies: the standard library and PyYAML only.
- At the time of the port, behaviour and every user-visible text (messages, reports, rendered
  markdown, prompts, files written) were byte-for-byte identical to the Rust tools, checked by
  running both on the same courses. Courses, journals, LLM reply caches and logs written by
  the Rust tools keep working.
- Doc comments became docstrings; inline comments explain why.

## Shared foundation (read these first)

- `proctor/compat.py`: Rust behaviour where Python differs. Always use it for: `trim`,
  `trim_start`, `trim_end`, `split_whitespace`, `split_ascii_whitespace`, `lines` (Rust
  `str::lines`; never `str.splitlines()`, which also splits on other characters), `byte_len`,
  `from_utf8_lossy`, `debug_str`/`debug_char` (`{:?}` of strings and chars), `float_display`
  (`{}` of an f64: `2.0` prints `2`), `float_debug` (`{:?}` of an f64), `float_exp` (`{:e}`),
  `is_terminal(stream)` (Rust `IsTerminal`; never `isatty()`, which is wrong on Windows for
  `NUL` and in Git Bash), `as_millis`, `trim_start_matches`/`trim_end_matches`,
  `parse_int`/`parse_uint`/`parse_float` (Rust `.parse()`; never bare `int()`/`float()` on
  text, which accept spaces, underscores and non-ASCII digits), `div_trunc`/`rem_trunc`
  (Rust integer `/` and `%`), `read_text`/`write_text` (Rust `fs::read_to_string`/`fs::write`:
  strict UTF-8, no newline translation), `io_error` (an `OSError` displayed as Rust displays
  io errors), `regex` (compile a pattern written in Rust regex syntax: always use it for
  patterns that come from course data, unit specs or a model, and for constant patterns that
  use `$`), `is_whitespace`, `setup_stdio`.
- `proctor/errors.py`: `Error(msg)`, the exception that replaces `Err(String)`.
- `proctor/yamlio.py`: `load(text)`, `dump(value)`, `compose`, `construct`: YAML with the
  YAML 1.2 core schema, as the Rust `yaml_serde` crate reads and writes it. Use instead of
  `yaml.safe_load`/`yaml.dump`.
- `proctor/schema.py`: `load(Type, text)` (`yaml_serde::from_str::<T>`): typed loading into
  dataclasses with serde's rules and error messages. Integer aliases `U8`, `U16`, `U32`,
  `U64`, `I64`.
- `proctor/value.py`: `Value` and its variants (`ValueNull`, `ValueBool(b)`, `ValueInt(i)`,
  `ValueFloat(f)`, `ValueStr(s)`, `ValueChar(c)`, `ValueList(items)`, `ValueMap(pairs)`,
  `ValueOpaque(s)`, and the `NULL` constant), the wire format (`hex(data) -> str`, `unhex`,
  `unhex_text`, `encode`, `decode(iterator)`, `decode_str`).
- `proctor/command.py`: `Command`, the `std::process::Command` builder (`Command("cargo")
  .args([...]).arg(x).current_dir(p).env(k, v).envs(m).env_remove(k).stdin(Stdio.null())
  .stdout(Stdio.piped()).process_group(0)`, then `.status()` -> `ExitStatus`, `.output()` ->
  `Output(status, stdout: bytes, stderr: bytes)`, `.spawn()`/`.popen(...)` ->
  `subprocess.Popen`), `Stdio` (`Stdio.null()`, `.piped()`, `.inherit()`, `Stdio.STDERR` for
  `Stdio::from(std::io::stderr())`), `ExitStatus` (`success()`, `code()` as Rust's i32,
  `signal()`), `ProgramNotFound`. Every module that builds or runs a command uses it, so a
  `&mut Command` can be passed between modules (`run.run(cmd, stdin, timeout)`). Use
  `Popen` directly only where Rust uses `Child` (`try_wait` -> `poll()`, `id()` -> `pid`).
- `proctor/unit.py`: the course schema (`Course`, `LangConfig`, `Project`, `Target`,
  `Scaffold`, `Func`, `Rule`, `FileRule`, `StaticCheck`, `Law`, `Random`, `Unit`).
  `Requirements` is `str | list[str]` with helpers `requirements_is_empty(r)` and
  `requirements_markdown(r)`; `LangConfig::with` is `with_`. Since the port, reference
  solutions and `random.fuzz` are gone (the key still loads, untyped, for `validate` to report).

Put a helper that several modules need in the foundation rather than in private copies.

## Names

- Modules, functions, methods, fields, constants, parameters: the Rust names unchanged. A
  name that is a Python keyword gets a trailing underscore (`with` -> `with_`, `in` -> `in_`).
  `type` and `match` may stay as they are where Python allows them.
- A Rust struct is a class with the same name and the same fields: `@dataclass(kw_only=True)`
  unless it needs custom behaviour. Struct literals become keyword constructions:
  `Options { seed, against, tests_only: false }` -> `Options(seed=seed, against=against,
  tests_only=False)`. Private fields keep their names (no underscore prefix).
- Associated functions (no `self`) become `@staticmethod`s with the same name, called as
  `Engine.new(c, unit, opts)`. Methods keep their names and parameters.
- An enum whose variants carry no data becomes an `enum.Enum` with UPPER_SNAKE members whose
  values are the serde names when the enum is deserialized (`Target.FUNCTION = "function"`,
  `Rule.ONE_OR_MORE = "one_or_more"`, `Status.PASS`).
- An enum with data: a base class named like the enum, and one frozen dataclass per variant
  named enum + variant, subclassing the base (`Gen` -> `GenInt(lo, hi, width)`, `GenBool()`).
  Struct-like variants keep their field names. Tuple-like variants use the names in the table
  below, or `f0`, `f1`... for enums not listed. Methods of the enum go on the base class.
- Traits become `abc.ABC` classes (`Reporter`, `Host`) with the same method names.
- `impl Display` is `__str__`; `to_string()` is `str(x)`.

## Imports

The Rust modules use each other in cycles (`engine` and `report`, `render` and `rules`,
`course` and `journal`, coursecrafter's `main` and `guided`), which Python only allows if no
module needs another half-loaded one while it is being imported. So:

- Every module starts with `from __future__ import annotations`.
- Import sibling modules as modules: `from proctor import engine, lang`, then `engine.Item`,
  `lang.BUILD_TIMEOUT`, exactly as the Rust paths `engine::Item`, `lang::BUILD_TIMEOUT`.
  Never `from proctor.engine import Item`. The foundation (`compat`, `errors`, `yamlio`,
  `schema`, `value`, `unit`) has no cycles: `from`-imports of it are fine.
- Use sibling modules only inside function bodies and annotations, not in code that runs at
  import time, with one exception: a class may subclass a sibling's class (`class
  Term(engine.Reporter)`) when that sibling does not use your module at import time.
  `engine.py` defines `Item`, `Status`, `Phase`, `Verdict` and `Reporter` before importing
  `harness`, `lang` and `program` (which load `report`, whose `Term` subclasses
  `engine.Reporter`), so those imports sit below the classes with `# noqa: E402`.
- coursecrafter functions that the Rust code defines in `main.rs` and calls from other
  modules as `crate::ask_yes`, `crate::approve`, `crate::check_plan`, `crate::review`,
  `crate::unwrap_curriculum` live in `coursecrafter/cli.py`; other modules import `cli`
  inside the function that calls them (`from coursecrafter import cli` in the body).

Fields of tuple-like variants used across modules:

| Rust | Python |
|---|---|
| `Expr::Lit(v) Name(n) Call(name, args) List(items) Map(pairs) Not(e) Neg(e) Bin(op, l, r) Field(e, name) Index(e, i)` | `ExprLit(value)`, `ExprName(name)`, `ExprCall(name, args)`, `ExprList(items)`, `ExprMap(pairs)`, `ExprNot(inner)`, `ExprNeg(inner)`, `ExprBin(op, left, right)`, `ExprField(target, name)`, `ExprIndex(target, index)` |
| `EvalError::Student(m) Spec(m)` | exceptions `EvalErrorStudent(msg)`, `EvalErrorSpec(msg)` (base `EvalError`) |
| `Outcome::Pass Fail(m) SpecError(m)` | `OutcomePass()`, `OutcomeFail(msg)`, `OutcomeSpecError(msg)` |
| `Ty::Int{unsigned} Float Bool Char Str StrList List(t) Opt(t) Void Other(s)` | `TyInt(unsigned)`, `TyFloat()`, ..., `TyList(inner)`, `TyOpt(inner)`, `TyVoid()`, `TyOther(name)` |
| `Gen::Choice(v) Const(v) Opt(g) Fmt(parts)` | `GenChoice(values)`, `GenConst(value)`, `GenOpt(inner)`, `GenFmt(parts)` |
| `FmtPart::Lit(s) Var(g)` | `FmtPartLit(text)`, `FmtPartVar(gen)` |
| `Assert::Exit(n) Eq(st, s) Ne(st, s) Re(st, s) NotRe(st, s) Empty(st)`; `Stream::Out Err` | `AssertExit(code)`, `AssertEq(stream, text)`, `AssertNe(stream, text)`, `AssertRe(stream, pattern)`, `AssertNotRe(stream, pattern)`, `AssertEmpty(stream)`; `Stream.OUT`, `Stream.ERR` |
| `CallError::Raised(m) Crashed(m) Timeout(ms) Protocol(m)` | exceptions `CallErrorRaised(msg)`, `CallErrorCrashed(msg)`, `CallErrorTimeout(ms)`, `CallErrorProtocol(msg)` (base `CallError`, with `message()`) |
| `ProtoFinding::Ok Missing Mismatch(m)` | `ProtoFindingOk()`, `ProtoFindingMissing()`, `ProtoFindingMismatch(msg)` |
| `Answer::Pick(i) Other(s)` | `AnswerPick(index)`, `AnswerOther(value)` (`Answer.text(options)` is a method) |
| `engine::Kind::Prog(case)` | `KindProg(case)` (the other `Kind` variants are struct-like) |
| `engine::Against::Student Dir(p)` | `AgainstStudent()`, `AgainstDir(path)` (`Reference` was dropped after the port) |
| `engine::Item::pass(..)` | `Item.pass_(..)` (keyword) |

## Types

| Rust | Python |
|---|---|
| `String`, `&str`, `char` | `str` (a `char` is a one-character `str`) |
| `Vec<T>`, `&[T]` | `list[T]` |
| `Option<T>` | `T \| None` |
| tuples | tuples |
| integers, `f64` | `int`, `float` |
| `BTreeMap<K, V>`, `BTreeSet<T>` | `dict`, `set`, **iterated in sorted key order** (`sorted(d.items())`, `sorted(s)`) wherever the Rust code iterates or prints them |
| `HashMap` | `dict` (order did not matter in Rust) |
| `yaml_serde::Value`, `Mapping` | plain values from `yamlio` (`dict`, `list`, `str`, ...); `Yaml::String(k)` keys are `str` |
| `serde_json::Value` | plain `json` values; `v["a"]["b"]` on a missing key is `Null` in Rust: guard with `.get` and `isinstance` |
| `Duration` | `float` seconds (`timeout.as_millis()` -> `compat.as_millis(timeout)`) |
| `Path`, `PathBuf` | `pathlib.Path`; `p.display()` -> `str(p)` |
| `Box<T>`, `Rc`, `&`, `&mut` | the value itself |
| `OnceLock`, `static` | module-level variable or `functools.cache` |

## Errors

- `Result<T, String>`: return `T`, raise `Error(msg)` for `Err(msg)`. `?` is a plain call (the
  exception propagates). `map_err(|e| format!("ctx: {e}"))?` -> `try: ... except Error as e:
  raise Error(f"ctx: {e.msg}") from None`.
- A Rust error type that is not `String` (`BuildError`, `CallError`, `EvalError`, `ChatError`)
  becomes an exception class that subclasses `Error`, so a `?` that converts it to `String`
  still propagates as `Error`. Its `msg` is what the Rust `Display`/`String` conversion gives.
- Catch the narrowest class the Rust `match` distinguishes, and keep `try` blocks around the
  fallible call only (use `try/except/else`), so errors raised by the code of the `Ok` branch
  are not caught by mistake.
- `.ok()`, `.unwrap_or(...)`, `.is_ok()`, `if let Ok(x) = ...` on a `Result` -> `try/except`.
- `std::io::Error` comes as `OSError`: catch `OSError` around file and process operations and
  format it with `compat.io_error(e)` where Rust formats `{e}`.
- A `Result` stored in a variable, a field or a list instead of being propagated:
  `errors.Ok(value)` /
  `errors.Err(msg)`, typed `errors.Result[T]` (`Ok[T] | Err`); match with `case Ok(v):` and
  `case Err(msg):`. Only `String` errors are stored this way in the Rust code.
- `panic!`, `unwrap()` and `expect()` on things that cannot fail: let Python raise.
- `#[derive(Debug)]` prints are developer output only; no need to match them.

## Strings, text and formatting

- Python indexes strings by character, Rust by byte. Translate the logic to character indices
  consistently within a function. Where a byte count is meaningful (a size shown to the user,
  a limit in bytes, `Content-Length`, `s.len()` compared to a byte limit), use
  `compat.byte_len`. `chars().count()` is `len(s)`.
- `format!`: Rust `{x}` -> f-string `{x}`, `{{` stays `{{`, `{:<28}` / `{:>5}` / `{:016x}` /
  `{:02}` are the same in Python. `{:.1}` on a float -> `{x:.1f}`. A `bool` prints
  `true`/`false` (never Python's `True`). An f64 with `{}` prints with `float_display` (`2.0`
  -> `2`, `1.5` -> `1.5`). `{:?}` -> `debug_str`, `debug_char`, `float_debug`, or the exact
  Rust Debug text (`Some(3)`, `None`, `["a", "b"]`) for options and lists.
- A Rust string literal that ends a line with `\` continues on the next line without the
  newline and without the next line's leading whitespace. Rebuild the exact text (adjacent
  literals or one long literal). Raw strings `r#"..."#` keep backslashes: use Python raw
  strings or doubled backslashes so the text is byte-identical. Embedded source code (the C,
  Python and Rust harnesses, Dockerfiles, prompts) must be byte-identical: compare against the
  Rust text with a throwaway script before you finish.
- `trim_matches`, `trim_start_matches(pat)` remove every repetition of the pattern (not once):
  `compat.trim_start_matches`, `compat.trim_end_matches`.
  `strip_prefix` returns an `Option`. `split_once`, `rsplit_once` return an `Option`.
  `char::is_ascii_digit` is `"0" <= c <= "9"` (never `str.isdigit()`, which accepts other
  digits). `is_ascii_alphabetic`/`is_ascii_alphanumeric`: test `c.isascii()` too.
  `to_ascii_lowercase` changes ASCII letters only. `char::is_whitespace` is
  `compat.is_whitespace`. `char::is_alphanumeric` is `c.isalnum()`.
- Unicode sorting and comparison of `String`s by byte order equals Python's code point order.

## Numbers

- `i128`/`u64` arithmetic that Rust checks (`checked_add`...) or wraps (`wrapping_mul`, hash
  functions, RNGs) must be reproduced exactly: mask with `& ((1 << 64) - 1)` for `u64`, check
  i128 bounds (`compat.I128_MIN`/`I128_MAX`) where Rust uses checked ops. Seeds, the generator
  sequence and cache keys (`fnv` hashes) must be identical to the Rust tools.
- `as` casts: `f as u64` saturates and truncates toward zero; `i as u8` keeps the low bits;
  `x as f64` is `float(x)`.
- Integer `/` and `%` on possibly negative values: `div_trunc`, `rem_trunc`.

## Processes, files, environment

- `std::process::Command` -> `proctor.command.Command` (see the foundation). Never use
  `shell=True`.
- `fs::write(path, text)` -> `compat.write_text`; `fs::read_to_string` -> `compat.read_text`.
- `fs::create_dir_all` -> `mkdir(parents=True, exist_ok=True)`; `fs::remove_dir_all` ->
  `shutil.rmtree`; `fs::rename` -> `os.replace` or `Path.rename` as appropriate.
- `cfg!(windows)` -> `sys.platform == "win32"`; `cfg!(target_os = "linux")` ->
  `sys.platform.startswith("linux")`; `#[cfg(unix)]` -> `os.name == "posix"`.
- `std::env::var(k)` -> `os.environ.get(k)`. `Instant` -> `time.monotonic()`.
- `thread::spawn` -> `threading.Thread(daemon=True)`; `mpsc` -> `queue.Queue` (bounded
  `sync_channel(n)` -> `Queue(maxsize=n)`).
- `impl Drop` -> a `close()` method called where Rust drops the value (end of scope) plus
  `__del__` as a safety net.
- `println!`/`eprintln!` -> `print(...)` / `print(..., file=sys.stderr)`; `print!` without a
  newline -> `print(..., end="")` and `flush=True` where Rust flushes. Each `cli.main` calls
  `compat.setup_stdio()` first.
- `serde_json::to_string` -> `json.dumps(v, separators=(",", ":"), ensure_ascii=False)`;
  `to_string_pretty` -> `json.dumps(v, indent=2, ensure_ascii=False)`. Keep serde's field
  order and `skip_serializing_if` behaviour.
- HTTP (`ureq`) -> `urllib.request`.

## Signatures used across modules

- `expr.read_string(cs: str, i: int) -> tuple[str, int]` (the Rust `&mut usize` cursor is
  returned as the new index).
- `expr.calls(e, out: list[str]) -> None` (fills `out` in place, as in Rust).
- `value.hex(data: bytes) -> str`.
- Any other function with a `&mut` scalar or `&mut String` parameter: return the new value.
  A `&mut Vec`, `&mut` struct or `&mut dyn Trait` parameter is mutated in place.

## Tests

- The tests of `X.py` are in `proctor/tests/test_X.py` or `coursecrafter/tests/test_X.py`
  (pytest, plain `assert`s); the Rust `#[cfg(test)]` cases were all ported.
- Type-annotate every function (mypy runs with `disallow_untyped_defs`).
- Set `$env:PYTHONIOENCODING='utf-8'` before printing non-ASCII text from Python in the
  agent shell.
