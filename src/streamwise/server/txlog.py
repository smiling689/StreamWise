"""Server-side transmission log (SQLite)."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from streamwise.common.sqlitelog import SqliteWriter

SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    session_id      TEXT PRIMARY KEY,
    client_name     TEXT,
    peer            TEXT NOT NULL,
    connected_at    REAL NOT NULL,   -- Unix time (s)
    disconnected_at REAL,
    close_reason    TEXT,
    encryption      TEXT             -- none | AES-256-GCM | AES-256-GCM+PSK
);
CREATE TABLE IF NOT EXISTS transmissions (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id     TEXT NOT NULL REFERENCES sessions(session_id),
    request_id     INTEGER NOT NULL,
    segment        TEXT NOT NULL,    -- requested file name
    video          TEXT,
    seq            INTEGER,
    level          INTEGER,
    resolution     TEXT,
    bitrate_kbps   INTEGER,
    bytes          INTEGER NOT NULL, -- payload bytes written (incl. the GCM tag)
    start_time     REAL NOT NULL,    -- Unix time (s) the response started
    end_time       REAL NOT NULL,    -- Unix time (s) the last byte was handed to TCP
    duration_s     REAL NOT NULL,
    throughput_bps REAL,
    status         TEXT NOT NULL,    -- ok | not_found | aborted | error
    chunk_offset   INTEGER           -- byte offset for GET_CHUNK, NULL for whole segments
);
CREATE INDEX IF NOT EXISTS idx_transmissions_session ON transmissions(session_id);
"""


@dataclass(frozen=True, slots=True)
class Transmission:
    session_id: str
    request_id: int
    segment: str
    video: str | None
    seq: int | None
    level: int | None
    resolution: str | None
    bitrate_kbps: int | None
    bytes: int
    start_time: float
    duration_s: float
    status: str
    chunk_offset: int | None = None

    @property
    def throughput_bps(self) -> float | None:
        if self.bytes <= 0 or self.duration_s <= 0:
            return None
        return self.bytes * 8 / self.duration_s


class TransmissionLog:
    def __init__(self, path: Path) -> None:
        self._db = SqliteWriter(
            path,
            SCHEMA,
            {"sessions": [("encryption", "TEXT")], "transmissions": [("chunk_offset", "INTEGER")]},
        )

    @property
    def path(self) -> Path:
        return self._db.path

    def session_opened(self, session_id: str, peer: str, connected_at: float) -> None:
        self._db.execute(
            "INSERT INTO sessions (session_id, peer, connected_at) VALUES (?, ?, ?)",
            (session_id, peer, connected_at),
        )

    def session_named(self, session_id: str, client_name: str, encryption: str = "none") -> None:
        self._db.execute(
            "UPDATE sessions SET client_name = ?, encryption = ? WHERE session_id = ?",
            (client_name, encryption, session_id),
        )

    def session_closed(self, session_id: str, disconnected_at: float, reason: str) -> None:
        self._db.execute(
            "UPDATE sessions SET disconnected_at = ?, close_reason = ? WHERE session_id = ?",
            (disconnected_at, reason, session_id),
        )

    def transmission(self, t: Transmission) -> None:
        self._db.execute(
            "INSERT INTO transmissions (session_id, request_id, segment, video, seq, level,"
            " resolution, bitrate_kbps, bytes, start_time, end_time, duration_s,"
            " throughput_bps, status, chunk_offset)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                t.session_id,
                t.request_id,
                t.segment,
                t.video,
                t.seq,
                t.level,
                t.resolution,
                t.bitrate_kbps,
                t.bytes,
                t.start_time,
                t.start_time + t.duration_s,
                t.duration_s,
                t.throughput_bps,
                t.status,
                t.chunk_offset,
            ),
        )

    def close(self) -> None:
        self._db.close()
