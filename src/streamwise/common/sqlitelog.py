"""Non-blocking SQLite writer for logs produced inside an asyncio event loop.

Statements are queued and executed by a dedicated thread, so a slow disk never stalls the
event loop (and therefore never distorts transfer timing). The database uses WAL mode with a
busy timeout, so several processes (e.g. multiple clients) can append to the same file.
"""

from __future__ import annotations

import logging
import queue
import sqlite3
import threading
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

_STOP = object()


class SqliteWriter:
    def __init__(self, path: Path, schema: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        # Create the schema synchronously so errors surface at construction time.
        with sqlite3.connect(path, timeout=30) as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.executescript(schema)
        self._queue: queue.Queue[Any] = queue.Queue()
        self._thread = threading.Thread(target=self._run, name=f"sqlite:{path.name}", daemon=True)
        self._closed = False
        self._thread.start()

    def execute(self, sql: str, params: tuple[Any, ...] = ()) -> None:
        if self._closed:
            raise RuntimeError("SqliteWriter is closed")
        self._queue.put((sql, params))

    def close(self) -> None:
        """Flush all queued statements and stop the writer thread."""
        if self._closed:
            return
        self._closed = True
        self._queue.put(_STOP)
        self._thread.join()

    def _run(self) -> None:
        conn = sqlite3.connect(self.path, timeout=30)
        try:
            stop = False
            while not stop:
                item = self._queue.get()
                batch = [item]
                while True:  # drain everything already queued into one transaction
                    try:
                        batch.append(self._queue.get_nowait())
                    except queue.Empty:
                        break
                with conn:
                    for entry in batch:
                        if entry is _STOP:
                            stop = True
                            continue
                        try:
                            conn.execute(*entry)
                        except sqlite3.Error:
                            log.exception("failed to write log entry: %s", entry[0])
        finally:
            conn.close()
