import asyncio
import struct

import pytest

from streamwise.common import protocol as proto
from streamwise.common.protocol import Message, MessageType, ProtocolError


def reader_with(data: bytes, eof: bool = True) -> asyncio.StreamReader:
    reader = asyncio.StreamReader()
    reader.feed_data(data)
    if eof:
        reader.feed_eof()
    return reader


def test_header_layout() -> None:
    raw = proto.encode_message(Message(MessageType.GET_SEGMENT, 7, b"abc"))
    assert proto.HEADER_SIZE == 16
    assert raw[:2] == b"SW"
    assert raw[2] == proto.VERSION
    assert raw[3] == MessageType.GET_SEGMENT
    assert struct.unpack("!III", raw[4:16]) == (7, 3, proto.crc32(b"abc"))
    assert raw[16:] == b"abc"


async def test_roundtrip_multiple_messages() -> None:
    msgs = [
        Message(MessageType.HELLO, 1, b'{"client":"a"}'),
        Message(MessageType.SEGMENT, 2, bytes(range(256)) * 100),
        Message(MessageType.BYE, 3),
    ]
    reader = reader_with(b"".join(proto.encode_message(m) for m in msgs))
    for expected in msgs:
        assert await proto.read_message(reader, proto.MAX_RESPONSE_PAYLOAD) == expected
    assert await proto.read_message(reader, proto.MAX_RESPONSE_PAYLOAD) is None


async def test_crc_mismatch_detected() -> None:
    raw = bytearray(proto.encode_message(Message(MessageType.SEGMENT, 1, b"payload")))
    raw[-1] ^= 0xFF
    with pytest.raises(ProtocolError, match="CRC"):
        await proto.read_message(reader_with(bytes(raw)), 1024)


@pytest.mark.parametrize(
    ("mutate", "error"),
    [
        (lambda h: b"XX" + h[2:], "magic"),
        (lambda h: h[:2] + b"\x09" + h[3:], "version"),
        (lambda h: h[:3] + b"\x63" + h[4:], "unknown message type"),
    ],
)
async def test_bad_headers(mutate, error: str) -> None:
    raw = proto.encode_message(Message(MessageType.BYE, 1))
    with pytest.raises(ProtocolError, match=error):
        await proto.read_message(reader_with(mutate(raw)), 1024)


async def test_payload_limit() -> None:
    raw = proto.encode_message(Message(MessageType.GET_SEGMENT, 1, b"x" * 2000))
    with pytest.raises(ProtocolError, match="exceeds"):
        await proto.read_message(reader_with(raw), 1024)


async def test_truncated_stream() -> None:
    raw = proto.encode_message(Message(MessageType.SEGMENT, 1, b"x" * 100))
    with pytest.raises(ProtocolError, match="header"):
        await proto.read_message(reader_with(raw[:5]), 1024)
    with pytest.raises(ProtocolError, match="payload"):
        await proto.read_message(reader_with(raw[:50]), 1024)


def test_json_payload_validation() -> None:
    assert proto.json_message(MessageType.WELCOME, 1, {"a": 1}).json() == {"a": 1}
    with pytest.raises(ProtocolError):
        Message(MessageType.HELLO, 1, b"[1,2]").json()
    with pytest.raises(ProtocolError):
        Message(MessageType.HELLO, 1, b"\xff").json()
