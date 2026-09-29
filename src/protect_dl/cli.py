"""Command-line entry point."""

from __future__ import annotations

import argparse
import asyncio
import getpass
import os
import platform
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx
from dotenv import find_dotenv, load_dotenv
from rich.console import Console
from rich.markup import escape
from rich.table import Table

from . import __version__, ffmpeg
from .client import CHANNELS, AuthError, CameraNotFound, ProtectClient, ProtectError
from .downloader import DownloadOptions, download_job
from .job import Job, JobError, default_folder_name, find_jobs, slugify
from .progress import fmt_bytes, fmt_duration
from .timeparse import TimeParseError, get_zone, parse_duration, parse_time

EXIT_OK, EXIT_ERROR, EXIT_INCOMPLETE = 0, 1, 3

console = Console(highlight=False)


class UsageError(Exception):
    pass


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="protect-dl",
        description=(
            "Download UniFi Protect footage for a time range into a resumable job folder, "
            "then optionally merge it or make a timelapse. With only --folder, reports "
            "whether the folder holds a complete download."
        ),
        epilog=(
            'Example: protect-dl --download --camera "Front Door" '
            '--start "2026-09-18 11:33:00 PM" --end "2026-09-21 07:36:00 AM"'
        ),
    )
    p.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    act = p.add_argument_group("actions (combinable; run in order download → merge → timelapse)")
    act.add_argument("--download", action="store_true", help="download (or resume) footage")
    act.add_argument("--merge", action="store_true", help="losslessly join chunks into one MP4")
    act.add_argument("--timelapse", metavar="FACTOR", help="make a timelapse, e.g. 60x")
    act.add_argument("--list-cameras", action="store_true", help="list camera names and exit")
    act.add_argument("--burn-in", action="store_true",
                     help="draw camera name and time into the timelapse video")  # fmt: skip
    act.add_argument("--subtitles", action="store_true",
                     help="add a camera name + time subtitle track to merge/timelapse outputs")  # fmt: skip

    job = p.add_argument_group("job")
    job.add_argument("--folder", type=Path, help="job folder (default: derived from camera and range)")
    job.add_argument("--camera", action="append", default=[], help="camera name (repeatable)")
    job.add_argument("--start", help="start time, e.g. '2026-09-18 11:33:00 PM'")
    job.add_argument("--end", help="end time, e.g. '2026-09-21 07:36:00 AM'")
    job.add_argument("--tz", help="timezone for --start/--end (default: system local)")
    job.add_argument("--chunk", default="60m", help="chunk length (default: 60m)")
    job.add_argument("--channel", choices=list(CHANNELS), default="high",
                     help="stream quality, or the package lens of cameras that have one (default: high)")  # fmt: skip
    job.add_argument("--stall-timeout", type=float, default=60, metavar="SECONDS",
                     help="abort and retry a chunk after this long with no data (default: 60)")  # fmt: skip
    job.add_argument("--retries", type=int, default=3, help="retries per chunk (default: 3)")
    job.add_argument("--force", action="store_true", help="merge/timelapse an incomplete folder")
    job.add_argument("--overwrite", action="store_true", help="overwrite existing merge/timelapse outputs")
    job.add_argument("--debug", action="store_true",
                     help="log console requests, versions and full errors (for bug reports)")  # fmt: skip

    conn = p.add_argument_group(
        "connection (or PROTECT_* variables in the environment, ./.env or ~/.config/protect-dl/config.env)"
    )
    conn.add_argument("--host", help="console address, e.g. 192.168.1.1 [PROTECT_HOST]")
    conn.add_argument("--username", help="local UniFi OS user [PROTECT_USERNAME]")
    conn.add_argument("--password", help="password [PROTECT_PASSWORD]; prompted if unset. Prefer the config file: "
                           "this option shows up in shell history")  # fmt: skip
    conn.add_argument("--insecure", action="store_true",
                      help="skip TLS certificate verification [PROTECT_VERIFY_SSL=false]")  # fmt: skip
    return p


# ---- connection ---------------------------------------------------------------


def make_client(args: argparse.Namespace) -> ProtectClient:
    host = args.host or os.environ.get("PROTECT_HOST")
    username = args.username or os.environ.get("PROTECT_USERNAME")
    if not host or not username:
        raise UsageError("Set --host and --username (or PROTECT_HOST / PROTECT_USERNAME in .env)")
    password = args.password or os.environ.get("PROTECT_PASSWORD")
    if not password:
        password = getpass.getpass(f"Password for {username}@{host}: ")
    verify = not args.insecure and os.environ.get("PROTECT_VERIFY_SSL", "true").lower() not in (
        "0", "false", "no",
    )  # fmt: skip
    log = (lambda msg: console.print(f"[dim]{escape(msg)}[/]")) if args.debug else None
    return ProtectClient(host, username, password, verify_ssl=verify, log=log)


# ---- reporting ----------------------------------------------------------------


