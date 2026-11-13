"""ABR interface shared by all bitrate-selection algorithms."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass

from streamwise.common.manifest import Manifest


@dataclass(frozen=True, slots=True)
class AbrState:
    """Everything an algorithm may use to pick the level of segment ``seq``."""

    seq: int
    """Index of the segment about to be requested."""
    manifest: Manifest
    buffer_s: float
    """Seconds of media buffered ahead of the playhead."""
    max_buffer_s: float
    last_level: int | None
    """Level of the previous segment (``None`` for the first segment)."""
    throughputs_bps: tuple[float, ...]
    """Measured throughput of every previous segment download, oldest first."""
    download_times_s: tuple[float, ...]
    """Download time of every previous segment, oldest first."""

    @property
    def level_count(self) -> int:
        return len(self.manifest.levels)


class AbrAlgorithm(ABC):
    name: str = "abr"

    @abstractmethod
    def choose(self, state: AbrState) -> int:
        """Return the level index (``0 <= level < state.level_count``) for ``state.seq``."""

    def reset(self) -> None:
        """Forget per-session state; called before a new session starts."""
        return None

    def describe(self) -> str:
        return self.name
