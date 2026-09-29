# protect-dl

Download recorded footage for a time range from a local UniFi Protect console. It works for anything from a few minutes to several days. The footage is saved as a folder of clips that you can play in order, merge into one file, or turn into a timelapse.

```
$ protect-dl --download --camera "Front Door" \
    --start "2026-09-18 11:33:00 PM" --end "2026-09-21 07:36:00 AM"
──────────── Front Door → front-door_2026-09-18_2333_to_2026-09-21_0736 ────────────
✓ #0001 20260918-233300→000000 51.2 MB in 0m06s
✓ #0002 20260919-000000→010000 116.4 MB in 0m13s
⠧ Overall                      ━━╸━━━━━━━━━━━━━━━  2/57 chunks · 1.5/56.0 h footage · 167.6 MB · ETA 13m10s
⠧ #0003 20260919-010000→020000 ━━━━━━━━╺━━━━━━━━━  58.6 MB / 116.9 MB · 9.0 MB/s · last byte 0s ago
```

- **Resumable.** If a download is interrupted, running the same command again re-fetches only the clip it was working on.
- **Shows when it's stuck.** The progress rows update live. If no data arrives, the current row turns yellow. After a timeout the clip is retried automatically.
- **Download once, process offline.** Merging and timelapses work from the downloaded folder, with no network access.
- **Timestamps.** It can draw the camera name and time into a timelapse, or add them as a subtitle track.

