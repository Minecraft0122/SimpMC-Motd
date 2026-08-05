from __future__ import annotations

import asyncio
import base64
import json
import struct
import tempfile
import unittest
from contextlib import suppress
from pathlib import Path

from simpmc_motd.constants import (
    MAX_FAVICON_BYTES,
    MAX_PLAYER_COUNT,
    MAX_STATUS_PACKET_BYTES,
)
from simpmc_motd.minecraft.client import query_minecraft_status
from simpmc_motd.minecraft.codec import (
    pack_packet,
    pack_string,
    pack_varint,
    parse_string_from,
    read_packet,
    read_varint,
    unpack_varint_from,
)
from simpmc_motd.minecraft.components import (
    PNG_SIGNATURE,
    clean_motd,
    component_to_html,
    component_to_plain,
    legacy_text_to_html,
    motd_to_html,
    safe_favicon,
    safe_minecraft_color,
)
from simpmc_motd.rendering.background import fallback_background_data_uri
from simpmc_motd.storage import HistoryStore


class CodecTests(unittest.TestCase):
    def test_varint_known_values_and_round_trip(self) -> None:
        cases = {
            0: b"\x00",
            1: b"\x01",
            127: b"\x7f",
            128: b"\x80\x01",
            255: b"\xff\x01",
            2_147_483_647: b"\xff\xff\xff\xff\x07",
            -1: b"\xff\xff\xff\xff\x0f",
        }
        for value, expected in cases.items():
            with self.subTest(value=value):
                encoded = pack_varint(value)
                self.assertEqual(expected, encoded)
                decoded, offset = unpack_varint_from(b"prefix" + encoded, 6)
                self.assertEqual(value & 0xFFFFFFFF, decoded)
                self.assertEqual(6 + len(encoded), offset)

    def test_unpack_varint_rejects_invalid_inputs(self) -> None:
        with self.assertRaisesRegex(ValueError, "偏移量"):
            unpack_varint_from(b"\x00", -1)
        with self.assertRaisesRegex(ValueError, "不完整"):
            unpack_varint_from(b"\x80")
        with self.assertRaisesRegex(ValueError, "超过 5"):
            unpack_varint_from(b"\x80" * 5)

    def test_strings_and_packets(self) -> None:
        encoded = pack_string("你好 Minecraft")
        value, offset = parse_string_from(encoded)
        self.assertEqual("你好 Minecraft", value)
        self.assertEqual(len(encoded), offset)

        packet = pack_packet(3, b"payload")
        packet_length, body_offset = unpack_varint_from(packet)
        packet_id, payload_offset = unpack_varint_from(packet, body_offset)
        self.assertEqual(len(packet) - body_offset, packet_length)
        self.assertEqual(3, packet_id)
        self.assertEqual(b"payload", packet[payload_offset:])

    def test_parse_string_enforces_completeness_and_limit(self) -> None:
        with self.assertRaisesRegex(ValueError, "不完整"):
            parse_string_from(pack_varint(3) + b"ab")
        with self.assertRaisesRegex(ValueError, "大小限制"):
            parse_string_from(pack_varint(4) + b"test", max_length=3)


