"""Local segment cache in the ``download`` directory.

Each client session stores its segments in its own sub-directory (``download/<session>/``) so
concurrent clients on one machine never overwrite each other's files. A segment is deleted as
soon as every registered consumer (the playback clock and, if present, the media player) has
released it, and the whole session directory is removed when the session ends, whether it
finished normally or not.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import shutil
from pathlib import Path

log = logging.getLogger(__name__)


class SegmentCache:
    def __init__(self, root: Path, session_name: str, consumers: frozenset[str]) -> None:
        if not consumers:
            raise ValueError("at least one consumer is required")
        self.root = root
        self.directory = root / session_name
        self._consumers = consumers
        self._pending: dict[int, set[str]] = {}
        self._paths: dict[int, Path] = {}
        self.stored = 0
        self.deleted = 0
        self.peak_files = 0
        self._inflight: set[asyncio.Future[Path]] = set()
        if self.directory.exists():
            shutil.rmtree(self.directory)  # leftovers of a crashed session with the same name
        self.directory.mkdir(parents=True)

    async def store(self, seq: int, filename: str, data: bytes) -> Path:
        """Write a segment atomically (temporary file + rename) without blocking the loop.

        Only the file I/O runs in a worker thread; bookkeeping stays on the event loop thread.
        """
        if seq in self._paths:
            raise ValueError(f"segment {seq} is already cached")
        write = asyncio.ensure_future(
            asyncio.to_thread(self._write, self.directory / filename, data)
        )
        self._inflight.add(write)
        write.add_done_callback(self._inflight.discard)
        # Shield: if the caller is cancelled the thread keeps running anyway, and aclose()
        # must be able to wait for it before deleting the directory.
        path = await asyncio.shield(write)
        self._paths[seq] = path
        self._pending[seq] = set(self._consumers)
        self.stored += 1
        self.peak_files = max(self.peak_files, len(self._paths))
        return path

    @staticmethod
    def _write(path: Path, data: bytes) -> Path:
        tmp = path.with_name(path.name + ".part")
        with tmp.open("wb") as fh:
            fh.write(data)
        os.replace(tmp, path)
        return path

    def path(self, seq: int) -> Path:
        return self._paths[seq]

    @property
    def files(self) -> int:
        return len(self._paths)

    def release(self, seq: int, consumer: str) -> bool:
        """Mark ``seq`` as no longer needed by ``consumer``; delete it once nobody needs it."""
        pending = self._pending.get(seq)
        if pending is None:
            return False
        pending.discard(consumer)
        if pending:
            return False
        path = self._paths.pop(seq)
        del self._pending[seq]
        try:
            path.unlink()
        except FileNotFoundError:
            log.warning("cached segment %s already removed", path)
        self.deleted += 1
        return True

    async def aclose(self) -> None:
        """Wait for in-flight writes, then remove the session directory and, if it is then
        empty, the download root."""
        if self._inflight:
            await asyncio.gather(*self._inflight, return_exceptions=True)
        shutil.rmtree(self.directory, ignore_errors=True)
        self._paths.clear()
        self._pending.clear()
        with contextlib.suppress(OSError):  # not empty: other sessions are still running
            self.root.rmdir()
