"""Application-level bandwidth shaping.

A :class:`BandwidthTrace` is a piecewise-constant bandwidth over time. A :class:`TokenBucket`
paces outgoing bytes so the long-run sending rate follows the trace while allowing bursts of
at most ``burst_bytes``. Shaping in the application (instead of the kernel) keeps experiments
reproducible and needs no root privileges; ``scripts/netem.sh`` offers kernel-level shaping
for comparison.

Trace file format: one ``<time_s> <bandwidth_mbps>`` pair per line (whitespace or comma
separated), ``#`` starts a comment. Times must start at 0 and increase strictly; each rate
holds until the next time, and the last rate holds forever.
"""

from __future__ import annotations

import asyncio
import bisect
import math
import re
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Protocol

MBPS = 1_000_000.0


@dataclass(frozen=True, slots=True)
class BandwidthTrace:
    times: tuple[float, ...]
    """Start time (s) of each piece; ``times[0] == 0``."""
    rates_bps: tuple[float, ...]
    """Bandwidth (bit/s) of each piece."""

    def __post_init__(self) -> None:
        if not self.times or len(self.times) != len(self.rates_bps):
            raise ValueError("trace needs equally many times and rates (at least one)")
        if self.times[0] != 0:
            raise ValueError("trace must start at time 0")
        if any(b <= a for a, b in zip(self.times, self.times[1:], strict=False)):
            raise ValueError("trace times must be strictly increasing")
        if any(r < 0 or not math.isfinite(r) for r in self.rates_bps):
            raise ValueError("trace rates must be finite and non-negative")
        if self.rates_bps[-1] <= 0:
            raise ValueError("the last trace rate must be positive")

    @classmethod
    def constant(cls, mbps: float) -> BandwidthTrace:
        return cls((0.0,), (mbps * MBPS,))

    @classmethod
    def parse(cls, text: str) -> BandwidthTrace:
        times, rates = [], []
        for lineno, raw in enumerate(text.splitlines(), 1):
            line = raw.split("#", 1)[0].strip()
            if not line:
                continue
            fields = [f for f in re.split(r"[,\s]+", line) if f]
            if len(fields) != 2:
                raise ValueError(f"trace line {lineno}: expected '<time_s> <mbps>'")
            times.append(float(fields[0]))
            rates.append(float(fields[1]) * MBPS)
        return cls(tuple(times), tuple(rates))

    @classmethod
    def load(cls, path: Path) -> BandwidthTrace:
        return cls.parse(path.read_text())

    @property
    def end_time(self) -> float:
        """Time of the last change."""
        return self.times[-1]

    def rate_at(self, t: float) -> float:
        return self.rates_bps[max(0, bisect.bisect_right(self.times, t) - 1)]

    def bytes_between(self, t0: float, t1: float) -> float:
        """Bytes the link can carry in ``[t0, t1]``."""
        if t1 <= t0:
            return 0.0
        total = 0.0
        i = max(0, bisect.bisect_right(self.times, t0) - 1)
        t = t0
        while t < t1:
            end = self.times[i + 1] if i + 1 < len(self.times) else math.inf
            seg_end = min(end, t1)
            total += self.rates_bps[i] / 8.0 * (seg_end - t)
            t = seg_end
            i += 1
        return total

    def time_to_send(self, t0: float, nbytes: float) -> float:
        """Smallest ``dt`` such that ``bytes_between(t0, t0 + dt) >= nbytes``."""
        if nbytes <= 0:
            return 0.0
        remaining = nbytes
        i = max(0, bisect.bisect_right(self.times, t0) - 1)
        t = t0
        while True:
            rate = self.rates_bps[i] / 8.0
            end = self.times[i + 1] if i + 1 < len(self.times) else math.inf
            capacity = rate * (end - t)
            if capacity >= remaining:
                return t + remaining / rate - t0
            remaining -= capacity
            t = end
            i += 1


class Shaper(Protocol):
    async def consume(self, nbytes: int) -> None: ...


class Unlimited:
    async def consume(self, nbytes: int) -> None:
        return None


class TokenBucket:
    """Token bucket whose fill rate follows a :class:`BandwidthTrace`.

    The trace clock starts at ``start`` (default: creation time). The bucket starts full.
    ``consume`` is safe to call from many tasks; an ``asyncio.Lock`` (FIFO) serializes
    waiters so a shared bucket divides bandwidth fairly between connections.
    """

    def __init__(
        self,
        trace: BandwidthTrace,
        burst_bytes: int,
        *,
        clock: Callable[[], float] = time.monotonic,
        start: float | None = None,
    ) -> None:
        if burst_bytes <= 0:
            raise ValueError("burst_bytes must be positive")
        self._trace = trace
        self._burst = float(burst_bytes)
        self._clock = clock
        self._start = clock() if start is None else start
        self._last = self._start
        self._tokens = self._burst
        self._lock = asyncio.Lock()

    @property
    def trace(self) -> BandwidthTrace:
        return self._trace

    def elapsed(self) -> float:
        return self._clock() - self._start

    def _refill(self, now: float) -> None:
        if now > self._last:
            gained = self._trace.bytes_between(self._last - self._start, now - self._start)
            self._tokens = min(self._burst, self._tokens + gained)
            self._last = now

    async def consume(self, nbytes: int) -> None:
        async with self._lock:
            remaining = float(nbytes)
            while remaining > 0:
                take = min(remaining, self._burst)
                now = self._clock()
                self._refill(now)
                if self._tokens + 1e-9 >= take:
                    self._tokens -= take
                    remaining -= take
                    continue
                deficit = take - self._tokens
                await asyncio.sleep(self._trace.time_to_send(now - self._start, deficit))


ShapingScope = Literal["connection", "global"]


@dataclass(frozen=True, slots=True)
class ShapingConfig:
    """How the server paces responses.

    ``scope="connection"``: every connection gets its own bucket and its own trace clock
    starting at connection time (independent links; reproducible experiments).
    ``scope="global"``: all connections share one bucket whose trace clock starts when the
    server starts (one shared bottleneck; live demos).
    """

    trace: BandwidthTrace | None = None
    scope: ShapingScope = "connection"
    burst_bytes: int = 32 * 1024


class ShaperFactory:
    def __init__(self, config: ShapingConfig) -> None:
        self.config = config
        self._global: TokenBucket | None = None
        if config.trace is not None and config.scope == "global":
            self._global = TokenBucket(config.trace, config.burst_bytes)

    def for_connection(self) -> Shaper:
        if self.config.trace is None:
            return Unlimited()
        if self._global is not None:
            return self._global
        return TokenBucket(self.config.trace, self.config.burst_bytes)
