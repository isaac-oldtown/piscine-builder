"""Optional user config: `%APPDATA%\\piscinebuilder\\config.yaml` on Windows,
`$XDG_CONFIG_HOME/piscinebuilder/config.yaml` or `~/.config/piscinebuilder/config.yaml`
elsewhere. Holds choices, never secrets: keys stay in the environment."""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from pathlib import Path

from proctor.compat import read_text
from proctor.errors import Error

from proctor import schema


@dataclass(kw_only=True)
class Config:
    provider: str | None = None
    base_url: str | None = None
    model: str | None = None
    # Name of the environment variable holding the key.
    api_key_env: str | None = None


def path() -> Path | None:
    p = os.environ.get("PISCINEBUILDER_CONFIG")
    if p is not None:
        return Path(p)
    base: Path | None
    if sys.platform == "win32":
        appdata = os.environ.get("APPDATA")
        base = Path(appdata) if appdata is not None else None
    else:
        xdg = os.environ.get("XDG_CONFIG_HOME")
        home = os.environ.get("HOME")
        if xdg is not None:
            base = Path(xdg)
        elif home is not None:
            base = Path(home) / ".config"
        else:
            base = None
    if base is None:
        return None
    return base / "piscinebuilder" / "config.yaml"


def load() -> Config:
    p = path()
    if p is None:
        return Config()
    try:
        text = read_text(p)
    except OSError:
        return Config()
    try:
        return schema.load(Config, text)
    except Error as e:
        raise Error(f"{p}: {e.msg}") from None
