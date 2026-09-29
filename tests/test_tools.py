import os
import stat

import pytest

from protect_dl import tools

SLIM = " ... scale  V->V  Scale the input video size.\n"
FULL = SLIM + " T.C drawtext  V->V  Draw text on top of video frames.\n"


def fake_ffmpeg(directory, filters):
    """An executable that answers `-filters` like ffmpeg does."""
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "ffmpeg"
    path.write_text(f"#!/bin/sh\nprintf '%s' '{filters}'\n")  # builtin: PATH is isolated
    path.chmod(path.stat().st_mode | stat.S_IEXEC)
    return str(path)


@pytest.fixture
def env(tmp_path, monkeypatch):
    """Isolated PATH and Homebrew prefix; returns (path_dir, brew_prefix)."""
    tools._listing.cache_clear()
    path_dir, brew = tmp_path / "bin", tmp_path / "brew"
    path_dir.mkdir()
    monkeypatch.setenv("PATH", str(path_dir))
    monkeypatch.delenv(tools.ENV_VAR, raising=False)
    monkeypatch.setattr(tools, "BREW_PREFIXES", (str(brew),))
    yield path_dir, brew
    tools._listing.cache_clear()


def test_path_ffmpeg_used_when_it_has_the_filter(env):
    path_dir, _ = env
    slim = fake_ffmpeg(path_dir, FULL)
    assert tools.ffmpeg() == slim and tools.ffmpeg("drawtext") == slim


def test_falls_back_to_keg_only_ffmpeg_full_for_drawtext(env):
    path_dir, brew = env
    slim = fake_ffmpeg(path_dir, SLIM)
    full = fake_ffmpeg(brew / "opt/ffmpeg-full/bin", FULL)
    assert tools.ffmpeg() == slim  # plain merge/timelapse keep using the PATH one
    assert tools.ffmpeg("drawtext") == full


def test_missing_filter_explains_ffmpeg_full(env):
    fake_ffmpeg(env[0], SLIM)
    with pytest.raises(tools.FfmpegError, match="brew install ffmpeg-full"):
        tools.ffmpeg("drawtext")


def test_no_ffmpeg_at_all(env):
    with pytest.raises(tools.FfmpegError, match="ffmpeg not found"):
        tools.ffmpeg()


def test_env_var_overrides_everything(env, monkeypatch, tmp_path):
    fake_ffmpeg(env[0], FULL)
    custom = fake_ffmpeg(tmp_path / "custom", SLIM)
    monkeypatch.setenv(tools.ENV_VAR, custom)
    assert tools.ffmpeg() == custom
    with pytest.raises(tools.FfmpegError, match="lacks the drawtext filter"):
        tools.ffmpeg("drawtext")
    monkeypatch.setenv(tools.ENV_VAR, str(tmp_path / "nope"))
    with pytest.raises(tools.FfmpegError, match="not an executable"):
        tools.ffmpeg()


def test_ffprobe_next_to_chosen_ffmpeg(env):
    path_dir, _ = env
    fake_ffmpeg(path_dir, SLIM)
    probe = path_dir / "ffprobe"
    probe.write_text("#!/bin/sh\n")
    probe.chmod(0o755)
    assert tools.ffprobe() == str(probe)
    os.remove(probe)
    assert tools.ffprobe() is None
