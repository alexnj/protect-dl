from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from protect_dl.job import Job

LA = ZoneInfo("America/Los_Angeles")


@pytest.fixture
def make_job(tmp_path):
    def _make(hours=3, folder=None, chunk=timedelta(hours=1)):
        start = datetime(2026, 9, 18, 22, 30, tzinfo=LA)
        return Job.create(
            folder or tmp_path / "job", camera_id="cam1", camera_name="Front Door",
            channel="high", tz=LA, start=start, end=start + timedelta(hours=hours), chunk=chunk,
        )
    return _make
