"""Camera name + wall-clock timestamp, as burned-in drawtext filters or an SRT subtitle track.

Times are anchored to each chunk's start and advance with the chunk's own timeline, so any
gap inside a chunk's recording only skews the rest of that chunk, never the next one."""

from __future__ import annotations

import math
import shutil
import subprocess
from dataclasses import dataclass
from datetime import datetime, timedelta, tzinfo
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .job import Chunk, Job

TIME_FORMAT = "%Y-%m-%d %H:%M:%S"


@dataclass
class Segment:
    """A chunk's position in the concatenated timeline."""

    start: datetime  # wall-clock time of the chunk's first frame
    offset: float  # seconds into the concatenated video
    duration: float


def probe_duration(path: Path) -> float | None:
    ffprobe = shutil.which("ffprobe")
    if not ffprobe:
        return None
    result = subprocess.run(
        [ffprobe, "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(path)],
        capture_output=True, text=True,
    )  # fmt: skip
    try:
        return float(result.stdout.strip()) if result.returncode == 0 else None
    except ValueError:
        return None


def segments(job: Job, chunks: list[Chunk]) -> list[Segment]:
    """Measure each chunk's real duration (falling back to its nominal length)."""
    out, offset = [], 0.0
    for c in chunks:
        duration = probe_duration(job.path(c)) or c.seconds
        out.append(Segment(c.start, offset, duration))
        offset += duration
    return out


def zone(job: Job) -> tzinfo:
    try:
        return ZoneInfo(job.tz)
    except (ZoneInfoNotFoundError, ValueError):
        return job.start.tzinfo  # fixed offset from the manifest


def ffmpeg_env_tz(job: Job) -> str | None:
    """TZ value for ffmpeg so drawtext's localtime matches the job's timezone."""
    try:
        ZoneInfo(job.tz)
        return job.tz
    except (ZoneInfoNotFoundError, ValueError):
        return None


# ---- burned-in text (drawtext) --------------------------------------------------


def _drawtext_escape(text: str) -> str:
    # drawtext text expansion: a backslash makes the next character literal.
    return text.replace("\\", "\\\\").replace("%", "\\%")


def drawtext_filters(job: Job, segs: list[Segment], tmpdir: Path) -> list[str]:
    """One drawtext per chunk, enabled only for that chunk's slice of the timeline.
    Must run before any setpts, so ``t`` and ``pts`` are on the input timeline."""
    filters = []
    for i, seg in enumerate(segs):
        textfile = tmpdir / f"overlay_{i:05d}.txt"
        epoch_at_zero = seg.start.timestamp() - seg.offset
        textfile.write_text(
            f"{_drawtext_escape(job.camera_name)}  "
            f"%{{pts:localtime:{epoch_at_zero:.3f}:%Y-%m-%d %H\\:%M\\:%S}}"
        )
        enable = f"gte(t,{seg.offset:.3f})"
        if i < len(segs) - 1:
            enable += f"*lt(t,{seg.offset + seg.duration:.3f})"
        path = str(textfile).replace("'", r"'\''")
        filters.append(
            f"drawtext=textfile='{path}':enable='{enable}'"
            ":x=h/40:y=h/40:fontsize=h/28:fontcolor=white"
            ":box=1:boxcolor=black@0.55:boxborderw=10"
        )
    return filters


# ---- subtitle track (SRT) --------------------------------------------------------


def _srt_time(seconds: float) -> str:
    ms = round(seconds * 1000)
    h, ms = divmod(ms, 3_600_000)
    m, ms = divmod(ms, 60_000)
    s, ms = divmod(ms, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def write_srt(job: Job, segs: list[Segment], path: Path, speedup: float = 1.0) -> int:
    """One cue per output second. ``speedup`` maps input time to output time (timelapse).
    Returns the number of cues."""
    tz = zone(job)
    step = speedup  # input seconds covered by one output second
    n = 0
    with path.open("w", encoding="utf-8") as f:
        for seg in segs:
            end = seg.offset + seg.duration
            for k in range(math.ceil(seg.duration / step)):
                t0 = seg.offset + k * step
                t1 = min(t0 + step, end)
                wall = (seg.start + timedelta(seconds=k * step)).astimezone(tz)
                n += 1
                f.write(
                    f"{n}\n{_srt_time(t0 / speedup)} --> {_srt_time(t1 / speedup)}\n"
                    f"{job.camera_name} · {wall:{TIME_FORMAT}}\n\n"
                )
    return n
