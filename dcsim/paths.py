"""Where datacenter-sim keeps its editable files.

Everything the user can change lives in one data folder (default ``~/.dcsim``,
override with the ``DCSIM_HOME`` environment variable or ``dcsim --home``):

    config.yaml        settings
    interfaces.yaml    client behaviour library
    profiles/*.yaml    workload profiles
    runs/<id>/         recorded test runs and reports
    logs/              server and control-panel logs
"""
from __future__ import annotations

import os
import shutil
from pathlib import Path

PACKAGE_DIR = Path(__file__).resolve().parent
DEFAULTS_DIR = PACKAGE_DIR / "defaults"
UI_DIR = PACKAGE_DIR / "ui"

_home_override: Path | None = None


def set_home(path: str | os.PathLike | None) -> None:
    global _home_override
    _home_override = Path(path).expanduser().resolve() if path else None


def home() -> Path:
    if _home_override:
        return _home_override
    env = os.environ.get("DCSIM_HOME")
    return Path(env).expanduser().resolve() if env else Path.home() / ".dcsim"


def config_file() -> Path:
    return home() / "config.yaml"


def interfaces_file() -> Path:
    return home() / "interfaces.yaml"


def profiles_dir() -> Path:
    return home() / "profiles"


def runs_dir() -> Path:
    return home() / "runs"


def logs_dir() -> Path:
    return home() / "logs"


def scratch_dir() -> Path:
    return home() / "scratch"


def ensure_home(reset: bool = False) -> Path:
    """Create the data folder and copy defaults the first time (or on reset).

    Profiles are copied only on first setup, so profiles the user deleted are
    not brought back on the next launch.
    """
    h = home()
    marker = h / ".initialized"
    for d in (h, profiles_dir(), runs_dir(), logs_dir(), scratch_dir()):
        d.mkdir(parents=True, exist_ok=True)

    first = reset or not marker.exists()
    for name in ("config.yaml", "interfaces.yaml"):
        dst = h / name
        if reset or not dst.exists():
            shutil.copyfile(DEFAULTS_DIR / name, dst)
    if first:
        for src in sorted((DEFAULTS_DIR / "profiles").glob("*.yaml")):
            dst = profiles_dir() / src.name
            if reset or not dst.exists():
                shutil.copyfile(src, dst)
        marker.write_text("1")
    return h


def restore_default_profiles() -> list[str]:
    restored = []
    for src in sorted((DEFAULTS_DIR / "profiles").glob("*.yaml")):
        dst = profiles_dir() / src.name
        if not dst.exists():
            shutil.copyfile(src, dst)
            restored.append(src.stem)
    return restored
