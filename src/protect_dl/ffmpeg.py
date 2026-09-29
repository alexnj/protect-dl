"""Offline post-processing of a job folder with ffmpeg: lossless merge and timelapse."""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import tempfile
from functools import cache
from pathlib import Path

from rich.console import Console

from . import overlay
from .job import Chunk, Job
from .progress import TaskReporter

TIMELAPSE_FPS = 30


class FfmpegError(RuntimeError):
    pass


def require_ffmpeg() -> str:
    path = shutil.which("ffmpeg")
    if not path:
        raise FfmpegError("ffmpeg not found on PATH (install it, e.g. `brew install ffmpeg`)")
    return path


@cache
def has_encoder(name: str) -> bool:
    out = subprocess.run(
        [require_ffmpeg(), "-hide_banner", "-encoders"], capture_output=True, text=True
    ).stdout
    return re.search(rf"^\s*\S+\s+{re.escape(name)}\s", out, re.MULTILINE) is not None


def is_playable(path: Path) -> bool | None:
    """Whether ffprobe can read a video duration from ``path``; None if ffprobe is missing."""
    if not shutil.which("ffprobe"):
        return None
    duration = overlay.probe_duration(path)
    return duration is not None and duration > 0


def parse_factor(text: str) -> float:
    match = re.fullmatch(r"\s*(\d+(?:\.\d+)?)\s*x?\s*", text, re.IGNORECASE)
    if not match or float(match.group(1)) <= 1:
        raise ValueError(f"Invalid timelapse factor {text!r}; use e.g. 60x")
    return float(match.group(1))


def _base_args() -> list[str]:
    return [
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
        "-progress", "pipe:1", "-nostats",
    ]  # fmt: skip


def _subtitle_args(srt: Path | None) -> tuple[list[str], list[str]]:
    """(extra input args, output args) for muxing ``srt`` as a mov_text subtitle track."""
    if srt is None:
        return [], []
    return ["-i", str(srt)], [
        "-map", "1:s", "-c:s", "mov_text",
        "-metadata:s:s:0", "title=Camera and time", "-metadata:s:s:0", "language=eng",
    ]  # fmt: skip


def merge_args(concat_file: Path, out: Path, srt: Path | None = None) -> list[str]:
    sub_in, sub_out = _subtitle_args(srt)
    return _base_args() + [
        "-f", "concat", "-safe", "0", "-i", str(concat_file), *sub_in,
        "-map", "0", "-c", "copy", *sub_out, "-movflags", "+faststart", str(out),
    ]  # fmt: skip


def encoder_args(encoder: str) -> list[str]:
    if encoder == "hevc_videotoolbox":
        return ["-c:v", "hevc_videotoolbox", "-q:v", "60", "-tag:v", "hvc1"]
    return ["-c:v", "libx264", "-crf", "26", "-preset", "veryfast", "-pix_fmt", "yuv420p"]


def timelapse_filter(factor: float, overlay_filters: list[str] | None = None) -> str:
    # Overlays go first so they see the original timestamps.
    return ",".join([*(overlay_filters or []), f"setpts=PTS/{factor:g}", f"fps={TIMELAPSE_FPS}"])


def timelapse_args(
    concat_file: Path, out: Path, filter_script: Path, encoder: str, srt: Path | None = None
) -> list[str]:
    sub_in, sub_out = _subtitle_args(srt)
    return _base_args() + [
        "-hwaccel", "auto",
        "-f", "concat", "-safe", "0", "-i", str(concat_file), *sub_in,
        "-map", "0:v", "-filter_script:v", str(filter_script),
        "-an", *encoder_args(encoder), *sub_out, "-movflags", "+faststart", str(out),
    ]  # fmt: skip


def _run(
    args: list[str], reporter: TaskReporter, speedup: float = 1.0, tz: str | None = None
) -> None:
    """Run ffmpeg, feeding its -progress output (output timeline) into ``reporter``
    in input-footage seconds."""
    with tempfile.TemporaryFile("w+") as err:
        env = {**os.environ, "TZ": tz} if tz else None
        proc = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=err, text=True, env=env)
        speed = None
        try:
            assert proc.stdout is not None
            for line in proc.stdout:
                key, _, value = line.strip().partition("=")
                if key == "speed" and value not in ("N/A", ""):
                    speed = f"{float(value.rstrip('x')) * speedup:.0f}× realtime"
                elif key in ("out_time_us", "out_time_ms") and value.lstrip("-").isdigit():
                    reporter.update(max(int(value), 0) / 1e6 * speedup, speed)
            proc.wait()
        except BaseException:
            proc.kill()
            proc.wait()
            raise
        if proc.returncode != 0:
            err.seek(0)
            raise FfmpegError(err.read().strip()[-2000:] or f"ffmpeg exited {proc.returncode}")


