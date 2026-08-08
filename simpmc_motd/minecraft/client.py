from __future__ import annotations

import asyncio
import json
import struct
import time
from collections.abc import Awaitable
from contextlib import suppress
from typing import Any, TypeVar

from ..constants import MAX_PLAYER_COUNT
from ..models import MinecraftStatus
from ..text import normalize_unicode
from .codec import pack_packet, pack_string, pack_varint, parse_string_from, read_packet
from .components import clean_motd, safe_favicon, sanitize_component

T = TypeVar("T")
STATUS_PROTOCOL_VERSION = 47


def _non_negative_int(
    value: Any,
    default: int = 0,
    maximum: int = MAX_PLAYER_COUNT,
) -> int:
    try:
        return min(maximum, max(0, int(value)))
    except (TypeError, ValueError, OverflowError):
        return default


async def query_minecraft_status(
    host: str,
    port: int,
    timeout: float,
) -> MinecraftStatus:
    sampled_at = time.time()
    started = time.perf_counter()
    writer: asyncio.StreamWriter | None = None
    loop = asyncio.get_running_loop()
    deadline = loop.time() + max(0.1, timeout)

    async def wait_until_deadline(awaitable: Awaitable[T]) -> T:
        remaining = deadline - loop.time()
        if remaining <= 0:
            if hasattr(awaitable, "close"):
                awaitable.close()  # type: ignore[attr-defined]
            raise TimeoutError("Minecraft 状态查询超时")
        return await asyncio.wait_for(awaitable, timeout=remaining)

    try:
        reader, writer = await wait_until_deadline(asyncio.open_connection(host, port))
        handshake = (
            pack_varint(STATUS_PROTOCOL_VERSION)
            + pack_string(host)
            + struct.pack(">H", port)
            + pack_varint(1)
        )
        writer.write(pack_packet(0, handshake))
        await wait_until_deadline(writer.drain())

        status_started = time.perf_counter()
        writer.write(pack_packet(0))
        await wait_until_deadline(writer.drain())

        packet_id, payload = await wait_until_deadline(read_packet(reader))
        if packet_id != 0:
            raise ValueError(f"服务器返回了未知 status 包: {packet_id}")
        response_text, offset = parse_string_from(payload)
        if offset != len(payload):
            raise ValueError("Minecraft status 响应包含多余数据")
        status_json = json.loads(response_text)
        if not isinstance(status_json, dict):
            raise ValueError("Minecraft status 响应必须是 JSON 对象")

        latency_ms = max(0, round((time.perf_counter() - status_started) * 1000))
        try:
            ping_started = time.perf_counter()
            nonce = int(time.time() * 1000)
            writer.write(pack_packet(1, struct.pack(">q", nonce)))
            await wait_until_deadline(writer.drain())
            pong_id, pong_payload = await wait_until_deadline(read_packet(reader))
            if (
                pong_id == 1
                and len(pong_payload) == 8
                and struct.unpack(">q", pong_payload)[0] == nonce
            ):
                latency_ms = max(0, round((time.perf_counter() - ping_started) * 1000))
        except Exception:
            pass

        players = status_json.get("players")
        if not isinstance(players, dict):
            players = {}
        version = status_json.get("version")
        if not isinstance(version, dict):
            version = {}
        protocol = version.get("protocol")
        try:
            protocol = int(protocol) if protocol is not None else None
        except (TypeError, ValueError, OverflowError):
            protocol = None
        description = sanitize_component(status_json.get("description"))
        favicon = safe_favicon(status_json.get("favicon"))
        persisted_json: dict[str, Any] = {"description": description}
        if favicon is not None:
            persisted_json["favicon"] = favicon

        return MinecraftStatus(
            ok=True,
            sampled_at=sampled_at,
            host=host,
            port=port,
            online=_non_negative_int(players.get("online")),
            max_players=_non_negative_int(players.get("max")),
            motd_plain=clean_motd(description),
            version_name=normalize_unicode(version.get("name"), 256),
            protocol=protocol,
            favicon=favicon,
            latency_ms=latency_ms,
            raw_json=persisted_json,
        )
    except Exception as exc:
        message = str(exc).strip() or "服务器未响应"
        return MinecraftStatus(
            ok=False,
            sampled_at=sampled_at,
            host=host,
            port=port,
            latency_ms=max(0, round((time.perf_counter() - started) * 1000)),
            error=f"{type(exc).__name__}: {message}",
        )
    finally:
        if writer is not None:
            writer.close()
            with suppress(Exception):
                await asyncio.wait_for(writer.wait_closed(), timeout=1.0)
