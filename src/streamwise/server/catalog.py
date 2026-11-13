"""Catalog of the segmented videos the server can serve."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

from streamwise.common.manifest import Manifest
from streamwise.common.naming import parse_segment_filename

log = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class VideoEntry:
    directory: Path
    manifest: Manifest
    manifest_bytes: bytes


@dataclass(frozen=True, slots=True)
class ResolvedSegment:
    path: Path
    video: str
    filename: str
    seq: int
    level: int
    resolution: str
    bitrate_kbps: int
    size: int


class CatalogError(Exception):
    pass


class MediaCatalog:
    """Videos under ``media_dir/<video>/manifest.json``.

    Every segment listed by a manifest is checked to exist with the advertised size when the
    catalog is loaded, so a request can only fail later if files change underneath the server.
    Segment requests are resolved purely through the manifest: a client-supplied file name is
    parsed and matched against known levels, never joined onto a path directly, so path
    traversal is impossible.
    """

    def __init__(self, videos: dict[str, VideoEntry]) -> None:
        self._videos = videos

    @classmethod
    def load(cls, media_dir: Path) -> MediaCatalog:
        if not media_dir.is_dir():
            raise CatalogError(f"media directory {media_dir} does not exist")
        videos: dict[str, VideoEntry] = {}
        for manifest_path in sorted(media_dir.glob("*/manifest.json")):
            directory = manifest_path.parent
            if directory.name.startswith("."):
                continue
            try:
                raw = manifest_path.read_bytes()
                manifest = Manifest.from_json(raw)
            except (ValueError, KeyError, TypeError) as exc:
                raise CatalogError(f"{manifest_path}: invalid manifest: {exc}") from exc
            if manifest.video != directory.name:
                raise CatalogError(f"{manifest_path}: video name does not match its directory")
            cls._verify_files(directory, manifest)
            videos[manifest.video] = VideoEntry(directory, manifest, raw)
            log.info(
                "loaded %s: %d segments x %d levels",
                manifest.video,
                manifest.segment_count,
                len(manifest.levels),
            )
        if not videos:
            raise CatalogError(f"no videos found in {media_dir}")
        return cls(videos)

    @staticmethod
    def _verify_files(directory: Path, manifest: Manifest) -> None:
        for level in manifest.levels:
            for seq, size in enumerate(level.segment_sizes):
                path = directory / manifest.segment_filename(level.index, seq)
                try:
                    actual = path.stat().st_size
                except FileNotFoundError as exc:
                    raise CatalogError(f"missing segment {path}") from exc
                if actual != size:
                    raise CatalogError(f"{path}: size {actual} != manifest size {size}")

    @property
    def video_names(self) -> list[str]:
        return sorted(self._videos)

    def video(self, name: str) -> VideoEntry | None:
        return self._videos.get(name)

    def resolve(self, filename: str) -> ResolvedSegment | None:
        try:
            seg = parse_segment_filename(filename)
        except ValueError:
            return None
        entry = self._videos.get(seg.video)
        if entry is None:
            return None
        manifest = entry.manifest
        level = manifest.find_level(seg.height, seg.bitrate_kbps)
        if level is None or seg.seq >= manifest.segment_count:
            return None
        canonical = manifest.segment_filename(level.index, seg.seq)
        return ResolvedSegment(
            path=entry.directory / canonical,
            video=seg.video,
            filename=canonical,
            seq=seg.seq,
            level=level.index,
            resolution=level.resolution,
            bitrate_kbps=level.bitrate_kbps,
            size=level.segment_sizes[seg.seq],
        )
