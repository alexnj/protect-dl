import json
import shutil
import subprocess

import pytest
from rich.console import Console

from protect_dl import ffmpeg


def test_parse_factor():
    assert ffmpeg.parse_factor("60x") == 60
    assert ffmpeg.parse_factor("120") == 120
    for bad in ("x", "1x", "0.5x", "fast"):
        with pytest.raises(ValueError):
            ffmpeg.parse_factor(bad)


def test_args(tmp_path):
    concat, out = tmp_path / "c.txt", tmp_path / "o.mp4"
    m = ffmpeg.merge_args(concat, out)
    assert m[m.index("-f") + 1] == "concat" and m[m.index("-c") + 1] == "copy" and m[-1] == str(out)
    t = ffmpeg.timelapse_args(concat, out, tmp_path / "f.txt", "libx264")
    assert t[t.index("-filter_script:v") + 1] == str(tmp_path / "f.txt") and "-an" in t and "libx264" in t
    assert "-progress" in m and "-progress" in t
    assert ffmpeg.timelapse_filter(60) == "setpts=PTS/60,fps=30"
    assert ffmpeg.timelapse_filter(60, ["drawtext=a"]) == "drawtext=a,setpts=PTS/60,fps=30"
    ms = ffmpeg.merge_args(concat, out, tmp_path / "s.srt")
    assert ms[ms.index("-c:s") + 1] == "mov_text" and "1:s" in ms


needs_ffmpeg = pytest.mark.skipif(not shutil.which("ffmpeg"), reason="ffmpeg not installed")


def duration(path):
    out = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                          "-of", "json", str(path)], capture_output=True, text=True).stdout
    return float(json.loads(out)["format"]["duration"])


@needs_ffmpeg
def test_merge_and_timelapse_real(make_job):
    from datetime import timedelta
    job = make_job(hours=1, chunk=timedelta(minutes=20))
    for c in job.chunks:
        secs = c.seconds / 60  # 1 real second of test video per footage minute
        subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", f"testsrc=size=160x120:rate=15:duration={secs}",
                        "-c:v", "libx264", "-pix_fmt", "yuv420p", str(job.path(c))], check=True)
        job.mark_done(c, job.path(c).stat().st_size)
    console = Console(file=open("/dev/null", "w"))
    merged = ffmpeg.merge(job, console)
    assert duration(merged) == pytest.approx(60, abs=0.5)
    tl = ffmpeg.timelapse(job, 10, console)
    assert duration(tl) == pytest.approx(6, abs=0.5)
    assert not list(job.folder.glob("*.tmp.mp4"))


def streams(path):
    out = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "stream=codec_type",
                          "-of", "csv=p=0", str(path)], capture_output=True, text=True).stdout
    return out.split()


@needs_ffmpeg
def test_subtitles_and_burn_in_real(make_job):
    from datetime import timedelta
    job = make_job(hours=1, chunk=timedelta(minutes=20))
    for c in job.chunks:
        subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i",
                        f"testsrc=size=160x120:rate=15:duration={c.seconds / 60}",
                        "-c:v", "libx264", "-pix_fmt", "yuv420p", str(job.path(c))], check=True)
        job.mark_done(c, job.path(c).stat().st_size)
    console = Console(file=open("/dev/null", "w"))
    merged = ffmpeg.merge(job, console, subtitles=True)
    assert streams(merged) == ["video", "subtitle"]
    tl = ffmpeg.timelapse(job, 10, console, burn_in=True, subtitles=True)
    assert streams(tl) == ["video", "subtitle"]
    assert duration(tl) == pytest.approx(6, abs=0.5)