def _confirm_overwrite(out: Path, overwrite: bool, console: Console) -> bool:
    if not out.exists() or overwrite:
        return True
    if console.is_terminal:
        from rich.prompt import Confirm

        return Confirm.ask(f"{out.name} exists. Overwrite?", default=False, console=console)
    console.print(f"[yellow]{out} exists; skipping (use --overwrite)[/]")
    return False


def _concat(job: Job, tmpdir: Path) -> tuple[Path, list[Chunk]]:
    concat = tmpdir / "concat.txt"
    chunks = job.write_concat_list(concat)
    if not chunks:
        raise FfmpegError(f"{job.folder} has no downloaded chunks")
    return concat, chunks


def _segments(job: Job, chunks: list[Chunk], console: Console) -> list[overlay.Segment]:
    if not shutil.which("ffprobe"):
        console.print("[yellow]ffprobe not found; timestamps assume every chunk is its full length[/]")
    with console.status(f"Measuring {len(chunks)} chunks for timestamps…"):
        return overlay.segments(job, chunks)


def merge(
    job: Job, console: Console, overwrite: bool = False, subtitles: bool = False
) -> Path | None:
    out = job.output_name("merged")
    if not _confirm_overwrite(out, overwrite, console):
        return None
    tmp_out = out.with_suffix(".tmp.mp4")
    with tempfile.TemporaryDirectory() as d:
        tmpdir = Path(d)
        concat, chunks = _concat(job, tmpdir)
        srt = None
        if subtitles:
            srt = tmpdir / "timestamps.srt"
            overlay.write_srt(job, _segments(job, chunks, console), srt)
        seconds = sum(c.seconds for c in chunks)
        with TaskReporter(console, f"Merging {job.camera_name}", seconds) as rep:
            try:
                _run(merge_args(concat, tmp_out, srt), rep)
            except BaseException:
                tmp_out.unlink(missing_ok=True)
                raise
    tmp_out.replace(out)
    return out


def timelapse(
    job: Job,
    factor: float,
    console: Console,
    overwrite: bool = False,
    burn_in: bool = False,
    subtitles: bool = False,
) -> Path | None:
    out = job.output_name(f"timelapse{factor:g}x")
    if not _confirm_overwrite(out, overwrite, console):
        return None
    tmp_out = out.with_suffix(".tmp.mp4")
    encoders = ["libx264"]
    if has_encoder("hevc_videotoolbox"):
        encoders.insert(0, "hevc_videotoolbox")
    with tempfile.TemporaryDirectory() as d:
        tmpdir = Path(d)
        concat, chunks = _concat(job, tmpdir)
        segs = _segments(job, chunks, console) if burn_in or subtitles else []
        filter_script = tmpdir / "filter.txt"
        drawtext = overlay.drawtext_filters(job, segs, tmpdir) if burn_in else None
        filter_script.write_text(timelapse_filter(factor, drawtext))
        srt = None
        if subtitles:
            srt = tmpdir / "timestamps.srt"
            overlay.write_srt(job, segs, srt, speedup=factor)
        seconds = sum(c.seconds for c in chunks)
        tz = overlay.ffmpeg_env_tz(job) if burn_in else None
        for i, encoder in enumerate(encoders):
            label = f"Timelapse {factor:g}× {job.camera_name} ({encoder})"
            try:
                with TaskReporter(console, label, seconds) as rep:
                    args = timelapse_args(concat, tmp_out, filter_script, encoder, srt)
                    _run(args, rep, speedup=factor, tz=tz)
                break
            except FfmpegError as e:
                tmp_out.unlink(missing_ok=True)
                if i == len(encoders) - 1:
                    raise
                console.print(f"[yellow]{encoder} failed ({e.args[0][:200]}); falling back[/]")
            except BaseException:
                tmp_out.unlink(missing_ok=True)
                raise
    tmp_out.replace(out)
    return out
