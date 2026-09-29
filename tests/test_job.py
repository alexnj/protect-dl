from datetime import timedelta

import pytest

from protect_dl.job import PLAYLIST, Job, JobError, find_jobs


def test_create_writes_manifest_and_ordered_playlist(make_job):
    job = make_job()
    assert [c.file[:4] for c in job.chunks] == ["0001", "0002", "0003", "0004"]
    assert job.chunks[0].file == "0001_20260918-223000_20260918-230000.mp4"
    lines = (job.folder / PLAYLIST).read_text().splitlines()
    assert lines[0] == "#EXTM3U"
    assert [line for line in lines if line.endswith(".mp4")] == [c.file for c in job.chunks]
    assert lines[1].startswith("#EXTINF:1800,")


def test_roundtrip(make_job):
    job = make_job()
    loaded = Job.load(job.folder)
    assert loaded.to_json() == job.to_json()


def test_same_params_resumes_different_refused(make_job, tmp_path):
    job = make_job()
    job.chunks[0].status = "done"
    job.save()
    again = make_job()
    assert again.chunks[0].done
    with pytest.raises(JobError, match="time range"):
        make_job(hours=5)
    with pytest.raises(JobError, match="chunk size"):
        make_job(chunk=timedelta(minutes=30))


def _complete(job, *indexes):
    for c in job.chunks:
        if c.index in indexes:
            job.path(c).write_bytes(b"x" * 10)
            job.mark_done(c, 10)


def test_status_and_missing_ranges(make_job):
    job = make_job()
    _complete(job, 1, 4)
    st = job.status()
    assert (st.complete, st.done, st.total, st.bytes) == (False, 2, 4, 20)
    assert st.missing == [(job.chunks[1].start, job.chunks[2].end)]
    assert job.completed_at is None
    _complete(job, 2, 3)
    assert job.status().complete and Job.load(job.folder).completed_at


def test_verify_resets_missing_or_truncated(make_job):
    job = make_job()
    _complete(job, 1, 2, 3, 4)
    job.path(job.chunks[1]).write_bytes(b"x")  # truncated
    job.path(job.chunks[2]).unlink()  # missing
    reset = job.verify()
    assert [c.index for c in reset] == [2, 3]
    assert [c.index for c in Job.load(job.folder).pending()] == [2, 3]


def test_cleanup_partials(make_job):
    job = make_job()
    job.part_path(job.chunks[2]).write_bytes(b"partial")
    assert [p.name for p in job.cleanup_partials()] == [job.chunks[2].file + ".part"]
    assert not list(job.folder.glob("*.part"))


def test_find_jobs(make_job, tmp_path):
    a = make_job(folder=tmp_path / "parent" / "a")
    b = make_job(folder=tmp_path / "parent" / "b")
    assert find_jobs(a.folder) == [a.folder]
    assert find_jobs(tmp_path / "parent") == [a.folder, b.folder]
    assert find_jobs(tmp_path / "nope") == []


def test_concat_list_only_done_in_order(make_job, tmp_path):
    job = make_job()
    _complete(job, 3, 1)
    out = tmp_path / "concat.txt"
    job.write_concat_list(out)
    lines = out.read_text().splitlines()
    assert len(lines) == 2 and "0001_" in lines[0] and "0003_" in lines[1]
