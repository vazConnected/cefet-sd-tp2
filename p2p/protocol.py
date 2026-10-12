import asyncio
import json
import struct
from dataclasses import asdict
from enum import IntEnum

from p2p.metadata import Metadata

HEADER = struct.Struct("!BI")
INDEX = struct.Struct("!I")
MAX_PAYLOAD = 16 * 1024 * 1024


class MessageType(IntEnum):
    HELLO = 1
    GET_META = 2
    META = 3
    BITFIELD = 4
    REQUEST = 5
    PIECE = 6
    REJECT = 7


class ProtocolError(Exception):
    pass


async def read_message(reader: asyncio.StreamReader) -> tuple[MessageType, bytes]:
    msg_type, length = HEADER.unpack(await reader.readexactly(HEADER.size))
    if length > MAX_PAYLOAD:
        raise ProtocolError(f"payload too large: {length} bytes")
    try:
        msg_type = MessageType(msg_type)
    except ValueError:
        raise ProtocolError(f"unknown message type: {msg_type}") from None
    return msg_type, await reader.readexactly(length)


def write_message(writer: asyncio.StreamWriter, msg_type: MessageType, payload: bytes = b"") -> None:
    writer.write(HEADER.pack(msg_type, len(payload)) + payload)


def pack_index(index: int) -> bytes:
    return INDEX.pack(index)


def unpack_index(payload: bytes) -> int:
    if len(payload) != INDEX.size:
        raise ProtocolError("invalid index payload")
    return INDEX.unpack(payload)[0]


def pack_piece(index: int, data: bytes) -> bytes:
    return INDEX.pack(index) + data


def unpack_piece(payload: bytes) -> tuple[int, bytes]:
    if len(payload) < INDEX.size:
        raise ProtocolError("invalid piece payload")
    return INDEX.unpack_from(payload)[0], payload[INDEX.size:]


def pack_bitfield(have: list[bool]) -> bytes:
    bits = bytearray((len(have) + 7) // 8)
    for index, present in enumerate(have):
        if present:
            bits[index // 8] |= 0x80 >> (index % 8)
    return bytes(bits)


def unpack_bitfield(payload: bytes, count: int) -> list[bool]:
    if len(payload) != (count + 7) // 8:
        raise ProtocolError("invalid bitfield length")
    return [bool(payload[index // 8] & (0x80 >> (index % 8))) for index in range(count)]


def pack_meta(meta: Metadata) -> bytes:
    return json.dumps(asdict(meta)).encode()


def unpack_meta(payload: bytes) -> Metadata:
    try:
        return Metadata(**json.loads(payload))
    except (ValueError, TypeError) as error:
        raise ProtocolError(f"invalid metadata: {error}") from None
