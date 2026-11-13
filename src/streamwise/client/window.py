"""Sliding-window segment transport (extension E-03).

A segment is split into fixed-size chunks that are requested with ``GET_CHUNK`` over one or
more TCP connections. The receiver keeps a window of ``window`` chunks, ``[base,
base + window)``, where ``base`` is the first chunk not yet delivered in order:

- **Send side.** A chunk may be requested only if it lies inside the window. Retransmissions
  go first, and each request goes to the connection with the fewest requests in flight.
- **Receive side.** A chunk that arrives out of order (possible with several connections) is
  held in the reassembly buffer. When chunk ``base`` arrives, the contiguous run is appended
  to the segment and the window slides forward.
- **Selective repeat.** When a connection fails, only the chunks that were in flight on it
  are requested again, on another connection. The failed connection is re-established in
  the background with exponential backoff. Playback continues as long as one connection
  works.

Memory for out-of-order data is bounded by ``window × chunk_size``, whatever the length of
the stream. The window also bounds the requests in flight, so on one connection the
throughput is at most ``window × chunk_size / RTT``: the classic sliding-window limit.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

from streamwise.client.connection import FetchResult, ServerError, StreamClient
from streamwise.common.protocol import MAX_CHUNK, ProtocolError

log = logging.getLogger("streamwise.client")

TRANSIENT = (ConnectionError, OSError, TimeoutError, ProtocolError, EOFError)
"""Errors that end one connection but not the session."""


@dataclass(frozen=True, slots=True)
class WindowConfig:
    window: int = 16
    """Chunks that may be in flight or waiting for reassembly."""
    chunk_size: int = 64 * 1024
    connections: int = 1
    max_failures: int = 8
    """Consecutive failed (re)connection attempts after which a connection is given up."""
    backoff_s: float = 0.1
    """First reconnection delay; doubles after every failed attempt, up to ``backoff_max_s``."""
    backoff_max_s: float = 5.0

    def __post_init__(self) -> None:
        if self.window < 1:
            raise ValueError("window must be at least 1")
        if not 1 <= self.chunk_size <= MAX_CHUNK:
            raise ValueError(f"chunk size must be between 1 and {MAX_CHUNK} bytes")
        if self.connections < 1:
            raise ValueError("need at least one connection")


@dataclass(slots=True)
class WindowStats:
    chunks: int = 0
    """Chunks delivered (each counted once)."""
    retransmitted_chunks: int = 0
    retransmitted_bytes: int = 0
    connection_failures: int = 0
    reconnects: int = 0
    max_reassembly_chunks: int = 0
    """Peak number of chunks held out of order."""
    max_in_flight: int = 0


@dataclass(slots=True)
class _Slot:
    index: int
    client: StreamClient | None = None
    in_flight: int = 0
    failures: int = 0
    reconnect: asyncio.Task[None] | None = None
    dead: bool = False


@dataclass(slots=True)
class _Request:
    chunk: int
    slot: _Slot


@dataclass(slots=True)
class _Reassembly:
    """Receive-side state of one segment."""

    chunks: int
    base: int = 0
    held: dict[int, bytes] = field(default_factory=dict)
    parts: list[bytes] = field(default_factory=list)

    def accept(self, index: int, data: bytes) -> None:
        if index < self.base or index in self.held:
            return  # duplicate (cannot happen with selective repeat, but harmless)
        self.held[index] = data
        while self.base in self.held:
            self.parts.append(self.held.pop(self.base))
            self.base += 1


class SlidingWindowFetcher:
    """Fetches segments chunk by chunk over a pool of connections."""

    def __init__(
        self,
        connect: Callable[[], Awaitable[StreamClient]],
        config: WindowConfig,
        first: StreamClient | None = None,
    ) -> None:
        self._connect = connect
        self.config = config
        self.stats = WindowStats()
        self._slots = [_Slot(i) for i in range(config.connections)]
        self._slots[0].client = first
        self._changed = asyncio.Event()
        """Set whenever a connection comes up or is given up."""
        self._background: set[asyncio.Task[None]] = set()

    async def start(self) -> None:
        """Open the connections that are not open yet (in the background)."""
        for slot in self._slots:
            if slot.client is None and slot.reconnect is None:
                slot.reconnect = asyncio.create_task(self._reconnect(slot, initial=True))

    async def fetch_segment(self, filename: str, size: int) -> FetchResult:
        cfg = self.config
        chunks = -(-size // cfg.chunk_size)
        state = _Reassembly(chunks)
        next_new = 0
        retransmit: list[int] = []
        pending: dict[asyncio.Task[bytes], _Request] = {}
        request_time = time.monotonic()
        first_byte_time: float | None = None

        def chunk_range(index: int) -> tuple[int, int]:
            offset = index * cfg.chunk_size
            return offset, min(cfg.chunk_size, size - offset)

        try:
            while state.base < chunks:
                # Send side: fill the window.
                while True:
                    if retransmit:
                        index = retransmit[0]
                    elif next_new < chunks and next_new < state.base + cfg.window:
                        index = next_new
                    else:
                        break
                    slot = self._pick_slot()
                    if slot is None:
                        break
                    if retransmit:
                        retransmit.pop(0)
                    else:
                        next_new += 1
                    assert slot.client is not None
                    offset, length = chunk_range(index)
                    task = asyncio.create_task(slot.client.get_chunk(filename, offset, length))
                    pending[task] = _Request(index, slot)
                    slot.in_flight += 1
                self.stats.max_in_flight = max(self.stats.max_in_flight, len(pending))

                if not pending:
                    await self._wait_for_connection()
                    continue

                done, _ = await asyncio.wait(pending, return_when=asyncio.FIRST_COMPLETED)
                for task in done:
                    req = pending.pop(task)
                    req.slot.in_flight -= 1
                    try:
                        data = task.result()
                    except ServerError:
                        raise  # e.g. 404/416: an application error, not a transport failure
                    except TRANSIENT as exc:
                        _, length = chunk_range(req.chunk)
                        retransmit.append(req.chunk)
                        self.stats.retransmitted_chunks += 1
                        self.stats.retransmitted_bytes += length
                        self._connection_failed(req.slot, exc)
                        continue
                    if first_byte_time is None:
                        first_byte_time = time.monotonic()
                    state.accept(req.chunk, data)
                    self.stats.chunks += 1
                retransmit.sort()
                self.stats.max_reassembly_chunks = max(
                    self.stats.max_reassembly_chunks, len(state.held)
                )
        finally:
            for task in pending:
                task.cancel()
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)

        done_time = time.monotonic()
        return FetchResult(
            filename,
            b"".join(state.parts),
            request_time,
            first_byte_time if first_byte_time is not None else done_time,
            done_time,
        )

    async def aclose(self) -> None:
        for slot in self._slots:
            if slot.reconnect is not None:
                slot.reconnect.cancel()
                with contextlib.suppress(BaseException):
                    await slot.reconnect
            if slot.client is not None:
                await slot.client.close(send_bye=not slot.client.broken)
                slot.client = None
        if self._background:
            await asyncio.gather(*self._background, return_exceptions=True)

    # -- connection pool ---------------------------------------------------------------

    def _pick_slot(self) -> _Slot | None:
        for slot in self._slots:
            # A connection can become unusable without a failed request of ours, e.g. when a
            # request was cancelled; replace it as well.
            if slot.client is not None and slot.client.broken and slot.in_flight == 0:
                self._connection_failed(slot, ConnectionError("connection unusable"))
        usable = [s for s in self._slots if s.client is not None and not s.client.broken]
        return min(usable, key=lambda s: s.in_flight) if usable else None

    def _connection_failed(self, slot: _Slot, exc: BaseException) -> None:
        if slot.client is None:
            return  # already being replaced (several requests failed together)
        self.stats.connection_failures += 1
        log.warning(
            "connection %d failed (%s: %s); reconnecting", slot.index, type(exc).__name__, exc
        )
        client, slot.client = slot.client, None
        closing = asyncio.create_task(client.close(send_bye=False))
        self._background.add(closing)
        closing.add_done_callback(self._background.discard)
        slot.reconnect = asyncio.create_task(self._reconnect(slot))

    async def _reconnect(self, slot: _Slot, initial: bool = False) -> None:
        delay = 0.0 if initial else self.config.backoff_s
        try:
            while True:
                if delay:
                    await asyncio.sleep(delay)
                try:
                    slot.client = await self._connect()
                except (*TRANSIENT, ServerError) as exc:
                    slot.failures += 1
                    log.warning("connection %d: reconnect failed (%s)", slot.index, exc)
                    if slot.failures >= self.config.max_failures:
                        slot.dead = True
                        return
                    delay = min(max(delay * 2, self.config.backoff_s), self.config.backoff_max_s)
                    continue
                slot.failures = 0
                if not initial:
                    self.stats.reconnects += 1
                return
        finally:
            slot.reconnect = None
            self._changed.set()

    async def _wait_for_connection(self) -> None:
        while self._pick_slot() is None:
            if all(s.dead for s in self._slots):
                raise ConnectionError(
                    f"all {len(self._slots)} connections failed "
                    f"{self.config.max_failures} times in a row"
                )
            self._changed.clear()
            await self._changed.wait()
