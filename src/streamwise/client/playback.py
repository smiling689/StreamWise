"""Playback buffer model.

The model is the client's authoritative playback clock. Media time advances in real time while
playing; the buffer level is the media time downloaded but not yet played. Playback starts once
``startup_buffer_s`` of media is buffered (time until then is the *startup delay*). If the
playhead reaches the end of the buffered media before the video ends, playback *stalls*
(rebuffers) until the next segment arrives. Segments are downloaded in order.

All timestamps come from ``clock`` (``time.monotonic`` by default) and stall boundaries use the
exact moments the playhead ran dry and the segment arrived, not the time the task woke up.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from enum import Enum


class PlayerState(Enum):
    STARTUP = "startup"
    PLAYING = "playing"
    STALLED = "stalled"
    ENDED = "ended"


@dataclass(frozen=True, slots=True)
class Stall:
    seq: int
    """Segment the player was waiting for."""
    start: float
    end: float

    @property
    def duration(self) -> float:
        return self.end - self.start


class PlaybackModel:
    def __init__(
        self,
        durations: Sequence[float],
        startup_buffer_s: float,
        *,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if not durations:
            raise ValueError("no segments to play")
        self._durations = list(durations)
        self._total = sum(self._durations)
        self._startup_buffer = min(startup_buffer_s, self._total)
        self._clock = clock
        self.created_at = clock()

        self._downloaded = 0
        self._arrivals: list[float] = []
        self._buffered_until = 0.0
        self._played = 0.0
        self._playing_since: float | None = None
        self.state = PlayerState.STARTUP
        self.playback_started_at: float | None = None
        self.ended_at: float | None = None
        self.stalls: list[Stall] = []
        self._stall_start: float | None = None
        self._changed = asyncio.Event()

    # -- producer side -----------------------------------------------------------------

    def segment_downloaded(self, seq: int) -> None:
        if seq != self._downloaded:
            raise ValueError(f"segment {seq} arrived out of order (expected {self._downloaded})")
        self._arrivals.append(self._clock())
        self._downloaded += 1
        self._buffered_until += self._durations[seq]
        self._changed.set()

    # -- queries -----------------------------------------------------------------------

    @property
    def segment_count(self) -> int:
        return len(self._durations)

    @property
    def downloaded(self) -> int:
        return self._downloaded

    def position(self, now: float | None = None) -> float:
        """Media time of the playhead."""
        if self.state is PlayerState.PLAYING and self._playing_since is not None:
            now = self._clock() if now is None else now
            return min(self._played + max(0.0, now - self._playing_since), self._buffered_until)
        return self._played

    def buffer_level(self, now: float | None = None) -> float:
        return max(0.0, self._buffered_until - self.position(now))

    @property
    def startup_delay(self) -> float | None:
        if self.playback_started_at is None:
            return None
        return self.playback_started_at - self.created_at

    @property
    def stall_time(self) -> float:
        return sum(s.duration for s in self.stalls)

    def current_stall(self, now: float | None = None) -> float:
        if self._stall_start is None:
            return 0.0
        return (self._clock() if now is None else now) - self._stall_start

    async def wait_for_buffer_below(self, threshold_s: float) -> None:
        """Wait until the buffer level is at most ``threshold_s``.

        The buffer only drains while playing; in other states we wait for a state change.
        """
        while True:
            excess = self.buffer_level() - threshold_s
            if excess <= 1e-9 or self.state is PlayerState.ENDED:
                return
            if self.state is PlayerState.PLAYING:
                await asyncio.sleep(excess)
            else:
                await self._wait_change()

    # -- playback loop -----------------------------------------------------------------

    async def _wait_change(self) -> None:
        self._changed.clear()
        await self._changed.wait()

    async def _sleep_until(self, deadline: float) -> None:
        while (remaining := deadline - self._clock()) > 0:
            await asyncio.sleep(remaining)

    async def run(
        self,
        on_segment_start: Callable[[int], None] | None = None,
        on_segment_played: Callable[[int], None] | None = None,
        on_stall: Callable[[int, bool], None] | None = None,
    ) -> None:
        """Play every segment in order; returns when the last segment has been played.

        ``on_stall(seq, started)`` is called when a stall starts (``True``) and ends.
        """
        called_at = self._clock()
        while self._buffered_until + 1e-9 < self._startup_buffer:
            await self._wait_change()
        # Playback starts the moment the segment completing the startup buffer arrived.
        start = max(called_at, self._arrivals[-1]) if self._arrivals else called_at
        self.playback_started_at = start
        next_start = start

        for seq, duration in enumerate(self._durations):
            if seq >= self._downloaded:
                self.state = PlayerState.STALLED
                self._stall_start = next_start
                self._changed.set()
                if on_stall:
                    on_stall(seq, True)
                while seq >= self._downloaded:
                    await self._wait_change()
                arrival = self._arrivals[seq]
                self.stalls.append(Stall(seq, next_start, arrival))
                self._stall_start = None
                if on_stall:
                    on_stall(seq, False)
                next_start = arrival

            self.state = PlayerState.PLAYING
            self._playing_since = next_start
            self._changed.set()
            if on_segment_start:
                on_segment_start(seq)
            deadline = next_start + duration
            await self._sleep_until(deadline)
            self._played += duration
            self._playing_since = deadline
            if on_segment_played:
                on_segment_played(seq)
            next_start = deadline

        self.state = PlayerState.ENDED
        self.ended_at = next_start
        self._playing_since = None
        self._changed.set()
