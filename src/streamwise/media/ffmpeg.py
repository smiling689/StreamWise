"""Thin wrappers around the ``ffmpeg`` and ``ffprobe`` command-line tools."""

from __future__ import annotations

import json
import shutil
import subprocess
from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path


class FfmpegError(RuntimeError):
    pass


def require_tool(name: str) -> str:
    path = shutil.which(name)
    if path is None:
        raise FfmpegError(f"{name} not found on PATH; activate the streamwise conda environment")
    return path


def run(args: list[str]) -> subprocess.CompletedProcess[str]:
    """Run a command, raising :class:`FfmpegError` with its stderr on failure."""
    proc = subprocess.run(args, capture_output=True, text=True, check=False)
    if proc.returncode != 0:
        tail = "\n".join(proc.stderr.strip().splitlines()[-20:])
        raise FfmpegError(f"command failed ({proc.returncode}): {' '.join(args)}\n{tail}")
    return proc


@dataclass(frozen=True, slots=True)
class SourceInfo:
    path: Path
    duration: float
    width: int
    height: int
    frame_rate: Fraction
    has_audio: bool


def probe(path: Path) -> SourceInfo:
    proc = run(
        [
            require_tool("ffprobe"),
            "-v", "error",
            "-show_entries", "format=duration:stream=codec_type,width,height,avg_frame_rate",
            "-of", "json",
            str(path),
        ]
    )  # fmt: skip
    data = json.loads(proc.stdout)
    streams = data.get("streams", [])
    video = next((s for s in streams if s.get("codec_type") == "video"), None)
    if video is None:
        raise FfmpegError(f"{path} has no video stream")
    rate = Fraction(video.get("avg_frame_rate") or "0/1")
    if rate <= 0:
        raise FfmpegError(f"{path}: cannot determine frame rate")
    return SourceInfo(
        path=path,
        duration=float(data["format"]["duration"]),
        width=int(video["width"]),
        height=int(video["height"]),
        frame_rate=rate,
        has_audio=any(s.get("codec_type") == "audio" for s in streams),
    )
