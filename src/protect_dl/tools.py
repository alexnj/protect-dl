"""Locating ffmpeg and ffprobe.

Homebrew's default ffmpeg leaves out some filters (notably drawtext, used by --burn-in). Its
``ffmpeg-full`` formula has them but is keg-only, so it is not on PATH; look for it too."""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from functools import cache
from pathlib import Path

ENV_VAR = "PROTECT_DL_FFMPEG"
BREW_PREFIXES = ("/opt/homebrew", "/usr/local", "/home/linuxbrew/.linuxbrew")


class FfmpegError(RuntimeError):
    pass


def _candidates() -> list[str]:
    """ffmpeg binaries to consider, best first. An explicit PROTECT_DL_FFMPEG is the only one."""
    if explicit := os.environ.get(ENV_VAR):
        path = shutil.which(explicit)
        if not path:
            raise FfmpegError(f"{ENV_VAR}={explicit} is not an executable ffmpeg")
        return [path]
    found = []
    if path := shutil.which("ffmpeg"):
        found.append(path)
    for prefix in BREW_PREFIXES:
        full = Path(prefix, "opt", "ffmpeg-full", "bin", "ffmpeg")
        if full.is_file() and os.access(full, os.X_OK):
            found.append(str(full))
    return found


@cache
def _listing(binary: str, flag: str) -> str:
    try:
        return subprocess.run(
            [binary, "-hide_banner", flag], capture_output=True, text=True, timeout=30
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return ""


def _listed(binary: str, flag: str, name: str) -> bool:
    # Lines look like " T.C drawtext   V->V  Draw text..." or " V....D libx264  ...".
    return re.search(rf"^\s*\S+\s+{re.escape(name)}\s", _listing(binary, flag), re.MULTILINE) is not None


def has_filter(binary: str, name: str) -> bool:
    return _listed(binary, "-filters", name)


def has_encoder(binary: str, name: str) -> bool:
    return _listed(binary, "-encoders", name)


def ffmpeg(*filters: str) -> str:
    """Path of an ffmpeg that has all of ``filters``."""
    candidates = _candidates()
    if not candidates:
        raise FfmpegError(
            "ffmpeg not found on PATH (install it, e.g. `brew install ffmpeg` or `apt install ffmpeg`)"
        )
    for binary in candidates:
        if all(has_filter(binary, f) for f in filters):
            return binary
    missing = ", ".join(f for f in filters if not has_filter(candidates[0], f))
    raise FfmpegError(
        f"Your ffmpeg ({candidates[0]}) lacks the {missing} filter, which --burn-in needs. "
        "Homebrew's default ffmpeg leaves it out: run `brew install ffmpeg-full` (protect-dl finds "
        f"it automatically), or set {ENV_VAR} to an ffmpeg that has it. --subtitles works without it."
    )


def ffprobe() -> str | None:
    """ffprobe next to the ffmpeg in use, else the one on PATH."""
    try:
        sibling = Path(ffmpeg()).with_name("ffprobe")
        if sibling.is_file():
            return str(sibling)
    except FfmpegError:
        pass
    return shutil.which("ffprobe")
