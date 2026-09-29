from protect_dl import overlay


def test_srt_cues_reset_per_chunk(make_job, tmp_path, monkeypatch):
    job = make_job(hours=1)  # 22:30-23:00 and 23:00-23:30
    # First chunk's recording is 10 s shorter than nominal (a gap in the recording).
    durations = iter([1790.0, 1800.0])
    monkeypatch.setattr(overlay, "probe_duration", lambda p: next(durations))
    segs = overlay.segments(job, job.chunks)
    assert [(s.offset, s.duration) for s in segs] == [(0.0, 1790.0), (1790.0, 1800.0)]

    srt = tmp_path / "t.srt"
    n = overlay.write_srt(job, segs, srt, speedup=10)
    assert n == 179 + 180
    cues = srt.read_text().split("\n\n")
    assert cues[0].splitlines()[1:] == ["00:00:00,000 --> 00:00:01,000", "Front Door · 2026-09-18 22:30:00"]
    # The second chunk starts exactly at its own wall-clock start despite the earlier gap.
    assert cues[179].splitlines()[1:] == ["00:02:59,000 --> 00:03:00,000", "Front Door · 2026-09-18 23:00:00"]


def test_drawtext_per_chunk(make_job, tmp_path, monkeypatch):
    job = make_job(hours=1)
    monkeypatch.setattr(overlay, "probe_duration", lambda p: None)  # nominal lengths
    segs = overlay.segments(job, job.chunks)
    f = overlay.drawtext_filters(job, segs, tmp_path)
    assert len(f) == 2
    assert "enable='gte(t,0.000)*lt(t,1800.000)'" in f[0] and "enable='gte(t,1800.000)'" in f[1]
    text = (tmp_path / "overlay_00001.txt").read_text()
    epoch = job.chunks[1].start.timestamp() - 1800
    assert text == f"Front Door  %{{pts:localtime:{epoch:.3f}:%Y-%m-%d %H\\:%M\\:%S}}"


def test_drawtext_escapes_camera_name():
    assert overlay._drawtext_escape("100% \\ yard") == "100\\% \\\\ yard"
