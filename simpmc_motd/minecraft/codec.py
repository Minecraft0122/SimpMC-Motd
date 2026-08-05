from __future__ import annotations

import asyncio

from ..constants import MAX_STATUS_PACKET_BYTES


def pack_varint(value: int) -> bytes:
    value &= 0xFFFFFFFF
    output = bytearray()
    while True:
        part = value & 0x7F
        value >>= 7
        if value:
            output.append(part | 0x80)
        else:
            output.append(part)
            return bytes(output)


def unpack_varint_from(data: bytes, offset: int = 0) -> tuple[int, int]:
    if offset < 0:
        raise ValueError("VarInt 偏移量不能为负数")
    value = 0
    for index in range(5):
        position = offset + index
        if position >= len(data):
            raise ValueError("VarInt 数据不完整")
        byte = data[position]
        value |= (byte & 0x7F) << (7 * index)
        if not byte & 0x80:
            return value, position + 1
    raise ValueError("VarInt 长度超过 5 字节")


async def read_varint(reader: asyncio.StreamReader) -> int:
    value = 0
    for index in range(5):
        raw = await reader.readexactly(1)
        byte = raw[0]
        value |= (byte & 0x7F) << (7 * index)
        if not byte & 0x80:
            return value
    raise ValueError("VarInt 长度超过 5 字节")


def pack_string(value: str) -> bytes:
    encoded = value.encode("utf-8")
    return pack_varint(len(encoded)) + encoded


def pack_packet(packet_id: int, payload: bytes = b"") -> bytes:
    packet = pack_varint(packet_id) + payload
    return pack_varint(len(packet)) + packet


async def read_packet(
    reader: asyncio.StreamReader,
    max_length: int = MAX_STATUS_PACKET_BYTES,
) -> tuple[int, bytes]:
    length = await read_varint(reader)
    if length < 1:
        raise ValueError("Minecraft 数据包长度不能小于 1 字节")
    if length > max_length:
        raise ValueError(f"Minecraft 数据包超过大小限制: {length} > {max_length}")
    data = await reader.readexactly(length)
    packet_id, offset = unpack_varint_from(data)
    return packet_id, data[offset:]


def parse_string_from(
    data: bytes,
    offset: int = 0,
    max_length: int = MAX_STATUS_PACKET_BYTES,
) -> tuple[str, int]:
    length, offset = unpack_varint_from(data, offset)
    if length > max_length:
        raise ValueError(f"字符串超过大小限制: {length} > {max_length}")
    end = offset + length
    if end > len(data):
        raise ValueError("字符串数据不完整")
    return data[offset:end].decode("utf-8", errors="replace"), end