class AsyncCodecTests(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    def reader_with(data: bytes) -> asyncio.StreamReader:
        reader = asyncio.StreamReader()
        reader.feed_data(data)
        reader.feed_eof()
        return reader

    async def test_read_varint_and_packet(self) -> None:
        self.assertEqual(300, await read_varint(self.reader_with(pack_varint(300))))
        packet_id, payload = await read_packet(self.reader_with(pack_packet(2, b"response")))
        self.assertEqual(2, packet_id)
        self.assertEqual(b"response", payload)

    async def test_read_packet_rejects_empty_and_oversized_packets(self) -> None:
        with self.assertRaisesRegex(ValueError, "不能小于"):
            await read_packet(self.reader_with(pack_varint(0)))
        with self.assertRaisesRegex(ValueError, "大小限制"):
            await read_packet(self.reader_with(pack_varint(MAX_STATUS_PACKET_BYTES + 1)))
        with self.assertRaisesRegex(ValueError, "超过 5"):
            await read_varint(self.reader_with(b"\x80" * 5))


class ComponentTests(unittest.TestCase):
    def test_plain_component_and_clean_motd(self) -> None:
        component = {
            "translate": "server.greeting",
            "with": [{"text": "玩家"}],
            "extra": ["§a 在线\r\n第二行"],
        }
        self.assertEqual(
            "server.greeting玩家§a 在线\r\n第二行",
            component_to_plain(component),
        )
        self.assertEqual(
            "server.greeting玩家 在线\n第二行",
            clean_motd(component),
        )
        self.assertEqual("Minecraft Server", clean_motd("§a  "))

    def test_html_preserves_supported_styles_and_escapes_text(self) -> None:
        rendered = legacy_text_to_html("§aHi <player>§l & welcome")
        self.assertEqual(
            '<span style="color:#55ff55">Hi &lt;player&gt;</span>'
            '<span style="color:#55ff55;font-weight:700"> &amp; welcome</span>',
            rendered,
        )

        component = {
            "text": "<root>",
            "color": "red",
            "bold": True,
            "extra": [{"text": "&child", "italic": True}],
        }
        rendered = component_to_html(component)
        self.assertIn("color:#ff5555", rendered)
        self.assertIn("font-weight:700", rendered)
        self.assertIn("font-style:italic", rendered)
        self.assertIn("&lt;root&gt;", rendered)
        self.assertIn("&amp;child", rendered)
        self.assertNotIn("<root>", rendered)

    def test_color_validation_blocks_css_injection(self) -> None:
        self.assertEqual("#ff5555", safe_minecraft_color("RED"))
        self.assertEqual("#12abef", safe_minecraft_color("#12AbEf"))
        self.assertEqual("", safe_minecraft_color("red;background:url(x)"))
        rendered = component_to_html(
            {"text": "<script>alert(1)</script>", "color": "red;position:fixed"}
        )
        self.assertNotIn("style=", rendered)
        self.assertEqual("&lt;script&gt;alert(1)&lt;/script&gt;", rendered)
        self.assertEqual("Minecraft Server", motd_to_html(None))

    def test_recursive_components_are_bounded(self) -> None:
        component: dict[str, object] = {"text": "tail"}
        for _ in range(100):
            component = {"text": "x", "extra": [component]}
        rendered = component_to_plain(component)
        self.assertLessEqual(len(rendered), 17)
        self.assertNotIn("tail", rendered)

    def test_safe_favicon_requires_bounded_png_data(self) -> None:
        valid = fallback_background_data_uri(64, 64)
        self.assertEqual(valid, safe_favicon(valid))
        self.assertIsNone(safe_favicon(None))
        self.assertIsNone(safe_favicon("data:image/jpeg;base64,AAAA"))
        self.assertIsNone(safe_favicon("data:image/png;base64,not-valid!"))
        not_png = "data:image/png;base64," + base64.b64encode(b"not png").decode("ascii")
        self.assertIsNone(safe_favicon(not_png))
        self.assertIsNone(safe_favicon(fallback_background_data_uri(32, 32)))
        oversized = PNG_SIGNATURE + b"x" * (MAX_FAVICON_BYTES + 1)
        oversized_uri = "data:image/png;base64," + base64.b64encode(oversized).decode("ascii")
        self.assertIsNone(safe_favicon(oversized_uri))


class MinecraftClientIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def _start_status_server(
        self,
        response: object | None,
        *,
        response_packet_id: int = 0,
        trailing_payload: bytes = b"",
        ping_mode: str = "observe",
    ) -> tuple[asyncio.AbstractServer, int, dict[str, object], asyncio.Event]:
        captured: dict[str, object] = {}
        done = asyncio.Event()

        async def handle(
            reader: asyncio.StreamReader,
            writer: asyncio.StreamWriter,
        ) -> None:
            try:
                handshake_id, handshake = await read_packet(reader)
                request_id, request_payload = await read_packet(reader)
                protocol, offset = unpack_varint_from(handshake)
                requested_host, offset = parse_string_from(handshake, offset)
                requested_port = struct.unpack_from(">H", handshake, offset)[0]
                offset += 2
                next_state, offset = unpack_varint_from(handshake, offset)
                captured.update(
                    {
                        "handshake_id": handshake_id,
                        "protocol": protocol,
                        "host": requested_host,
                        "port": requested_port,
                        "next_state": next_state,
                        "handshake_end": offset,
                        "handshake_size": len(handshake),
                        "request_id": request_id,
                        "request_payload": request_payload,
                    }
                )

                if response is None:
                    await reader.read()
                    return

                response_json = json.dumps(response, ensure_ascii=True)
                writer.write(
                    pack_packet(
                        response_packet_id,
                        pack_string(response_json) + trailing_payload,
                    )
                )
                await writer.drain()

                if ping_mode == "pong":
                    ping_id, ping_payload = await read_packet(reader)
                    captured["ping_id"] = ping_id
                    captured["ping_payload"] = ping_payload
                    writer.write(pack_packet(1, ping_payload))
                    await writer.drain()
                elif ping_mode == "observe":
                    try:
                        packet_id, packet_payload = await asyncio.wait_for(
                            read_packet(reader), timeout=0.5
                        )
                        captured["extra_packet"] = (packet_id, packet_payload)
                    except (asyncio.IncompleteReadError, TimeoutError):
                        captured["extra_packet"] = None
                elif ping_mode != "drop":
                    raise AssertionError(f"unknown ping mode: {ping_mode}")
            except Exception as exc:  # surfaced explicitly by each test
                captured["server_error"] = exc
            finally:
                writer.close()
                with suppress(Exception):
                    await writer.wait_closed()
                done.set()

        server = await asyncio.start_server(handle, "127.0.0.1", 0)
        self.addAsyncCleanup(self._close_server, server)
        port = int(server.sockets[0].getsockname()[1])
        return server, port, captured, done

    @staticmethod
    async def _close_server(server: asyncio.AbstractServer) -> None:
        server.close()
        await server.wait_closed()

    async def test_status_round_trip_and_handshake_without_latency_ping(self) -> None:
        favicon = fallback_background_data_uri(64, 64)
        response = {
            "players": {
                "online": 10**400,
                "max": 20,
                "sample": [{"name": "private", "id": "private-uuid"}],
            },
            "version": {"name": "1.21.8", "protocol": 772},
            "description": {
                "text": "§aSimpMC",
                "extra": [{"text": " 生存服"}],
            },
            "favicon": favicon,
            "modinfo": {"private": "not persisted"},
        }
        _, port, captured, done = await self._start_status_server(response)

        status = await query_minecraft_status(
            "127.0.0.1",
            port,
            timeout=1.0,
            protocol_version=772,
            send_latency_ping=False,
        )
        await asyncio.wait_for(done.wait(), timeout=1.0)

        self.assertTrue(status.ok, status.error)
        self.assertEqual((MAX_PLAYER_COUNT, 20), (status.online, status.max_players))
        self.assertEqual("SimpMC 生存服", status.motd_plain)
        self.assertEqual("1.21.8", status.version_name)
        self.assertEqual(772, status.protocol)
        self.assertEqual(favicon, status.favicon)
        self.assertEqual(
            {"description": response["description"], "favicon": favicon},
            status.raw_json,
        )
        self.assertIsNone(status.latency_ms)
        self.assertEqual(0, captured["handshake_id"])
        self.assertEqual(772, captured["protocol"])
        self.assertEqual("127.0.0.1", captured["host"])
        self.assertEqual(port, captured["port"])
        self.assertEqual(1, captured["next_state"])
        self.assertEqual(captured["handshake_size"], captured["handshake_end"])
        self.assertEqual(0, captured["request_id"])
        self.assertEqual(b"", captured["request_payload"])
        self.assertIsNone(captured["extra_packet"])
        self.assertNotIn("server_error", captured)

    async def test_optional_latency_ping_is_sent_and_pong_is_accepted(self) -> None:
        response = {
            "players": {"online": "4", "max": "12"},
            "version": {"name": "test", "protocol": "760"},
            "description": "ready",
        }
        _, port, captured, done = await self._start_status_server(response, ping_mode="pong")

        status = await query_minecraft_status(
            "127.0.0.1",
            port,
            timeout=1.0,
            protocol_version=760,
            send_latency_ping=True,
        )
        await asyncio.wait_for(done.wait(), timeout=1.0)

        self.assertTrue(status.ok, status.error)
        self.assertEqual(1, captured["ping_id"])
        self.assertEqual(8, len(captured["ping_payload"]))
        self.assertIsInstance(status.latency_ms, int)
        self.assertGreaterEqual(status.latency_ms or 0, 0)
        self.assertNotIn("server_error", captured)

    async def test_escaped_lone_surrogates_are_safe_to_render_and_persist(self) -> None:
        response = {
            "players": {"online": 1, "max": 20},
            "version": {"name": "broken-\ud800", "protocol": 760},
            "description": {"text": "hello-\ud800"},
        }
        _, port, captured, done = await self._start_status_server(response)
        status = await query_minecraft_status(
            "127.0.0.1",
            port,
            timeout=1.0,
            protocol_version=760,
        )
        await asyncio.wait_for(done.wait(), timeout=1.0)

        self.assertTrue(status.ok, status.error)
        self.assertEqual("hello-\ufffd", status.motd_plain)
        self.assertEqual("broken-\ufffd", status.version_name)
        self.assertEqual({"text": "hello-\ufffd"}, status.raw_json["description"])
        self.assertNotIn("server_error", captured)

        with tempfile.TemporaryDirectory() as directory:
            store = HistoryStore(Path(directory) / "history.sqlite3")
            await store.add_sample("group:surrogate", status)
            row = await store.latest_status(
                "group:surrogate",
                status.host,
                status.port,
                max_age_seconds=60,
            )
        self.assertIsNotNone(row)
        self.assertEqual("hello-\ufffd", json.loads(row["raw_json"])["description"]["text"])

    async def test_ping_failure_does_not_discard_valid_status(self) -> None:
        response = {
            "players": {"online": -5, "max": "invalid"},
            "version": "invalid",
            "description": None,
        }
        _, port, captured, done = await self._start_status_server(response, ping_mode="drop")

        status = await query_minecraft_status(
            "127.0.0.1",
            port,
            timeout=1.0,
            protocol_version=760,
            send_latency_ping=True,
        )
        await asyncio.wait_for(done.wait(), timeout=1.0)

        self.assertTrue(status.ok, status.error)
        self.assertEqual(0, status.online)
        self.assertEqual(0, status.max_players)
        self.assertEqual("Minecraft Server", status.motd_plain)
        self.assertEqual("", status.version_name)
        self.assertIsNone(status.protocol)
        self.assertIsInstance(status.latency_ms, int)
        self.assertNotIn("server_error", captured)

    async def test_protocol_errors_become_offline_statuses(self) -> None:
        for packet_id, trailing, expected in (
            (2, b"", "未知 status 包"),
            (0, b"extra", "包含多余数据"),
        ):
            with self.subTest(packet_id=packet_id, trailing=trailing):
                _, port, captured, done = await self._start_status_server(
                    {"description": "bad"},
                    response_packet_id=packet_id,
                    trailing_payload=trailing,
                    ping_mode="drop",
                )
                status = await query_minecraft_status(
                    "127.0.0.1",
                    port,
                    timeout=1.0,
                    protocol_version=760,
                )
                await asyncio.wait_for(done.wait(), timeout=1.0)
                self.assertFalse(status.ok)
                self.assertIn(expected, status.error)
                self.assertNotIn("server_error", captured)

    async def test_single_deadline_times_out_silent_server(self) -> None:
        _, port, captured, done = await self._start_status_server(None)
        status = await query_minecraft_status(
            "127.0.0.1",
            port,
            timeout=0.1,
            protocol_version=760,
        )
        await asyncio.wait_for(done.wait(), timeout=1.0)

        self.assertFalse(status.ok)
        self.assertIn("TimeoutError", status.error)
        self.assertGreaterEqual(status.latency_ms or 0, 0)
        self.assertNotIn("server_error", captured)


if __name__ == "__main__":
    unittest.main()