def print_status(job: Job) -> bool:
    job.verify()
    st = job.status()
    state = "[bold green]COMPLETE[/]" if st.complete else "[bold yellow]INCOMPLETE[/]"
    if st.complete and st.short:
        state += f" [yellow]({len(st.short)} chunk(s) short, see below)[/]"
    t = Table.grid(padding=(0, 2))
    t.add_row("Folder", str(job.folder))
    t.add_row("Camera", f"{job.camera_name} ({job.channel})")
    t.add_row("Range", f"{job.start:%Y-%m-%d %I:%M:%S %p} → {job.end:%Y-%m-%d %I:%M:%S %p} {job.tz}")
    t.add_row("Chunks", f"{st.done}/{st.total} · {fmt_duration(st.done_seconds)} of "
                        f"{fmt_duration(st.total_seconds)} footage · {fmt_bytes(st.bytes)}")  # fmt: skip
    t.add_row("Status", state)
    for c in st.short:
        t.add_row("Short", f"[yellow]{c.label}: console sent {fmt_bytes(c.bytes)} of "
                           f"{fmt_bytes(c.bytes + c.short_by)}; the end of this hour may be missing[/]")  # fmt: skip
    for s, e in st.missing:
        t.add_row("Missing", f"{s:%Y-%m-%d %H:%M:%S} → {e:%Y-%m-%d %H:%M:%S}")
    outputs = sorted(p.name for p in job.folder.glob("*.mp4") if p.name.startswith(("merged_", "timelapse")))
    for name in outputs:
        t.add_row("Output", name)
    console.print(t)
    if not st.complete:
        console.print(f"Resume with: [bold]protect-dl --download --folder {job.folder}[/]")
    console.print()
    return st.complete


# ---- actions ------------------------------------------------------------------


@dataclass
class RangeSpec:
    tz: ZoneInfo
    start: datetime
    end: datetime
    chunk: timedelta


def parse_range(args: argparse.Namespace) -> RangeSpec:
    """Validate --start/--end/--chunk before connecting to the console."""
    if not (args.start and args.end):
        raise UsageError("--download with --camera needs --start and --end")
    tz = get_zone(args.tz)
    start, end = parse_time(args.start, tz), parse_time(args.end, tz)
    if end <= start:
        raise UsageError("--end must be after --start")
    if end > datetime.now(tz):
        console.print("[yellow]Note: --end is in the future; the last chunk may be short or fail[/]")
    return RangeSpec(tz, start, end, parse_duration(args.chunk))


def resolve_new_jobs(args: argparse.Namespace, spec: RangeSpec, client: ProtectClient) -> list[Job]:
    tz, start, end = spec.tz, spec.start, spec.end
    jobs = []
    for name in args.camera:
        cam = client.find_camera(name)
        if not cam.supports(args.channel):
            raise UsageError(f"{cam.name} has no {args.channel} channel ({cam.channel_summary() or 'none listed'})")
        if args.folder and len(args.camera) == 1:
            folder = args.folder
        elif args.folder:
            folder = args.folder / slugify(cam.name)
        else:
            folder = Path.cwd() / default_folder_name(cam.name, start.astimezone(tz), end.astimezone(tz))
        jobs.append(
            Job.create(folder, camera_id=cam.id, camera_name=cam.name, channel=args.channel,
                       tz=tz, start=start, end=end, chunk=spec.chunk)  # fmt: skip
        )
    return jobs


def load_jobs(folder: Path | None) -> list[Job]:
    if folder is None:
        raise UsageError("Specify --folder (or --camera/--start/--end with --download)")
    paths = find_jobs(folder)
    if not paths:
        raise UsageError(f"No job found in {folder} (expected a manifest.json there or in a subfolder)")
    return [Job.load(p) for p in paths]


async def run_online(
    args: argparse.Namespace, spec: RangeSpec | None, jobs: list[Job]
) -> tuple[int, list[Job]]:
    async with make_client(args) as client:
        if args.list_cameras:
            t = Table("Name", "Model", "State", "Channels", "ID")
            for cam in client.cameras():
                t.add_row(cam.name, cam.model, cam.state, cam.channel_summary(), cam.id)
            console.print(t)
            if not args.download:
                return EXIT_OK, []

        if spec is not None:
            jobs = resolve_new_jobs(args, spec, client)
        opts = DownloadOptions(stall_timeout=args.stall_timeout, retries=args.retries)
        complete = True
        for job in jobs:
            client.camera_by_id(job.camera_id)
            args.active_folder = job.folder
            console.rule(f"{job.camera_name} → {job.folder}")
            complete &= await download_job(client, job, opts, console)
            print_status(job)
        return (EXIT_OK if complete else EXIT_INCOMPLETE), jobs


