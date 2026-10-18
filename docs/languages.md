# Languages

| | function target (harness) | program target | Allowed scanner | build / static |
|---|---|---|---|---|
| C | yes (scalar, `char *`, `char **`) | yes | headers + calls | `cc -std=c11 -Wall -Wextra -Werror` |
| Python | yes (any value; dataclasses and namedtuples become maps) | yes | imports | `compileall` |
| Rust | yes (scalars, `&str`/`String`, `Vec`/slices, `Option`, `Result`) | yes | `use`, paths, Cargo deps | `cargo build`, `fmt --check`, `clippy -D warnings` |
| Bash | no | yes | external commands | `bash -n` |
| JS/TS, Go, Java, Ruby | no | yes | imports (regex) | `build:` command |
| anything else | no | yes | `imports:` regexes in the spec | `build:` command |

On Windows, `bash` resolves to msys2 or Git Bash, never WSL's `System32\bash.exe`.

## Language settings are data

The presets for C, C++, Python, Rust, Bash, JavaScript, TypeScript, Go, Java and Ruby are a
YAML block embedded in skillchecker (`skillchecker/src/defaults.rs`). Only the function harnesses
and the dedicated Allowed scanners are code. A course overrides any preset field in its
`languages:` block, and a list set there replaces the preset's list (`static: []` removes
Rust's fmt and clippy).

## Adding a language

Declare it once in `course.yaml`; every unit in that language inherits it, and any field set
on a unit wins. `taskmaster build` writes this block itself for a language without a preset.

```yaml
languages:
  perl:
    name: Perl 5                       # shown in the instructions
    build: perl -c main.pl             # optional
    run: perl main.pl                  # program command
    static: [{ name: lint, cmd: "perlcritic main.pl" }]
    imports: ['(?m)^\s*(?:use|require)\s+([A-Za-z][\w:]*)']   # Allowed check, group 1 = module
    extensions: [pl, pm]               # which files are source
    comment: "#"                       # line comments ignored by the scanners
    env: { PERL_UNICODE: SD }
    timeout_ms: 5000
    compiler: gcc                      # C-family builds
    flags: [-std=c11, -Wall, -Werror]  # program build and function harness
    wrap: valgrind --leak-check=full --error-exitcode=99   # prefix for every run of student code
    support: [support/common/shim.c]   # course files built in with the student's code
```

## Leak checks, sanitizers and allocation failures

These are course content, not skillchecker features: `wrap` (valgrind) or `flags`
(`-fsanitize=address`) make a run fail when the tool reports a problem, and the harness must
exit cleanly, so leaks reported at exit fail the check. Units can add `extra_flags`, `wrap`,
`support` files and `helpers` (test-only functions from support files):

```yaml
extra_flags: [-Dmalloc=sc_malloc]
support: [support/d01-string-basics/fail_alloc.c]   # defines sc_malloc and fail_alloc_after
helpers: [{ name: fail_alloc_after, sig: "void fail_alloc_after(int n)" }]
cases: |
  then(fail_alloc_after(0), str_dup("abc")) == null
```
