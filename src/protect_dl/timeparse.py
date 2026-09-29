"""Parsing of user-supplied timestamps and durations, and chunking of time ranges."""

from __future__ import annotations

import os
import re
from datetime import UTC, datetime, timedelta, tzinfo
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

FORMATS = (
    "%Y-%m-%d %I:%M:%S %p",
    "%Y-%m-%d %I:%M %p",
    "%Y-%m-%d %I:%M:%S%p",
    "%Y-%m-%d %I:%M%p",
    "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%d %H:%M",
)


class TimeParseError(ValueError):
    pass


def local_zone() -> ZoneInfo:
    """The system's local timezone as a DST-aware ZoneInfo."""
    name = os.environ.get("TZ", "").lstrip(":")
    if name:
        try:
            return ZoneInfo(name)
        except (ZoneInfoNotFoundError, ValueError):
            pass
    localtime = Path("/etc/localtime")
    if localtime.exists():
        target = str(localtime.resolve())
        if "zoneinfo/" in target:
            try:
                return ZoneInfo(target.split("zoneinfo/", 1)[1])
            except (ZoneInfoNotFoundError, ValueError):
                pass
        with localtime.open("rb") as f:
            return ZoneInfo.from_file(f, key="localtime")
    raise TimeParseError("Cannot determine the local timezone; pass --tz")


def get_zone(name: str | None) -> ZoneInfo:
    if not name:
        return local_zone()
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError) as e:
        raise TimeParseError(f"Unknown timezone {name!r}") from e


def parse_time(text: str, tz: tzinfo) -> datetime:
    """Parse a wall-clock timestamp in ``tz`` (or an ISO-8601 string with an offset)."""
    cleaned = " ".join(text.strip().replace("T", " ", 1).split())
    naive: datetime | None = None
    try:
        parsed = datetime.fromisoformat(text.strip())
        if parsed.tzinfo is not None:
            return parsed
        naive = parsed
    except ValueError:
        for fmt in FORMATS:
            try:
                naive = datetime.strptime(cleaned, fmt)
                break
            except ValueError:
                continue
    if naive is None:
        raise TimeParseError(
            f"Cannot parse time {text!r}. Use e.g. '2026-09-18 11:33:00 PM', "
            "'2026-09-18 23:33' or ISO-8601."
        )

    earlier = naive.replace(tzinfo=tz, fold=0)
    later = naive.replace(tzinfo=tz, fold=1)
    if earlier.utcoffset() != later.utcoffset():
        roundtrip = earlier.astimezone(UTC).astimezone(tz).replace(tzinfo=None)
        if roundtrip != naive:
            raise TimeParseError(
                f"{text!r} does not exist in {tz} (skipped by a DST change)"
            )
        raise TimeParseError(
            f"{text!r} is ambiguous in {tz} (repeated by a DST change); "
            "give an explicit offset, e.g. '2026-11-01T01:30:00-07:00'"
        )
    return earlier


def parse_duration(text: str) -> timedelta:
    """Parse durations such as '60m', '1h', '90s', '1h30m' or a bare number of minutes."""
    text = text.strip().lower()
    if text.isdigit():
        return timedelta(minutes=int(text))
    match = re.fullmatch(r"(?:(\d+)h)?(?:(\d+)m)?(?:(\d+)s)?", text)
    if not text or not match:
        raise TimeParseError(f"Cannot parse duration {text!r}; use e.g. 60m, 1h, 90s")
    hours, minutes, seconds = (int(g) if g else 0 for g in match.groups())
    return timedelta(hours=hours, minutes=minutes, seconds=seconds)


def split_range(
    start: datetime, end: datetime, chunk: timedelta, tz: tzinfo
) -> list[tuple[datetime, datetime]]:
    """Split [start, end) into chunks whose boundaries fall on multiples of
    ``chunk`` after local midnight, so only the first and last are partial."""
    if end <= start:
        raise TimeParseError("End time must be after start time")
    if chunk < timedelta(minutes=1):
        raise TimeParseError("Chunk size must be at least 1 minute")
    step = chunk.total_seconds()
    chunks = []
    cur = start
    while cur < end:
        local = cur.astimezone(tz)
        since_midnight = (
            local.hour * 3600 + local.minute * 60 + local.second + local.microsecond / 1e6
        )
        boundary = (since_midnight // step + 1) * step
        nxt = min(cur + timedelta(seconds=boundary - since_midnight), end)
        chunks.append((cur, nxt))
        cur = nxt
    return chunks


def to_ms(dt: datetime) -> int:
    return int(dt.timestamp() * 1000)
