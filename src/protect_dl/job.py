"""A job folder: ordered chunk files, a manifest (source of truth) and a playlist."""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

from .timeparse import split_range, to_ms

MANIFEST = "manifest.json"
PLAYLIST = "playlist.m3u8"
FORMAT_VERSION = 1


class JobError(Exception):
    pass


def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def slugify(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9]+", "-", name).strip("-").lower() or "camera"


def default_folder_name(camera_name: str, start: datetime, end: datetime) -> str:
    return f"{slugify(camera_name)}_{start:%Y-%m-%d_%H%M}_to_{end:%Y-%m-%d_%H%M}"


def find_jobs(folder: Path) -> list[Path]:
    """A job folder itself, or the job folders directly inside a parent folder."""
    if (folder / MANIFEST).is_file():
        return [folder]
    if folder.is_dir():
        return sorted(p for p in folder.iterdir() if (p / MANIFEST).is_file())
    return []


@dataclass
class Chunk:
    index: int
    start: datetime
    end: datetime
    file: str
    status: str = "pending"
    bytes: int = 0
    completed_at: str | None = None
    short_by: int = 0  # bytes the console promised but never sent (see downloader)

    @property
    def done(self) -> bool:
        return self.status == "done"

    @property
    def seconds(self) -> float:
        return (self.end - self.start).total_seconds()

    @property
    def label(self) -> str:
        return f"#{self.index:04d} {self.start:%Y%m%d-%H%M%S}→{self.end:%H%M%S}"

    def to_json(self) -> dict:
        return {
            "index": self.index,
            "start": self.start.isoformat(),
            "end": self.end.isoformat(),
            "start_ms": to_ms(self.start),
            "end_ms": to_ms(self.end),
            "file": self.file,
            "status": self.status,
            "bytes": self.bytes,
            "completed_at": self.completed_at,
            "short_by": self.short_by,
        }

    @classmethod
    def from_json(cls, d: dict) -> Chunk:
        return cls(
            index=d["index"],
            start=datetime.fromisoformat(d["start"]),
            end=datetime.fromisoformat(d["end"]),
            file=d["file"],
            status=d.get("status", "pending"),
            bytes=d.get("bytes", 0),
            completed_at=d.get("completed_at"),
            short_by=d.get("short_by", 0),
        )


@dataclass
class JobStatus:
    complete: bool
    done: int
    total: int
    bytes: int
    done_seconds: float
    total_seconds: float
    missing: list[tuple[datetime, datetime]] = field(default_factory=list)
    short: list[Chunk] = field(default_factory=list)


