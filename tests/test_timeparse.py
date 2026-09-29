from datetime import datetime, timedelta
from itertools import pairwise
from zoneinfo import ZoneInfo

import pytest

from protect_dl.timeparse import TimeParseError, parse_duration, parse_time, split_range

LA = ZoneInfo("America/Los_Angeles")


def test_parse_am_pm():
    dt = parse_time("2026-09-18 11:33:00 PM", LA)
    assert (dt.hour, dt.minute, dt.tzinfo) == (23, 33, LA)
    assert parse_time("2026-09-21 07:36:00 am", LA).hour == 7
    assert parse_time("2026-09-21 7:36 AM", LA).hour == 7


def test_twelve_oclock_edges():
    assert parse_time("2026-09-18 12:00:00 AM", LA).hour == 0
    assert parse_time("2026-09-18 12:00:00 PM", LA).hour == 12


def test_24h_and_iso():
    assert parse_time("2026-09-18 23:33", LA).hour == 23
    dt = parse_time("2026-09-18T23:33:00+00:00", LA)
    assert dt.utcoffset() == timedelta(0)


def test_tz_override_changes_instant():
    a = parse_time("2026-09-18 11:33 PM", LA)
    b = parse_time("2026-09-18 11:33 PM", ZoneInfo("UTC"))
    assert (a - b) == timedelta(hours=7)


def test_dst_gap_and_ambiguity():
    with pytest.raises(TimeParseError, match="does not exist"):
        parse_time("2026-03-08 02:30 AM", LA)
    with pytest.raises(TimeParseError, match="ambiguous"):
        parse_time("2026-11-01 01:30 AM", LA)


def test_garbage():
    with pytest.raises(TimeParseError):
        parse_time("yesterday", LA)


def test_split_example_range():
    start = parse_time("2026-09-18 11:33:00 PM", LA)
    end = parse_time("2026-09-21 07:36:00 AM", LA)
    chunks = split_range(start, end, timedelta(minutes=60), LA)
    assert len(chunks) == 57
    assert chunks[0] == (start, datetime(2026, 9, 19, tzinfo=LA))
    assert chunks[-1] == (datetime(2026, 9, 21, 7, tzinfo=LA), end)
    assert all(a[1] == b[0] for a, b in pairwise(chunks))


def test_split_aligned_start():
    start = datetime(2026, 9, 18, 10, tzinfo=LA)
    chunks = split_range(start, start + timedelta(hours=2), timedelta(hours=1), LA)
    assert len(chunks) == 2


def test_split_across_dst_is_contiguous():
    start = datetime(2026, 3, 7, 22, tzinfo=LA)
    end = datetime(2026, 3, 8, 6, tzinfo=LA)
    chunks = split_range(start, end, timedelta(hours=1), LA)
    assert chunks[0][0] == start and chunks[-1][1] == end
    assert all(a[1] == b[0] and a[0] < a[1] for a, b in pairwise(chunks))


@pytest.mark.parametrize("text,expected", [
    ("60m", timedelta(minutes=60)), ("1h", timedelta(hours=1)),
    ("1h30m", timedelta(minutes=90)), ("90s", timedelta(seconds=90)), ("15", timedelta(minutes=15)),
])
def test_parse_duration(text, expected):
    assert parse_duration(text) == expected
