# Changelog

## 0.1.0

First release.

- Download footage for a time range into a job folder of clock-aligned chunks, with a manifest and an M3U playlist.
- Resume an interrupted download; only the interrupted chunk is fetched again.
- Live progress, stall detection, retries, and handling of exports the console repeatedly cuts short.
- Offline `--merge` (lossless) and `--timelapse` (ffmpeg), with an optional burned-in camera name and time (`--burn-in`) or a subtitle track (`--subtitles`).
- `--channel package` downloads from the second lens of cameras such as the G4 Doorbell Pro.
- `--debug` logs console requests, versions and full errors for bug reports, without credentials.
- Works with ffmpeg 6 through 9. `--burn-in` uses Homebrew's `ffmpeg-full` automatically when the default ffmpeg lacks `drawtext`; `PROTECT_DL_FFMPEG` picks a specific ffmpeg.
