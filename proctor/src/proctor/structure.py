"""The "soft" project tree: `files:` rules say what must exist and what may
exist. When a unit lists any rule, every other file is unexpected unless a
`free` rule covers it (`{ path: "**", rule: free }` allows anything)."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from proctor import scan
from proctor.unit import FileRule, Rule


def glob(pattern: str, path: str) -> bool:
    """Glob match on `/`-separated paths: `*` and `?` stay inside one segment,
    `**` spans any number of segments (including none)."""
    p = [s for s in pattern.split("/") if s != ""]
    s = [x for x in path.split("/") if x != ""]

    def segs(p: list[str], s: list[str]) -> bool:
        if not p:
            return not s
        if p[0] == "**":
            return any(segs(p[1:], s[k:]) for k in range(len(s) + 1))
        return bool(s) and one(p[0].encode("utf-8"), s[0].encode("utf-8")) and segs(p[1:], s[1:])

    # Byte-wise, as the Rust tools match: `?` is one byte.
    def one(p: bytes, s: bytes) -> bool:
        if not p:
            return not s
        if p[0] == ord("*"):
            return any(one(p[1:], s[k:]) for k in range(len(s) + 1))
        if p[0] == ord("?"):
            return bool(s) and one(p[1:], s[1:])
        return s[:1] == p[:1] and one(p[1:], s[1:])

    return segs(p, s)


# Build output and editor litter never count as student files.
LITTER = [
    "*.o",
    "*.obj",
    "*.exe",
    "*.pdb",
    "*.ilk",
    "a.out",
    "*.pyc",
    ".DS_Store",
    "*~",
    "*.swp",
    "Cargo.lock",
    "*.class",
    "Thumbs.db",
]


def litter(rel: str) -> bool:
    name = rel.rsplit("/", 1)[-1]
    return any(glob(lit, name) for lit in LITTER)


@dataclass(kw_only=True)
class Finding:
    ok: bool
    name: str
    detail: str


def check(ws: Path, rules: list[FileRule]) -> list[Finding]:
    files = [f for f in scan.walk(ws) if not litter(f)]
    out: list[Finding] = []
    for r in rules:
        hits = [f for f in files if glob(r.path, f)]
        match r.rule:
            case Rule.REQUIRED | Rule.ONE_OR_MORE:
                match len(hits):
                    case 0:
                        detail = "missing"
                    case 1:
                        detail = "present"
                    case n:
                        detail = f"{n} files"
                out.append(Finding(ok=bool(hits), name=r.path, detail=detail))
            case Rule.FORBIDDEN:
                out.append(
                    Finding(
                        ok=not hits,
                        name=f"no {r.path}",
                        detail=f"{hits[0]} must not exist" if hits else "none",
                    )
                )
            case Rule.FREE:
                pass
    if rules:
        stray = [
            f for f in files if not any(r.rule != Rule.FORBIDDEN and glob(r.path, f) for r in rules)
        ]
        if not stray:
            detail = f"{len(files)} file(s) checked"
        elif len(stray) == 1:
            detail = f"{stray[0]} is not part of the expected layout"
        else:
            detail = f"{stray[0]} (and {len(stray) - 1} more) not part of the expected layout"
        out.append(Finding(ok=not stray, name="no unexpected file", detail=detail))
    return out
