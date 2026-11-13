"""Trace-driven streaming simulator.

The simulator reproduces the real client's behaviour (see ``streamwise.client.playback`` and
``streamwise.client.session``) without real time or sockets, so thousands of sessions run per
second. That makes it possible to train learning-based ABR and to evaluate algorithms on
hundreds of traces. Its rules mirror the real system exactly:

1. Before requesting segment ``k`` the client waits until ``buffer + d_k <= max_buffer``
   (the buffer drains only while playing).
2. The ABR decides on an :class:`AbrState` built exactly like the real client's.
3. Downloading takes the time the link (a :class:`BandwidthTrace`, clock starting at the
   connection) needs for the segment's bytes, plus an optional request latency.
4. While playing, the buffer drains during the download; if it runs dry, the session stalls
   until the segment arrives.
5. Playback starts once ``startup_buffer`` seconds of media are buffered.

``StreamingEnv`` exposes this as a step-wise environment (observation, action = level,
reward = per-segment QoE), and :func:`simulate` runs a complete session for any
:class:`AbrAlgorithm`.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from streamwise.abr.base import AbrAlgorithm, AbrState
from streamwise.analysis.metrics import QoeWeights, SessionMetrics, compute_metrics
from streamwise.common.manifest import Manifest
from streamwise.server.shaping import BandwidthTrace

EPS = 1e-9
PREAMBLE_OVERHEAD_BYTES = 160
"""WELCOME message plus message headers sent before the manifest payload."""


@dataclass(frozen=True, slots=True)
class SimConfig:
    max_buffer_s: float = 30.0
    startup_buffer_s: float = 5.0
    request_latency_s: float = 0.0
    """Fixed per-request delay (RTT, server processing) added to every download. On loopback
    the real overhead is a few milliseconds of timing noise rather than a constant delay, so
    the default is 0 (see the simulator validation in the test report)."""
    burst_bytes: int = 32 * 1024
    """Token-bucket size of the emulated link (the server's ``--burst-kb``). The bucket refills
    while the link is idle, so up to this many bytes of a response leave immediately."""
    weights: QoeWeights = field(default_factory=QoeWeights)


@dataclass(frozen=True, slots=True)
class SimSegment:
    seq: int
    level: int
    bitrate_kbps: int
    bytes: int
    wait_s: float
    request_t: float
    done_t: float
    throughput_bps: float
    buffer_before_s: float
    buffer_after_s: float
    stall_s: float
    reward: float

    @property
    def download_s(self) -> float:
        return self.done_t - self.request_t


@dataclass(slots=True)
class SimResult:
    segments: list[SimSegment]
    startup_delay_s: float
    stalls_s: list[float]
    metrics: SessionMetrics


class StreamingEnv:
    """Step-wise streaming session: ``reset`` -> ``state``; ``step(level)`` -> reward."""

    def __init__(self, manifest: Manifest, config: SimConfig | None = None) -> None:
        self.manifest = manifest
        self.config = config or SimConfig()
        longest = max(manifest.segment_durations)
        if self.config.max_buffer_s < self.config.startup_buffer_s + longest:
            raise ValueError("max buffer must hold the startup buffer plus one segment")
        top = manifest.levels[-1].bitrate_kbps / 1000
        w = self.config.weights
        self._mu = w.rebuffer if w.rebuffer is not None else top
        self._mu_s = w.startup if w.startup is not None else self._mu
        self._smooth = w.smoothness
        self._trace: BandwidthTrace | None = None
        self._count = 0

    # -- episode control ---------------------------------------------------------------

    def reset(self, trace: BandwidthTrace, segments: int | None = None) -> AbrState:
        self._trace = trace
        self._count = min(segments or self.manifest.segment_count, self.manifest.segment_count)
        self.t = 0.0
        self.buffer = 0.0
        self.buffered_media = 0.0
        self.playing = False
        self.seq = 0
        self.last_level: int | None = None
        self.throughputs: list[float] = []
        self.download_times: list[float] = []
        self.startup_delay: float | None = None
        self.stalls: list[float] = []
        self.segments: list[SimSegment] = []
        self._wait = 0.0
        # The handshake and the manifest pass through the same token bucket before the first
        # segment and use up part of its initial burst.
        preamble = len(self.manifest.to_json()) + 1 + PREAMBLE_OVERHEAD_BYTES
        self._tokens = max(0.0, float(self.config.burst_bytes) - preamble)
        self._idle_since = 0.0
        return self.state()

    @property
    def done(self) -> bool:
        return self.seq >= self._count

    def state(self) -> AbrState:
        return AbrState(
            seq=self.seq,
            manifest=self.manifest,
            buffer_s=self.buffer,
            max_buffer_s=self.config.max_buffer_s,
            last_level=self.last_level,
            throughputs_bps=tuple(self.throughputs),
            download_times_s=tuple(self.download_times),
        )

    def quality(self, level: int) -> float:
        return self.manifest.levels[level].bitrate_kbps / 1000

    def step(self, level: int) -> float:
        """Download segment ``self.seq`` at ``level``; return its QoE contribution."""
        if self._trace is None or self.done:
            raise RuntimeError("call reset() first / episode is over")
        if not 0 <= level < len(self.manifest.levels):
            raise ValueError(f"invalid level {level}")
        seq = self.seq
        duration = self.manifest.segment_durations[seq]
        size = self.manifest.segment_size(level, seq)
        buffer_before = self.buffer
        request_t = self.t
        # Tokens accumulated while the link was idle (capped by the bucket size) go out at once.
        idle_gain = self._trace.bytes_between(self._idle_since, self.t)
        tokens = min(float(self.config.burst_bytes), self._tokens + idle_gain)
        paced = max(0.0, size - tokens)
        download = self.config.request_latency_s + self._trace.time_to_send(self.t, paced)

        stall = 0.0
        startup_part = 0.0
        if self.playing:
            if download > self.buffer + EPS:
                stall = download - self.buffer
                self.buffer = 0.0
                self.stalls.append(stall)
            else:
                self.buffer = max(0.0, self.buffer - download)
        else:
            startup_part = download
        self.t += download
        self._tokens = max(0.0, tokens - size)
        self._idle_since = self.t
        self.buffer += duration
        self.buffered_media += duration
        if not self.playing and self.buffered_media + EPS >= self.config.startup_buffer_s:
            self.playing = True
            self.startup_delay = self.t

        q = self.quality(level)
        reward = q - self._mu * stall - self._mu_s * startup_part
        if self.last_level is not None:
            reward -= self._smooth * abs(q - self.quality(self.last_level))

        throughput = size * 8 / max(download, 1e-9)
        self.segments.append(
            SimSegment(
                seq=seq,
                level=level,
                bitrate_kbps=self.manifest.levels[level].bitrate_kbps,
                bytes=size,
                wait_s=self._wait,
                request_t=request_t,
                done_t=self.t,
                throughput_bps=throughput,
                buffer_before_s=buffer_before,
                buffer_after_s=self.buffer,
                stall_s=stall,
                reward=reward,
            )
        )
        self.throughputs.append(throughput)
        self.download_times.append(download)
        self.last_level = level
        self.seq += 1

        # Wait for buffer space before the next request (only drains while playing).
        self._wait = 0.0
        if not self.done:
            excess = self.buffer + self.manifest.segment_durations[self.seq]
            excess -= self.config.max_buffer_s
            if excess > 0 and self.playing:
                self.t += excess
                self.buffer -= excess
                self._wait = excess
        return reward

    def result(self) -> SimResult:
        startup = self.startup_delay if self.startup_delay is not None else self.t
        metrics = compute_metrics(
            [s.bitrate_kbps for s in self.segments],
            [self.manifest.segment_durations[s.seq] for s in self.segments],
            startup,
            self.stalls,
            self.manifest.levels[-1].bitrate_kbps,
            self.config.weights,
        )
        return SimResult(list(self.segments), startup, list(self.stalls), metrics)


def simulate(
    manifest: Manifest,
    trace: BandwidthTrace,
    abr: AbrAlgorithm,
    config: SimConfig | None = None,
    segments: int | None = None,
) -> SimResult:
    """Run one complete session of ``abr`` over ``trace``."""
    env = StreamingEnv(manifest, config)
    state = env.reset(trace, segments)
    abr.reset()
    while not env.done:
        env.step(abr.choose(state))
        state = env.state()
    return env.result()
