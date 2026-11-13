"""Client-side session log (SQLite)."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from streamwise.analysis.metrics import SessionMetrics
from streamwise.client.playback import Stall
from streamwise.common.sqlitelog import SqliteWriter

if TYPE_CHECKING:
    from streamwise.client.window import WindowStats

SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    session_id        TEXT PRIMARY KEY,
    client_name       TEXT NOT NULL,
    server_session_id TEXT,
    video             TEXT NOT NULL,
    abr               TEXT NOT NULL,
    server            TEXT NOT NULL,     -- host:port
    started_at        REAL NOT NULL,     -- Unix time (s); relative times are measured from here
    ended_at          REAL,
    status            TEXT NOT NULL,     -- running | completed | failed | interrupted
    error             TEXT,
    config            TEXT,              -- JSON
    segments          INTEGER,
    played_s          REAL,
    startup_delay_s   REAL,
    stall_count       INTEGER,
    stall_time_s      REAL,
    rebuffer_ratio    REAL,
    avg_bitrate_kbps  REAL,
    switch_count      INTEGER,
    avg_switch_kbps   REAL,
    qoe               REAL,
    qoe_per_segment   REAL,
    connection_failures  INTEGER,    -- sliding-window transport only
    reconnects           INTEGER,
    retransmitted_chunks INTEGER,
    retransmitted_bytes  INTEGER
);
CREATE TABLE IF NOT EXISTS segments (
    session_id      TEXT NOT NULL REFERENCES sessions(session_id),
    seq             INTEGER NOT NULL,
    level           INTEGER NOT NULL,
    resolution      TEXT NOT NULL,
    bitrate_kbps    INTEGER NOT NULL,
    filename        TEXT NOT NULL,
    bytes           INTEGER NOT NULL,
    wait_s          REAL NOT NULL,   -- time spent waiting for buffer space before requesting
    request_t       REAL NOT NULL,   -- seconds since session start
    first_byte_t    REAL NOT NULL,
    done_t          REAL NOT NULL,
    download_s      REAL NOT NULL,
    throughput_bps  REAL NOT NULL,
    buffer_before_s REAL NOT NULL,   -- buffer level when the ABR decision was made
    buffer_after_s  REAL NOT NULL,   -- buffer level right after the segment arrived
    PRIMARY KEY (session_id, seq)
);
CREATE TABLE IF NOT EXISTS stalls (
    session_id TEXT NOT NULL REFERENCES sessions(session_id),
    seq        INTEGER NOT NULL,     -- segment the player waited for
    start_t    REAL NOT NULL,        -- seconds since session start
    end_t      REAL NOT NULL,
    duration_s REAL NOT NULL
);
"""


@dataclass(frozen=True, slots=True)
class SegmentRecord:
    seq: int
    level: int
    resolution: str
    bitrate_kbps: int
    filename: str
    bytes: int
    wait_s: float
    request_t: float
    first_byte_t: float
    done_t: float
    throughput_bps: float
    buffer_before_s: float
    buffer_after_s: float

    @property
    def download_s(self) -> float:
        return self.done_t - self.request_t


class ClientLog:
    def __init__(self, path: Path) -> None:
        self._db = SqliteWriter(
            path,
            SCHEMA,
            {
                "sessions": [
                    ("connection_failures", "INTEGER"),
                    ("reconnects", "INTEGER"),
                    ("retransmitted_chunks", "INTEGER"),
                    ("retransmitted_bytes", "INTEGER"),
                ]
            },
        )

    def session_started(
        self,
        session_id: str,
        client_name: str,
        video: str,
        abr: str,
        server: str,
        started_at: float,
        config: dict[str, Any],
    ) -> None:
        self._db.execute(
            "INSERT INTO sessions (session_id, client_name, video, abr, server, started_at,"
            " status, config) VALUES (?, ?, ?, ?, ?, ?, 'running', ?)",
            (session_id, client_name, video, abr, server, started_at, json.dumps(config)),
        )

    def server_session(self, session_id: str, server_session_id: str | None) -> None:
        self._db.execute(
            "UPDATE sessions SET server_session_id = ? WHERE session_id = ?",
            (server_session_id, session_id),
        )

    def segment(self, session_id: str, r: SegmentRecord) -> None:
        self._db.execute(
            "INSERT INTO segments VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                session_id,
                r.seq,
                r.level,
                r.resolution,
                r.bitrate_kbps,
                r.filename,
                r.bytes,
                r.wait_s,
                r.request_t,
                r.first_byte_t,
                r.done_t,
                r.download_s,
                r.throughput_bps,
                r.buffer_before_s,
                r.buffer_after_s,
            ),  # fmt: skip
        )

    def stall(self, session_id: str, stall: Stall, origin: float) -> None:
        self._db.execute(
            "INSERT INTO stalls VALUES (?, ?, ?, ?, ?)",
            (session_id, stall.seq, stall.start - origin, stall.end - origin, stall.duration),
        )

    def session_finished(
        self,
        session_id: str,
        ended_at: float,
        status: str,
        error: str | None,
        metrics: SessionMetrics | None,
        transport: WindowStats | None = None,
    ) -> None:
        values: dict[str, Any] = {"ended_at": ended_at, "status": status, "error": error}
        if metrics is not None:
            values.update(metrics.to_dict())
        if transport is not None:
            values.update(
                connection_failures=transport.connection_failures,
                reconnects=transport.reconnects,
                retransmitted_chunks=transport.retransmitted_chunks,
                retransmitted_bytes=transport.retransmitted_bytes,
            )
        columns = ", ".join(f"{k} = ?" for k in values)
        self._db.execute(
            f"UPDATE sessions SET {columns} WHERE session_id = ?",
            (*values.values(), session_id),
        )

    def close(self) -> None:
        self._db.close()
