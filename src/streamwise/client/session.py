"""One streaming session: download loop, playback clock and player feeder.

Three concurrent tasks share a :class:`PlaybackModel`:

* **downloader** — waits for buffer space, asks the ABR for a level, fetches the segment over
  TCP, stores it in the cache and hands it to the model and the feeder;
* **playback** — the model's real-time clock; it plays segments, detects stalls and releases
  each segment from the cache once played;
* **feeder** — streams downloaded segments into the media player in order.

If any task fails the others are cancelled (``asyncio.TaskGroup``) and the cache, player,
connection and log are always cleaned up.
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path

from streamwise.abr import AbrAlgorithm, AbrState
from streamwise.analysis.metrics import SessionMetrics, compute_metrics
from streamwise.client.cache import SegmentCache
from streamwise.client.clientlog import ClientLog, SegmentRecord
from streamwise.client.connection import ServerError, StreamClient
from streamwise.client.playback import PlaybackModel, Stall
from streamwise.client.player import NullPlayer, Player
from streamwise.common.manifest import Manifest
from streamwise.common.protocol import ProtocolError

log = logging.getLogger("streamwise.client")

PLAYBACK = "playback"
PLAYER = "player"


@dataclass(frozen=True, slots=True)
class ClientConfig:
    host: str = "127.0.0.1"
    port: int = 9000
    video: str = "bbb"
    name: str = "client"
    max_buffer_s: float = 30.0
    startup_buffer_s: float = 5.0
    max_segments: int | None = None
    """Only play the first N segments (``None``: whole video)."""
    download_dir: Path = Path("download")
    log_db: Path | None = Path("logs/client.db")
    connect_timeout: float = 10.0
    stall_timeout: float = 30.0
    player_close_timeout: float = 15.0

    def validate(self, manifest: Manifest) -> None:
        longest = max(manifest.segment_durations)
        if self.startup_buffer_s <= 0:
            raise ValueError("startup buffer must be positive")
        if self.max_buffer_s < self.startup_buffer_s + longest:
            raise ValueError(
                f"max buffer ({self.max_buffer_s}s) must be at least the startup buffer plus "
                f"one segment ({self.startup_buffer_s + longest}s)"
            )


@dataclass(slots=True)
class SessionResult:
    session_id: str
    status: str
    error: str | None = None
    manifest: Manifest | None = None
    segments: list[SegmentRecord] = field(default_factory=list)
    stalls: list[Stall] = field(default_factory=list)
    metrics: SessionMetrics | None = None


class StreamingSession:
    def __init__(
        self,
        config: ClientConfig,
        abr: AbrAlgorithm,
        player: Player | None = None,
    ) -> None:
        self.config = config
        self.abr = abr
        self.player: Player = player or NullPlayer()
        self.session_id = f"{config.name}-{uuid.uuid4().hex[:8]}"
        self.result = SessionResult(self.session_id, "running")
        self._origin = time.monotonic()

    def _rel(self, t: float) -> float:
        return t - self._origin

    async def run(self) -> SessionResult:
        cfg = self.config
        clog = ClientLog(cfg.log_db) if cfg.log_db else None
        started_wall = time.time()
        if clog:
            clog.session_started(
                self.session_id,
                cfg.name,
                cfg.video,
                self.abr.describe(),
                f"{cfg.host}:{cfg.port}",
                started_wall,
                {k: str(v) if isinstance(v, Path) else v for k, v in asdict(cfg).items()},
            )
        client: StreamClient | None = None
        cache: SegmentCache | None = None
        model: PlaybackModel | None = None
        try:
            try:
                client = await StreamClient.connect(
                    cfg.host,
                    cfg.port,
                    cfg.name,
                    connect_timeout=cfg.connect_timeout,
                    stall_timeout=cfg.stall_timeout,
                )
                if clog:
                    clog.server_session(self.session_id, client.session_id)
                manifest = await client.get_manifest(cfg.video)
                self.result.manifest = manifest
                cfg.validate(manifest)
                count = manifest.segment_count
                if cfg.max_segments is not None:
                    count = max(1, min(count, cfg.max_segments))
                durations = manifest.segment_durations[:count]
                log.info(
                    "[%s] %s: %d segments, %d levels, ABR %s",
                    cfg.name,
                    manifest.video,
                    count,
                    len(manifest.levels),
                    self.abr.describe(),
                )

                await self.player.start()
                consumers = frozenset({PLAYBACK, PLAYER} if self.player.alive else {PLAYBACK})
                cache = SegmentCache(cfg.download_dir, self.session_id, consumers)
                self.abr.reset()
                self._origin = time.monotonic()
                model = PlaybackModel(durations, cfg.startup_buffer_s, clock=time.monotonic)
                model.created_at = self._origin
                feed_queue: asyncio.Queue[int] = asyncio.Queue()

                def on_played(seq: int) -> None:
                    assert cache is not None
                    cache.release(seq, PLAYBACK)

                def on_stall(seq: int, started: bool) -> None:
                    if started:
                        log.warning("[%s] STALL waiting for segment %d", cfg.name, seq)
                    else:
                        stall = model.stalls[-1]
                        log.warning("[%s] resumed after %.2fs stall", cfg.name, stall.duration)
                        if clog:
                            clog.stall(self.session_id, stall, self._origin)

                async with asyncio.TaskGroup() as tg:
                    tg.create_task(model.run(on_segment_played=on_played, on_stall=on_stall))
                    tg.create_task(self._feed(cache, feed_queue, count))
                    tg.create_task(
                        self._download(client, manifest, model, cache, feed_queue, count, clog)
                    )

                self.result.status = "completed"
            except* (OSError, TimeoutError, ProtocolError, ServerError, ValueError) as group:
                self.result.status = "failed"
                self.result.error = "; ".join(f"{type(e).__name__}: {e}" for e in group.exceptions)
                log.error("[%s] session failed: %s", cfg.name, self.result.error)
        except asyncio.CancelledError:
            self.result.status = "interrupted"
            raise
        finally:
            if model is not None:
                self.result.stalls = list(model.stalls)
                self.result.metrics = self._metrics(model)
            await self.player.close(
                cfg.player_close_timeout if self.result.status == "completed" else 0.5
            )
            if cache is not None:
                await cache.aclose()
            if client is not None:
                await client.close()
            if clog:
                clog.session_finished(
                    self.session_id,
                    time.time(),
                    self.result.status,
                    self.result.error,
                    self.result.metrics,
                )
                clog.close()
        return self.result

    def _metrics(self, model: PlaybackModel) -> SessionMetrics | None:
        manifest = self.result.manifest
        if manifest is None or model.startup_delay is None:
            return None
        played = self.result.segments[: model.downloaded]
        if model.ended_at is None:  # interrupted: only count what was fully played
            played = played[: int(model.position() // manifest.segment_duration)]
        return compute_metrics(
            [r.bitrate_kbps for r in played],
            [manifest.segment_durations[r.seq] for r in played],
            model.startup_delay,
            [s.duration for s in model.stalls],
            manifest.levels[-1].bitrate_kbps,
        )

    async def _download(
        self,
        client: StreamClient,
        manifest: Manifest,
        model: PlaybackModel,
        cache: SegmentCache,
        feed_queue: asyncio.Queue[int],
        count: int,
        clog: ClientLog | None,
    ) -> None:
        cfg = self.config
        throughputs: list[float] = []
        download_times: list[float] = []
        last_level: int | None = None
        for seq in range(count):
            wait_started = time.monotonic()
            await model.wait_for_buffer_below(cfg.max_buffer_s - manifest.segment_durations[seq])
            waited = time.monotonic() - wait_started
            buffer_before = model.buffer_level()
            level = self.abr.choose(
                AbrState(
                    seq=seq,
                    manifest=manifest,
                    buffer_s=buffer_before,
                    max_buffer_s=cfg.max_buffer_s,
                    last_level=last_level,
                    throughputs_bps=tuple(throughputs),
                    download_times_s=tuple(download_times),
                )
            )
            if not 0 <= level < len(manifest.levels):
                raise ValueError(f"ABR returned invalid level {level}")
            filename = manifest.segment_filename(level, seq)
            fetched = await client.fetch_segment(filename)
            expected = manifest.segment_size(level, seq)
            if len(fetched.data) != expected:
                raise ProtocolError(
                    f"{filename}: got {len(fetched.data)} bytes, expected {expected}"
                )
            await cache.store(seq, filename, fetched.data)
            model.segment_downloaded(seq)
            feed_queue.put_nowait(seq)

            lv = manifest.levels[level]
            record = SegmentRecord(
                seq=seq,
                level=level,
                resolution=lv.resolution,
                bitrate_kbps=lv.bitrate_kbps,
                filename=filename,
                bytes=len(fetched.data),
                wait_s=waited,
                request_t=self._rel(fetched.request_time),
                first_byte_t=self._rel(fetched.first_byte_time),
                done_t=self._rel(fetched.done_time),
                throughput_bps=fetched.throughput_bps,
                buffer_before_s=buffer_before,
                buffer_after_s=model.buffer_level(),
            )
            self.result.segments.append(record)
            if clog:
                clog.segment(self.session_id, record)
            throughputs.append(fetched.throughput_bps)
            download_times.append(fetched.download_time)
            switch = (
                "" if last_level in (None, level) else (" UP" if level > last_level else " DOWN")
            )
            last_level = level
            log.info(
                "[%s] seg %3d/%d  %-12s %6.2f Mbps  %5.2fs  buffer %5.1fs  stalls %d (%.1fs)%s",
                cfg.name,
                seq + 1,
                count,
                lv.label,
                fetched.throughput_bps / 1e6,
                fetched.download_time,
                record.buffer_after_s,
                len(model.stalls),
                model.stall_time,
                switch,
            )

    async def _feed(self, cache: SegmentCache, feed_queue: asyncio.Queue[int], count: int) -> None:
        for _ in range(count):
            seq = await feed_queue.get()
            if self.player.alive:
                data = await asyncio.to_thread(cache.path(seq).read_bytes)
                await self.player.feed(data)
            cache.release(seq, PLAYER)