@dataclass
class Job:
    folder: Path
    camera_id: str
    camera_name: str
    channel: str
    tz: str
    start: datetime
    end: datetime
    chunk_seconds: int
    chunks: list[Chunk]
    created_at: str
    completed_at: str | None = None

    # ---- creation / loading -------------------------------------------------

    @classmethod
    def create(
        cls,
        folder: Path,
        *,
        camera_id: str,
        camera_name: str,
        channel: str,
        tz,
        start: datetime,
        end: datetime,
        chunk: timedelta,
    ) -> Job:
        """Create a new job folder, or return the existing job if it has the same parameters."""
        if (folder / MANIFEST).exists():
            job = cls.load(folder)
            mismatches = job.mismatches(camera_id, channel, start, end, chunk)
            if mismatches:
                raise JobError(
                    f"{folder} already holds a different job ({', '.join(mismatches)} differ). "
                    "Pick another --folder, or resume it with: "
                    f"protect-dl --download --folder {folder}"
                )
            return job

        chunks = []
        for i, (s, e) in enumerate(split_range(start, end, chunk, tz), start=1):
            s, e = s.astimezone(tz), e.astimezone(tz)
            chunks.append(
                Chunk(i, s, e, f"{i:04d}_{s:%Y%m%d-%H%M%S}_{e:%Y%m%d-%H%M%S}.mp4")
            )
        job = cls(
            folder=folder,
            camera_id=camera_id,
            camera_name=camera_name,
            channel=channel,
            tz=getattr(tz, "key", None) or str(tz),
            start=start.astimezone(tz),
            end=end.astimezone(tz),
            chunk_seconds=int(chunk.total_seconds()),
            chunks=chunks,
            created_at=now_iso(),
        )
        folder.mkdir(parents=True, exist_ok=True)
        job.save()
        job.write_playlist()
        return job

    @classmethod
    def load(cls, folder: Path) -> Job:
        path = folder / MANIFEST
        try:
            d = json.loads(path.read_text())
        except FileNotFoundError:
            raise JobError(f"{folder} is not a job folder (no {MANIFEST})") from None
        except json.JSONDecodeError as e:
            raise JobError(f"{path} is corrupt: {e}") from e
        if d.get("version") != FORMAT_VERSION:
            raise JobError(f"{path} has unsupported version {d.get('version')!r}")
        return cls(
            folder=folder,
            camera_id=d["camera"]["id"],
            camera_name=d["camera"]["name"],
            channel=d["camera"]["channel"],
            tz=d["timezone"],
            start=datetime.fromisoformat(d["start"]),
            end=datetime.fromisoformat(d["end"]),
            chunk_seconds=d["chunk_seconds"],
            chunks=[Chunk.from_json(c) for c in d["chunks"]],
            created_at=d["created_at"],
            completed_at=d.get("completed_at"),
        )

    def mismatches(
        self, camera_id: str, channel: str, start: datetime, end: datetime, chunk: timedelta
    ) -> list[str]:
        out = []
        if camera_id != self.camera_id:
            out.append("camera")
        if channel != self.channel:
            out.append("channel")
        if to_ms(start) != to_ms(self.start) or to_ms(end) != to_ms(self.end):
            out.append("time range")
        if int(chunk.total_seconds()) != self.chunk_seconds:
            out.append("chunk size")
        return out

    # ---- persistence --------------------------------------------------------

    def to_json(self) -> dict:
        return {
            "version": FORMAT_VERSION,
            "camera": {"id": self.camera_id, "name": self.camera_name, "channel": self.channel},
            "timezone": self.tz,
            "start": self.start.isoformat(),
            "end": self.end.isoformat(),
            "start_ms": to_ms(self.start),
            "end_ms": to_ms(self.end),
            "chunk_seconds": self.chunk_seconds,
            "created_at": self.created_at,
            "completed_at": self.completed_at,
            "chunks": [c.to_json() for c in self.chunks],
        }

    def save(self) -> None:
        path = self.folder / MANIFEST
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(self.to_json(), indent=2) + "\n")
        os.replace(tmp, path)

    def write_playlist(self) -> None:
        lines = ["#EXTM3U"]
        for c in self.chunks:
            lines.append(
                f"#EXTINF:{c.seconds:.0f},{self.camera_name} "
                f"{c.start:%Y-%m-%d %H:%M:%S} → {c.end:%Y-%m-%d %H:%M:%S}"
            )
            lines.append(c.file)
        (self.folder / PLAYLIST).write_text("\n".join(lines) + "\n", encoding="utf-8")

    # ---- chunk bookkeeping --------------------------------------------------

    def path(self, chunk: Chunk) -> Path:
        return self.folder / chunk.file

    def part_path(self, chunk: Chunk) -> Path:
        return self.folder / (chunk.file + ".part")

    def cleanup_partials(self) -> list[Path]:
        removed = sorted(self.folder.glob("*.part"))
        for p in removed:
            p.unlink(missing_ok=True)
        return removed

    def verify(self) -> list[Chunk]:
        """Reset 'done' chunks whose file is missing or has the wrong size."""
        reset = []
        for c in self.chunks:
            if not c.done:
                continue
            p = self.path(c)
            if not p.is_file() or p.stat().st_size != c.bytes or c.bytes == 0:
                c.status, c.bytes, c.completed_at, c.short_by = "pending", 0, None, 0
                reset.append(c)
        if reset:
            self.completed_at = None
            self.save()
        return reset

    def pending(self) -> list[Chunk]:
        return [c for c in self.chunks if not c.done]

    def mark_done(self, chunk: Chunk, size: int, short_by: int = 0) -> None:
        chunk.status, chunk.bytes, chunk.completed_at = "done", size, now_iso()
        chunk.short_by = short_by
        if not self.pending():
            self.completed_at = now_iso()
        self.save()

    def status(self) -> JobStatus:
        done = [c for c in self.chunks if c.done]
        missing: list[tuple[datetime, datetime]] = []
        for c in self.chunks:
            if c.done:
                continue
            if missing and missing[-1][1] == c.start:
                missing[-1] = (missing[-1][0], c.end)
            else:
                missing.append((c.start, c.end))
        return JobStatus(
            complete=not missing,
            done=len(done),
            total=len(self.chunks),
            bytes=sum(c.bytes for c in done),
            done_seconds=sum(c.seconds for c in done),
            total_seconds=sum(c.seconds for c in self.chunks),
            missing=missing,
            short=[c for c in done if c.short_by],
        )

    # ---- post-processing helpers --------------------------------------------

    def write_concat_list(self, path: Path) -> list[Chunk]:
        """Write an ffmpeg concat-demuxer list of the downloaded chunks, in order."""
        done = [c for c in self.chunks if c.done]
        lines = []
        for c in done:
            escaped = str(self.path(c).resolve()).replace("'", r"'\''")
            lines.append(f"file '{escaped}'")
        path.write_text("\n".join(lines) + "\n")
        return done

    def output_name(self, prefix: str) -> Path:
        return self.folder / f"{prefix}_{self.start:%Y%m%d-%H%M}_to_{self.end:%Y%m%d-%H%M}.mp4"
