"""Per-user runtime locations, resolved when a command or operation uses them."""
from __future__ import annotations

import os
from pathlib import Path
import sys


def configured_path(value: str | os.PathLike[str]) -> Path:
    """Expand a configured path without following or creating filesystem entries."""
    return Path(os.path.abspath(Path(value).expanduser()))


def data_root() -> Path:
    """Return the explicit data root or the current user's platform default."""
    override = os.environ.get("MODPORT_DATA_ROOT")
    if override:
        return configured_path(override)
    home = Path.home()
    if sys.platform == "win32":
        local = os.environ.get("LOCALAPPDATA")
        return configured_path(local or home / "AppData" / "Local") / "ModPort"
    if sys.platform == "darwin":
        return home / "Library" / "Application Support" / "ModPort"
    xdg = os.environ.get("XDG_DATA_HOME")
    base = Path(xdg).expanduser() if xdg else home / ".local" / "share"
    if not base.is_absolute():
        base = home / ".local" / "share"
    return base / "modport"


def runs_root(path: str | os.PathLike[str] | None = None) -> Path:
    return configured_path(path or os.environ.get("MODPORT_OUTPUT_ROOT") or data_root() / "runs")


def skill_store(path: str | os.PathLike[str] | None = None) -> Path:
    return configured_path(path or os.environ.get("MODPORT_SKILL_STORE") or data_root() / "migration-skills")


def archives_root(path: str | os.PathLike[str] | None = None) -> Path:
    return configured_path(path or os.environ.get("MODPORT_ARCHIVE_ROOT") or data_root() / "archives")