def postprocess(args: argparse.Namespace, jobs: list[Job], factor: float | None) -> int:
    code = EXIT_OK
    for job in jobs:
        job.verify()
        st = job.status()
        if not st.complete and not args.force:
            console.print(f"[red]{job.folder} is incomplete ({st.done}/{st.total} chunks); "
                          "finish the download first or pass --force[/]")  # fmt: skip
            code = EXIT_INCOMPLETE
            continue
        if not st.complete:
            console.print(f"[yellow]--force: processing {st.done}/{st.total} chunks; output will have gaps[/]")
        if args.merge:
            out = ffmpeg.merge(job, console, args.overwrite, subtitles=args.subtitles)
            if out:
                console.print(f"[green]✓[/] Merged: {out} ({fmt_bytes(out.stat().st_size)})")
        if factor:
            out = ffmpeg.timelapse(job, factor, console, args.overwrite,
                                   burn_in=args.burn_in, subtitles=args.subtitles)  # fmt: skip
            if out:
                console.print(f"[green]✓[/] Timelapse: {out} ({fmt_bytes(out.stat().st_size)})")
    return code


def run(args: argparse.Namespace) -> int:
    factor = ffmpeg.parse_factor(args.timelapse) if args.timelapse else None
    if args.burn_in and not factor:
        raise UsageError("--burn-in applies to --timelapse")
    if args.subtitles and not (args.merge or factor):
        raise UsageError("--subtitles applies to --merge and/or --timelapse")
    if args.merge or factor:
        ffmpeg.require_ffmpeg()  # fail before a long download, not after it
    if args.camera and not args.download:
        raise UsageError("--camera is only used with --download (use --folder for existing jobs)")

    jobs: list[Job] = []
    spec = None
    if args.download:  # validate local input before logging in
        if args.camera:
            spec = parse_range(args)
        else:
            jobs = load_jobs(args.folder)

    code = EXIT_OK
    if args.download or args.list_cameras:
        code, jobs = asyncio.run(run_online(args, spec, jobs))
        if args.list_cameras and not args.download:
            return code

    if args.merge or factor:
        if not args.download:
            jobs = load_jobs(args.folder)
        code = max(code, postprocess(args, jobs, factor))
    elif not args.download:
        if args.folder is None:
            build_parser().print_help()
            return EXIT_ERROR
        complete = [print_status(j) for j in load_jobs(args.folder)]
        code = EXIT_OK if all(complete) else EXIT_INCOMPLETE
    return code


def config_file() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config"
    return Path(base) / "protect-dl" / "config.env"


def load_settings() -> None:
    """Environment variables win, then ./.env (or a parent's), then the user config file."""
    load_dotenv(find_dotenv(usecwd=True))
    load_dotenv(config_file())


def fail(args: argparse.Namespace, message: str) -> None:
    console.print(message)
    if args.debug:
        console.print_exception()  # no local variables, so no credentials
    sys.exit(EXIT_ERROR)


def main(argv: list[str] | None = None) -> None:
    load_settings()
    args = build_parser().parse_args(argv)
    if args.debug:
        console.print(
            f"[dim]protect-dl {__version__} · Python {platform.python_version()} · "
            f"{platform.platform()} · httpx {httpx.__version__}[/]"
        )
    try:
        sys.exit(run(args))
    except KeyboardInterrupt:
        console.print("\n[yellow]Interrupted.[/] Finished chunks are saved; the interrupted chunk "
                      "will be re-downloaded from its start.")  # fmt: skip
        folder = getattr(args, "active_folder", None)
        if folder:
            console.print(f"Resume with: [bold]protect-dl --download --folder {folder}[/]")
        sys.exit(130)
    except (UsageError, TimeParseError, JobError, CameraNotFound, ffmpeg.FfmpegError, ValueError) as e:
        fail(args, f"[red]Error:[/] {escape(str(e))}")
    except AuthError as e:
        fail(args, f"[red]Login failed:[/] {escape(str(e))}. Use a local (non-SSO) UniFi OS account.")
    except (ProtectError, httpx.HTTPError, OSError) as e:
        if "CERTIFICATE_VERIFY_FAILED" in str(e):
            fail(args, "[red]TLS certificate not trusted[/] (UniFi consoles use a self-signed certificate).\n"
                       "Pass --insecure, or set PROTECT_VERIFY_SSL=false in .env.")  # fmt: skip
        elif isinstance(e, httpx.TransportError):
            try:
                where = e.request.url.netloc.decode()
            except RuntimeError:
                where = "the console"
            fail(args, f"[red]Cannot reach {where}:[/] {escape(str(e) or type(e).__name__)}")
        else:
            fail(args, f"[red]Error:[/] {type(e).__name__}: {escape(str(e))}")
    except Exception as e:
        fail(args, f"[red]Unexpected error:[/] {type(e).__name__}: {escape(str(e))}\n"
                   "Please report it at https://github.com/alexnj/protect-dl/issues "
                   "with the output of the same command plus --debug.")  # fmt: skip


if __name__ == "__main__":
    main()
