"""Live progress display. Uses rich bars on a terminal and periodic plain lines otherwise."""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from rich.console import Console
from rich.progress import (
    BarColumn,
    Progress,
    SpinnerColumn,
    TaskID,
    TextColumn,
    TimeElapsedColumn,
)

from .job import Chunk, Job

STALL_WARN_SECONDS = 15
PLAIN_INTERVAL_SECONDS = 5


def fmt_bytes(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if abs(n) < 1024:
            return f"{n:.1f} {unit}" if unit != "B" else f"{n:.0f} B"
        n /= 1024
    return f"{n:.2f} TB"


def fmt_duration(seconds: float) -> str:
    seconds = int(max(seconds, 0))
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return f"{h}h{m:02d}m" if h else f"{m}m{s:02d}s"


@dataclass
class ChunkState:
    """Byte counters for the chunk in flight; written by the download callback."""

    bytes: int = 0
    total: int = 0
    started: float = field(default_factory=time.monotonic)
    last_byte: float = field(default_factory=time.monotonic)

    def on_bytes(self, n: int, total: int) -> None:
        self.total = total
        if n:
            self.bytes += n
            self.last_byte = time.monotonic()

    @property
    def idle(self) -> float:
        return time.monotonic() - self.last_byte

    @property
    def rate(self) -> float:
        elapsed = self.last_byte - self.started
        return self.bytes / elapsed if elapsed > 0 else 0.0


def _make_progress(console: Console) -> Progress:
    return Progress(
        SpinnerColumn(),
        TextColumn("{task.description}", style="bold"),
        BarColumn(bar_width=30),
        TextColumn("{task.fields[info]}"),
        TimeElapsedColumn(),
        console=console,
        refresh_per_second=4,
    )


class DownloadReporter:
    def __init__(self, console: Console, job: Job):
        self.console = console
        self.job = job
        self.tty = console.is_terminal
        self.run_started = time.monotonic()
        self.run_seconds_done = 0.0  # footage seconds downloaded during this run
        self.chunk: Chunk | None = None
        self.state: ChunkState | None = None
        self.attempt = 1
        self._last_plain = 0.0
        self._progress = _make_progress(console) if self.tty else None
        self._overall: TaskID | None = None
        self._current: TaskID | None = None

    def __enter__(self) -> DownloadReporter:
        if self._progress:
            self._progress.start()
            st = self.job.status()
            self._overall = self._progress.add_task(
                "Overall", total=st.total, completed=st.done, info=self._overall_info()
            )
        return self

    def __exit__(self, *exc) -> None:
        if self._progress:
            self._progress.stop()

    def log(self, message: str) -> None:
        self.console.print(message)

    def _overall_info(self) -> str:
        st = self.job.status()
        info = (
            f"{st.done}/{st.total} chunks · "
            f"{st.done_seconds / 3600:.1f}/{st.total_seconds / 3600:.1f} h footage · "
            f"{fmt_bytes(st.bytes)}"
        )
        elapsed = time.monotonic() - self.run_started
        if self.run_seconds_done > 0 and elapsed > 0:
            remaining = st.total_seconds - st.done_seconds
            eta = remaining / (self.run_seconds_done / elapsed)
            info += f" · ETA {fmt_duration(eta)}"
        return info

    def start_chunk(self, chunk: Chunk, state: ChunkState, attempt: int) -> None:
        self.chunk, self.state, self.attempt = chunk, state, attempt
        desc = chunk.label + (f" (try {attempt})" if attempt > 1 else "")
        if self._progress:
            if self._current is not None:
                self._progress.remove_task(self._current)
            self._current = self._progress.add_task(desc, total=None, info="connecting…")
        else:
            self.log(f"{desc}: requesting export")
            self._last_plain = time.monotonic()

    def _chunk_info(self) -> str:
        s = self.state
        assert s is not None
        info = f"{fmt_bytes(s.bytes)}"
        if s.total:
            info += f" / {fmt_bytes(s.total)}"
        info += f" · {fmt_bytes(s.rate)}/s"
        if s.idle >= STALL_WARN_SECONDS:
            what = "waiting for export to start" if s.bytes == 0 else "waiting for data"
            return f"[yellow]{info} · {what} ({s.idle:.0f}s)[/]"
        if s.bytes == 0:
            return f"connecting… ({s.idle:.0f}s)"
        return info + f" · last byte {s.idle:.0f}s ago"

    def tick(self) -> None:
        """Refresh the current chunk row; called about twice a second."""
        if self.state is None or self.chunk is None:
            return
        if self._progress and self._current is not None:
            s = self.state
            self._progress.update(
                self._current,
                total=s.total or None,
                completed=s.bytes,
                info=self._chunk_info(),
            )
        elif time.monotonic() - self._last_plain >= PLAIN_INTERVAL_SECONDS:
            self._last_plain = time.monotonic()
            self.log(f"{self.chunk.label}: {self._chunk_info()}")

    def finish_chunk(self, chunk: Chunk) -> None:
        self.run_seconds_done += chunk.seconds
        s = self.state
        took = time.monotonic() - s.started if s else 0
        line = f"[green]✓[/] {chunk.label} {fmt_bytes(chunk.bytes)} in {fmt_duration(took)}"
        if self._progress and self._overall is not None:
            self._progress.update(self._overall, advance=1, info=self._overall_info())
            self._progress.console.print(line)
        else:
            self.log(f"{line} · {self._overall_info()}")

    def fail_attempt(self, chunk: Chunk, reason: str, retry_in: float | None) -> None:
        tail = f", retrying in {retry_in:.0f}s" if retry_in is not None else ", giving up for now"
        self.log(f"[yellow]![/] {chunk.label} attempt {self.attempt} failed: {reason}{tail}")


class TaskReporter:
    """Percentage bar for ffmpeg steps."""

    def __init__(self, console: Console, label: str, total_seconds: float):
        self.console = console
        self.label = label
        self.total = max(total_seconds, 1.0)
        self._progress = _make_progress(console) if console.is_terminal else None
        self._task: TaskID | None = None
        self._last_plain = 0.0
        self._last_value = -1.0
        self._last_change = time.monotonic()

    def __enter__(self) -> TaskReporter:
        if self._progress:
            self._progress.start()
            self._task = self._progress.add_task(self.label, total=self.total, info="starting…")
        else:
            self.console.print(f"{self.label}: starting")
        return self

    def __exit__(self, *exc) -> None:
        if self._progress:
            self._progress.stop()

    def update(self, done_seconds: float, speed: str | None = None) -> None:
        now = time.monotonic()
        if done_seconds != self._last_value:
            self._last_value, self._last_change = done_seconds, now
        pct = min(done_seconds / self.total, 1.0) * 100
        info = f"{pct:5.1f}% · {fmt_duration(done_seconds)}/{fmt_duration(self.total)}"
        if speed:
            info += f" · {speed}"
        idle = now - self._last_change
        if idle >= STALL_WARN_SECONDS:
            info += f" · [yellow]no progress for {idle:.0f}s[/]"
        if self._progress and self._task is not None:
            self._progress.update(self._task, completed=min(done_seconds, self.total), info=info)
        elif now - self._last_plain >= PLAIN_INTERVAL_SECONDS:
            self._last_plain = now
            self.console.print(f"{self.label}: {info}")
