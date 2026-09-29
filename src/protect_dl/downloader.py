"""Download loop: resume, per-chunk retries and the stall watchdog."""

from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass

import httpx
from rich.console import Console

from . import ffmpeg
from .client import AuthError, ExportTruncated, ProtectClient, ProtectError
from .job import Chunk, Job
from .progress import ChunkState, DownloadReporter, fmt_bytes

TICK_SECONDS = 0.5


class StallError(Exception):
    pass


@dataclass
class DownloadOptions:
    stall_timeout: float = 60
    retries: int = 3
    retry_delay: float = 2  # doubles on each attempt, capped at 30 s


async def download_job(
    client: ProtectClient, job: Job, opts: DownloadOptions, console: Console
) -> bool:
    """Download every pending chunk of ``job``. Returns True if the job is complete."""
    for p in job.cleanup_partials():
        console.print(f"[dim]Removed interrupted partial {p.name}; it will be re-downloaded[/]")
    for c in job.verify():
        console.print(f"[yellow]{c.file} is missing or truncated; it will be re-downloaded[/]")

    pending = job.pending()
    if not pending:
        return True
    if len(pending) < len(job.chunks):
        console.print(
            f"Resuming: {len(job.chunks) - len(pending)} of {len(job.chunks)} chunks "
            f"already downloaded, starting at {pending[0].label}"
        )

    with DownloadReporter(console, job) as reporter:
        for chunk in pending:
            await _download_chunk(client, job, chunk, opts, reporter)
    return not job.pending()


async def _download_chunk(
    client: ProtectClient,
    job: Job,
    chunk: Chunk,
    opts: DownloadOptions,
    reporter: DownloadReporter,
) -> bool:
    part = job.part_path(chunk)
    cut_off_at: int | None = None  # where the previous attempt's response ended early
    for attempt in range(1, opts.retries + 2):
        state = ChunkState()
        reporter.start_chunk(chunk, state, attempt)
        task = asyncio.create_task(
            client.download_chunk(
                job.camera_id, chunk.start, chunk.end, job.channel, part, state.on_bytes
            )
        )
        try:
            while True:
                done, _ = await asyncio.wait({task}, timeout=TICK_SECONDS)
                if done:
                    size = task.result()
                    break
                reporter.tick()
                if state.idle > opts.stall_timeout:
                    raise StallError(f"no data for {state.idle:.0f}s")
            if size == 0:
                raise ProtectError("console returned an empty export")
            os.replace(part, job.path(chunk))
            job.mark_done(chunk, size)
            reporter.finish_chunk(chunk)
            return True
        except AuthError as e:
            reason = f"session expired ({e}); logging in again"
            try:
                await client.relogin()
            except (ProtectError, httpx.HTTPError) as login_error:
                reason = f"login failed: {login_error}"
        except ExportTruncated as e:
            reason = str(e) or type(e).__name__
            short = state.total > state.bytes > 0
            if short and state.bytes == cut_off_at:
                # Same cut-off twice: the console cannot produce the rest of this range
                # (typically damaged recording segments), so retrying will never succeed.
                # Keep what it sent if it plays.
                playable = ffmpeg.is_playable(part)
                if playable is not False:
                    os.replace(part, job.path(chunk))
                    job.mark_done(chunk, state.bytes, short_by=state.total - state.bytes)
                    reporter.finish_chunk(chunk)
                    reporter.log(
                        f"[yellow]  {chunk.label}: console repeatedly ends this export "
                        f"{fmt_bytes(state.total - state.bytes)} short; kept the "
                        f"{fmt_bytes(state.bytes)} it sent"
                        + (" (ffprobe not found, playback not verified)" if playable is None else "")
                        + "[/]"
                    )
                    return True
                reason += "; the partial file is not playable"
            cut_off_at = state.bytes if short else None
        except (StallError, ProtectError, httpx.HTTPError, OSError, TimeoutError) as e:
            reason = str(e) or type(e).__name__
        finally:
            # Also runs on Ctrl-C (cancellation): stop the transfer and drop the partial file.
            if not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
            part.unlink(missing_ok=True)

        retry_in = min(opts.retry_delay * 2 ** (attempt - 1), 30) if attempt <= opts.retries else None
        reporter.fail_attempt(chunk, reason, retry_in)
        if retry_in is None:
            return False
        await asyncio.sleep(retry_in)
    return False
