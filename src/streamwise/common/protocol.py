"""StreamWise application protocol over TCP.

Every message is a fixed 16-byte header followed by a payload::

     0               1               2               3
     0 1 2 3 4 5 6 7 0 1 2 3 4 5 6 7 0 1 2 3 4 5 6 7 0 1 2 3 4 5 6 7
    +-------------------------------+---------------+---------------+
    |          magic "SW"           |    version    |     type      |
    +-------------------------------+---------------+---------------+
    |                          request_id                           |
    +---------------------------------------------------------------+
    |                         payload_length                        |
    +---------------------------------------------------------------+
    |                         payload_crc32                         |
    +---------------------------------------------------------------+
    |                     payload (payload_length bytes) ...        |

All integers are unsigned and big-endian (network byte order). ``request_id`` is chosen by
the client and echoed in the response so responses can be matched to requests.
``payload_crc32`` is the CRC-32 (IEEE 802.3) of the payload and lets the receiver verify
segment integrity end to end.

Payloads: ``HELLO``/``WELCOME``/``ERROR`` carry UTF-8 JSON objects; ``GET_MANIFEST`` carries
the UTF-8 video name; ``MANIFEST`` carries the manifest JSON; ``GET_SEGMENT`` carries the
UTF-8 segment file name; ``SEGMENT`` carries the raw MPEG-TS bytes; ``BYE`` is empty.
"""

from __future__ import annotations

import asyncio
import json
import struct
import zlib
from dataclasses import dataclass
from enum import IntEnum
from typing import Any

MAGIC = b"SW"
VERSION = 1
HEADER = struct.Struct("!2sBBIII")
HEADER_SIZE = HEADER.size  # 16

MAX_REQUEST_PAYLOAD = 4 * 1024
"""Upper bound for payloads the server accepts from clients."""
MAX_RESPONSE_PAYLOAD = 256 * 1024 * 1024
"""Upper bound for payloads the client accepts from the server."""


class MessageType(IntEnum):
    HELLO = 1
    WELCOME = 2
    GET_MANIFEST = 3
    MANIFEST = 4
    GET_SEGMENT = 5
    SEGMENT = 6
    ERROR = 7
    BYE = 8


class ErrorCode(IntEnum):
    BAD_REQUEST = 400
    NOT_FOUND = 404
    UNEXPECTED_MESSAGE = 409
    INTERNAL_ERROR = 500
    UNSUPPORTED_VERSION = 505


class ProtocolError(Exception):
    """The peer sent bytes that violate the protocol."""


@dataclass(frozen=True, slots=True)
class Header:
    type: MessageType
    request_id: int
    payload_length: int
    payload_crc32: int


@dataclass(frozen=True, slots=True)
class Message:
    type: MessageType
    request_id: int
    payload: bytes = b""

    def json(self) -> dict[str, Any]:
        try:
            value = json.loads(self.payload)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ProtocolError(f"{self.type.name} payload is not valid JSON") from exc
        if not isinstance(value, dict):
            raise ProtocolError(f"{self.type.name} payload must be a JSON object")
        return value

    def text(self) -> str:
        try:
            return self.payload.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ProtocolError(f"{self.type.name} payload is not valid UTF-8") from exc


def crc32(data: bytes) -> int:
    return zlib.crc32(data) & 0xFFFFFFFF


def encode_header(
    msg_type: MessageType, request_id: int, payload_length: int, payload_crc32: int
) -> bytes:
    return HEADER.pack(MAGIC, VERSION, int(msg_type), request_id, payload_length, payload_crc32)


def encode_message(msg: Message) -> bytes:
    return encode_header(msg.type, msg.request_id, len(msg.payload), crc32(msg.payload)) + (
        msg.payload
    )


def json_message(msg_type: MessageType, request_id: int, obj: dict[str, Any]) -> Message:
    return Message(msg_type, request_id, json.dumps(obj, separators=(",", ":")).encode())


def error_message(request_id: int, code: ErrorCode, message: str) -> Message:
    return json_message(MessageType.ERROR, request_id, {"code": int(code), "message": message})


def decode_header(data: bytes, max_payload: int) -> Header:
    if len(data) != HEADER_SIZE:
        raise ProtocolError(f"header must be {HEADER_SIZE} bytes, got {len(data)}")
    magic, version, raw_type, request_id, length, checksum = HEADER.unpack(data)
    if magic != MAGIC:
        raise ProtocolError(f"bad magic {magic!r}")
    if version != VERSION:
        raise ProtocolError(f"unsupported protocol version {version}")
    try:
        msg_type = MessageType(raw_type)
    except ValueError as exc:
        raise ProtocolError(f"unknown message type {raw_type}") from exc
    if length > max_payload:
        raise ProtocolError(f"payload length {length} exceeds limit {max_payload}")
    return Header(msg_type, request_id, length, checksum)


async def read_header(reader: asyncio.StreamReader, max_payload: int) -> Header | None:
    """Read one header. Returns ``None`` on a clean EOF at a message boundary."""
    try:
        data = await reader.readexactly(HEADER_SIZE)
    except asyncio.IncompleteReadError as exc:
        if not exc.partial:
            return None
        raise ProtocolError("connection closed in the middle of a header") from exc
    return decode_header(data, max_payload)


async def read_payload(reader: asyncio.StreamReader, header: Header) -> bytes:
    try:
        payload = await reader.readexactly(header.payload_length)
    except asyncio.IncompleteReadError as exc:
        raise ProtocolError(
            f"connection closed after {len(exc.partial)}/{header.payload_length} payload bytes"
        ) from exc
    if crc32(payload) != header.payload_crc32:
        raise ProtocolError(f"CRC-32 mismatch in {header.type.name} payload")
    return payload


async def read_message(reader: asyncio.StreamReader, max_payload: int) -> Message | None:
    header = await read_header(reader, max_payload)
    if header is None:
        return None
    payload = await read_payload(reader, header)
    return Message(header.type, header.request_id, payload)
