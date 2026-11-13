"""Media player integration.

Segments are MPEG-TS, which is designed to be concatenated, so the player receives one
continuous TS byte stream on its standard input. ``ffplay`` (from FFmpeg) is used because it
ships with the conda environment. Writing to the pipe blocks once ffplay's internal queues are
full, so the player is fed at its own playback pace.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import shutil
from typing import Protocol

log = logging.getLogger(__name__)


class Player(Protocol):
    @property
    def alive(self) -> bool: ...

    async def start(self) -> None: ...

    async def feed(self, data: bytes) -> None: ...

    async def close(self, timeout: float) -> None: ...


class NullPlayer:
    """Headless mode: no video output."""

    alive = False

    async def start(self) -> None:
        return None

    async def feed(self, data: bytes) -> None:
        return None

    async def close(self, timeout: float) -> None:
        return None


class FfplayPlayer:
    def __init__(
        self,
        title: str,
        *,
        mute: bool = False,
        window_size: tuple[int, int] | None = (640, 360),
        window_pos: tuple[int, int] | None = None,
        executable: str = "ffplay",
    ) -> None:
        self.title = title
        self.mute = mute
        self.window_size = window_size
        self.window_pos = window_pos
        self.executable = executable
        self._proc: asyncio.subprocess.Process | None = None
        self._broken = False

    @property
    def alive(self) -> bool:
        return self._proc is not None and self._proc.returncode is None and not self._broken

    def command(self) -> list[str]:
        exe = shutil.which(self.executable)
        if exe is None:
            raise FileNotFoundError(f"{self.executable} not found on PATH")
        args = [
            exe, "-hide_banner", "-loglevel", "error", "-nostats", "-autoexit",
            "-window_title", self.title,
            # Segment 0 alone is enough to probe the stream, so playback can start right away.
            "-probesize", "262144", "-analyzeduration", "1000000",
        ]  # fmt: skip
        if self.mute:
            args.append("-an")
        if self.window_size:
            args += ["-x", str(self.window_size[0]), "-y", str(self.window_size[1])]
        if self.window_pos:
            args += ["-left", str(self.window_pos[0]), "-top", str(self.window_pos[1])]
        return [*args, "-i", "pipe:0"]

    async def start(self) -> None:
        self._proc = await asyncio.create_subprocess_exec(
            *self.command(),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.DEVNULL,
        )

    async def feed(self, data: bytes) -> None:
        if not self.alive or self._proc is None or self._proc.stdin is None:
            return
        try:
            self._proc.stdin.write(data)
            await self._proc.stdin.drain()
        except (BrokenPipeError, ConnectionResetError):
            self._broken = True
            log.warning("player window closed; continuing without video output")

    async def close(self, timeout: float) -> None:
        """Signal end of stream and let the player finish what it has buffered."""
        proc = self._proc
        if proc is None:
            return
        if proc.stdin is not None and not proc.stdin.is_closing():
            proc.stdin.close()
            with contextlib.suppress(BrokenPipeError, ConnectionResetError):
                await proc.stdin.wait_closed()
        try:
            await asyncio.wait_for(proc.wait(), timeout)
        except TimeoutError:
            proc.terminate()
            try:
                await asyncio.wait_for(proc.wait(), 3)
            except TimeoutError:
                proc.kill()
                await proc.wait()