> Not affiliated with or endorsed by Ubiquiti. UniFi and UniFi Protect are trademarks of Ubiquiti Inc.
> protect-dl uses the unofficial, undocumented API that the Protect web app itself uses. Ubiquiti's official Integration API can't export recordings. A Protect update could change the unofficial API without notice; if protect-dl stops working after an update, please [open an issue](https://github.com/alexnj/protect-dl/issues).

## Install

```sh
brew install alexnj/tap/protect-dl
```

Or with [pipx](https://pipx.pypa.io/) or [uv](https://docs.astral.sh/uv/). Install [ffmpeg](https://ffmpeg.org/) separately if you want to merge or make timelapses:

```sh
pipx install protect-dl
uv tool install protect-dl
```

Needs Python 3.11 or newer, on macOS or Linux.

## Set up

protect-dl logs in to the console the same way the Protect web app does. Create a **local** user for it on the console (UniFi OS → Admins & Users → local access only). Ubiquiti SSO accounts and two-factor logins can't be used. protect-dl only reads from the console, so give that user a **view-only** Protect role, not an admin one.

You can pass the connection settings as options every time. Alternatively, put them in `~/.config/protect-dl/config.env`, or in a `.env` file in the directory where you run protect-dl:

```sh
PROTECT_HOST=192.168.1.1
PROTECT_USERNAME=protect-dl
PROTECT_PASSWORD=...
PROTECT_VERIFY_SSL=false   # consoles use a self-signed certificate
```

Keep the file private (`chmod 600 ~/.config/protect-dl/config.env`). Avoid `--password` on the command line, because it is saved in your shell history and other users of the machine can see it while protect-dl runs. If no password is set anywhere, protect-dl asks for it. Check the setup with:

```sh
protect-dl --list-cameras
```

## Usage

```sh
# Download: a folder is created in the current directory, named after the camera and range
protect-dl --download --camera "Front Door" \
    --start "2026-09-18 11:33:00 PM" --end "2026-09-21 07:36:00 AM"

# Choose the folder, and merge + make a timelapse once the download finishes
protect-dl --download --merge --timelapse 60x --folder ./trip \
    --camera "Front Door" --start "2026-09-18 11:33 PM" --end "2026-09-21 7:36 AM"

# Resume an interrupted download (camera and range are read from the folder)
protect-dl --download --folder ./trip

# Check whether a folder holds a complete download
protect-dl --folder ./trip

# Work offline from a downloaded folder
protect-dl --folder ./trip --merge
protect-dl --folder ./trip --timelapse 120x --burn-in
```

Times are in your computer's local timezone; `--tz Europe/London` overrides it. 12-hour (`11:33 PM`), 24-hour (`23:33`) and ISO-8601 formats all work. To download several cameras, repeat `--camera`. Each camera gets its own folder, and those go inside `--folder` if you give one.

`--channel` picks the stream: `high` (default), `medium` or `low` for smaller files. On cameras with a second, downward-facing package lens, such as the G4 Doorbell Pro, `package` picks that lens. `protect-dl --list-cameras` shows which streams each camera has. Run `protect-dl --help` for all options, including `--chunk` and `--stall-timeout`.

## The download folder

```
manifest.json      camera, time range, and the status and size of every clip
playlist.m3u8      the clips in playback order (open in VLC or mpv)
0001_20260918-233300_20260919-000000.mp4
0002_20260919-000000_20260919-010000.mp4
...
merged_20260918-2333_to_20260921-0736.mp4        (--merge)
timelapse60x_20260918-2333_to_20260921-0736.mp4  (--timelapse 60x)
```

Footage is downloaded in clips of one hour (`--chunk`), lined up with the clock, so only the first and last clips are partial. Each clip is written to a `.part` file and renamed when it finishes. The manifest is updated only after that rename. So after a crash, Ctrl-C or network loss, a resumed download can always tell which clips are finished.

Before resuming or processing a folder, protect-dl checks that every finished clip still exists at its recorded size. `protect-dl --folder ./trip` reports **COMPLETE** or **INCOMPLETE**, and lists any missing time ranges.

Sometimes the console ends an export at exactly the same byte every time it's asked, usually because the recording itself is damaged. When that happens, protect-dl keeps what the console sent, provided it plays. The clip is then marked *short* in the status, instead of being retried forever.

## Merging and timelapses

These need `ffmpeg`, and they refuse to run on an incomplete folder unless you pass `--force`.

- `--merge` joins the clips into one MP4 without re-encoding, so it's fast and loses no quality.
- `--timelapse 60x` speeds the footage up 60 times and removes the audio. It uses the Mac's hardware HEVC encoder when available, and otherwise x264.
- `--burn-in` draws the camera name and time into the timelapse. It needs an ffmpeg with the `drawtext` filter. Homebrew's default `ffmpeg` doesn't have it, so run `brew install ffmpeg-full`. protect-dl finds that build automatically, even though Homebrew doesn't put it on your PATH. To use a different ffmpeg build, set `PROTECT_DL_FFMPEG` to its path.
- `--subtitles` adds the camera name and time as a subtitle track to the merged file and/or the timelapse. It needs no re-encoding, and players can show or hide it.

The time shown for each clip starts from that clip's own start time. If the recording has a gap inside a clip, the time after the gap runs early until the next clip begins. If you'd rather the camera draw its name and time into its recordings, turn on its overlay in Protect's camera settings.

## Troubleshooting

- **TLS certificate not trusted:** the console uses a self-signed certificate. Pass `--insecure` or set `PROTECT_VERIFY_SSL=false`.
- **Login failed:** use a local UniFi OS user rather than a Ubiquiti SSO account, and make sure it has access to Protect.
- **Anything else:** run the same command with `--debug`. That prints:
  - the protect-dl, Python and Protect versions;
  - every request made to the console, with its response status;
  - the full error.

  Please include that output when [reporting a problem](https://github.com/alexnj/protect-dl/issues). It never includes your password, session cookie or tokens.

## Exit codes

| Code | Meaning |
|---|---|
| 0 | Success |
| 1 | Error (bad options, login failed, ffmpeg failed…) |
| 3 | The folder is incomplete: some clips are still missing |
| 130 | Interrupted (Ctrl-C). Run the same command again to resume |

## Development

```sh
python3 -m venv .venv && .venv/bin/pip install -e '.[dev]'
.venv/bin/pytest
.venv/bin/ruff check .
```

The tests use a fake console and don't need Protect. The merge and timelapse tests use ffmpeg if it's installed.

## License

[Apache-2.0](LICENSE)
