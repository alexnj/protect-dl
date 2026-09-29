import asyncio

from rich.console import Console

from protect_dl.downloader import DownloadOptions, download_job
from protect_dl.job import Job


class FakeClient:
    def __init__(self, hang_on=None):
        self.requested = []
        self.hang_on = hang_on

    async def download_chunk(self, camera_id, start, end, channel, dest, on_bytes):
        self.requested.append(start)
        with dest.open("wb") as f:
            for _ in range(3):
                f.write(b"v" * 100)
                on_bytes(100, 0)
                await asyncio.sleep(0)
            if start == self.hang_on:
                await asyncio.sleep(3600)
        return 300

    async def relogin(self):
        pass


def run(client, job, **opts):
    console = Console(file=open("/dev/null", "w"))
    opts.setdefault("retry_delay", 0)
    return asyncio.run(download_job(client, job, DownloadOptions(**opts), console))


def test_fresh_download_completes(make_job):
    job = make_job()
    assert run(FakeClient(), job)
    loaded = Job.load(job.folder)
    assert loaded.status().complete
    assert all(loaded.path(c).stat().st_size == 300 for c in loaded.chunks)


def test_resume_redownloads_only_interrupted_and_later(make_job):
    job = make_job()
    for c in job.chunks[:2]:
        job.path(c).write_bytes(b"v" * 300)
        job.mark_done(c, 300)
    job.part_path(job.chunks[2]).write_bytes(b"half")  # interrupted mid-chunk
    client = FakeClient()
    assert run(client, Job.load(job.folder))
    assert client.requested == [c.start for c in job.chunks[2:]]


def test_complete_folder_downloads_nothing(make_job):
    job = make_job()
    run(FakeClient(), job)
    client = FakeClient()
    assert run(client, Job.load(job.folder))
    assert client.requested == []


def test_stall_aborts_and_leaves_chunk_pending(make_job):
    job = make_job()
    stuck = job.chunks[1]
    client = FakeClient(hang_on=stuck.start)
    assert not run(client, job, stall_timeout=0.3, retries=0)
    loaded = Job.load(job.folder)
    assert [c.index for c in loaded.pending()] == [stuck.index]
    assert not list(job.folder.glob("*.part"))
    # later chunks still downloaded after the failure
    assert client.requested == [c.start for c in job.chunks]


class TruncatingClient(FakeClient):
    """Sends ``cut_offs[n]`` of 1000 promised bytes on attempt n, then reports truncation."""

    def __init__(self, cut_offs):
        super().__init__()
        self.cut_offs = list(cut_offs)

    async def download_chunk(self, camera_id, start, end, channel, dest, on_bytes):
        from protect_dl.client import ExportTruncated
        self.requested.append(start)
        n = self.cut_offs.pop(0) if self.cut_offs else 1000
        dest.write_bytes(b"v" * n)
        on_bytes(n, 1000)
        if n < 1000:
            raise ExportTruncated(f"console closed the export early ({n} of 1000 bytes)")
        return n


def test_same_cut_off_twice_keeps_short_file(make_job, monkeypatch):
    monkeypatch.setattr("protect_dl.ffmpeg.is_playable", lambda p: True)
    job = make_job(hours=1)  # 22:30-23:30 -> 2 chunks
    assert run(TruncatingClient([900, 900]), job, retries=3)
    first = Job.load(job.folder).chunks[0]
    assert (first.done, first.bytes, first.short_by) == (True, 900, 100)
    assert Job.load(job.folder).status().short == [first]


def test_varying_cut_off_keeps_retrying(make_job, monkeypatch):
    monkeypatch.setattr("protect_dl.ffmpeg.is_playable", lambda p: True)
    job = make_job(hours=1)
    client = TruncatingClient([500, 700, 1000])
    assert run(client, job, retries=3)
    first = Job.load(job.folder).chunks[0]
    assert (first.bytes, first.short_by) == (1000, 0)
    assert len(client.requested) == 4  # 3 attempts for chunk 1, 1 for chunk 2


def test_unplayable_short_file_not_kept(make_job, monkeypatch):
    monkeypatch.setattr("protect_dl.ffmpeg.is_playable", lambda p: False)
    job = make_job(hours=1)
    assert not run(TruncatingClient([900, 900, 900]), job, retries=2)
    assert [c.index for c in Job.load(job.folder).pending()] == [1]
    assert not list(job.folder.glob("*.part"))
