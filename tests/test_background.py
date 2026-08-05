from __future__ import annotations

import asyncio
import base64
import socket
import threading
import time
import unittest
from unittest.mock import Mock, call, patch

from simpmc_motd.rendering.background import (
    BackgroundImageService,
    _detect_image_type,
    _open_public_response,
    _resolve_public_address,
    _resolve_public_addresses,
    _validate_image_dimensions,
    display_url,
    fallback_background_data_uri,
    fetch_image_data_uri,
)


def _decode_data_uri(value: str) -> bytes:
    return base64.b64decode(value.split(",", 1)[1])


def _vp8x(width: int, height: int, *, animated: bool = False) -> bytes:
    flags = 0x02 if animated else 0
    payload = (
        bytes([flags])
        + b"\0\0\0"
        + (width - 1).to_bytes(3, "little")
        + (height - 1).to_bytes(3, "little")
    )
    body = b"WEBP" + b"VP8X" + len(payload).to_bytes(4, "little") + payload
    return b"RIFF" + len(body).to_bytes(4, "little") + body


class BackgroundValidationTests(unittest.TestCase):
    def test_fallback_is_a_valid_bounded_png(self) -> None:
        data = _decode_data_uri(fallback_background_data_uri())
        self.assertEqual("image/png", _detect_image_type(data))
        _validate_image_dimensions(data, "image/png")

    def test_oversized_png_and_animated_webp_are_rejected(self) -> None:
        huge_png = (
            b"\x89PNG\r\n\x1a\n"
            + b"\0\0\0\rIHDR"
            + (9000).to_bytes(4, "big")
            + (9000).to_bytes(4, "big")
        )
        with self.assertRaisesRegex(ValueError, "像素限制"):
            _validate_image_dimensions(huge_png, "image/png")

        _validate_image_dimensions(_vp8x(640, 360), "image/webp")
        with self.assertRaisesRegex(ValueError, "像素限制"):
            _validate_image_dimensions(_vp8x(640, 360, animated=True), "image/webp")

    def test_display_url_keeps_only_scheme_hostname_and_optional_port(self) -> None:
        self.assertEqual(
            "https://example.com:8443",
            display_url(
                "https://user:password@example.com:8443/background.png?token=secret#fragment"
            ),
        )
        self.assertEqual(
            "http://[2606:4700:4700::1111]:8080",
            display_url("http://[2606:4700:4700::1111]:8080/private/path?q=secret"),
        )
        self.assertEqual("<无效 URL>", display_url("not-a-url/private/path"))

    def test_dns_results_must_all_be_globally_routable(self) -> None:
        private = (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 80))
        metadata = (
            socket.AF_INET,
            socket.SOCK_STREAM,
            6,
            "",
            ("169.254.169.254", 80),
        )
        public = (
            socket.AF_INET,
            socket.SOCK_STREAM,
            6,
            "",
            ("93.184.216.34", 80),
        )
        for addresses in ([private], [metadata], [public, private]):
            with (
                self.subTest(addresses=addresses),
                patch("socket.getaddrinfo", return_value=addresses),
                self.assertRaisesRegex(ValueError, "非公网地址"),
            ):
                _resolve_public_address("background.example", 80)

        second_public = (
            socket.AF_INET,
            socket.SOCK_STREAM,
            6,
            "",
            ("1.1.1.1", 80),
        )
        with patch("socket.getaddrinfo", return_value=[public, second_public]):
            resolved = _resolve_public_address("background.example", 80)
            all_resolved = _resolve_public_addresses("background.example", 80)
        self.assertEqual(("93.184.216.34", 80), resolved[3])
        self.assertEqual(
            [("93.184.216.34", 80), ("1.1.1.1", 80)],
            [address[3] for address in all_resolved],
        )

    def test_mixed_dns_answer_is_rejected_before_opening_any_socket(self) -> None:
        public = (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 80))
        private = (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.0.0.7", 80))
        with (
            patch("socket.getaddrinfo", return_value=[public, private]),
            patch("simpmc_motd.rendering.background.socket.socket") as socket_factory,
            self.assertRaisesRegex(ValueError, "非公网地址"),
        ):
            _open_public_response("http://background.example/image.png", time.monotonic() + 1)
        socket_factory.assert_not_called()

    def test_connection_falls_back_across_public_addresses_with_one_deadline(self) -> None:
        first_address = (
            socket.AF_INET,
            socket.SOCK_STREAM,
            6,
            "",
            ("93.184.216.34", 80),
        )
        second_address = (
            socket.AF_INET,
            socket.SOCK_STREAM,
            6,
            "",
            ("1.1.1.1", 80),
        )
        first_socket = Mock()
        first_socket.connect.side_effect = OSError("first address unavailable")
        second_socket = Mock()
        response = Mock()
        connection = Mock()
        connection.getresponse.return_value = response

        with (
            patch("socket.getaddrinfo", return_value=[first_address, second_address]),
            patch(
                "simpmc_motd.rendering.background.socket.socket",
                side_effect=[first_socket, second_socket],
            ),
            patch(
                "simpmc_motd.rendering.background.http.client.HTTPConnection",
                return_value=connection,
            ) as connection_factory,
            patch(
                "simpmc_motd.rendering.background.time.monotonic",
                side_effect=[100.0, 104.0, 105.0, 106.0, 107.0],
            ),
        ):
            actual_connection, actual_response, deadline_guard = _open_public_response(
                "http://background.example/image.png?size=large",
                deadline=110.0,
            )
        deadline_guard.cancel()

        self.assertIs(connection, actual_connection)
        self.assertIs(response, actual_response)
        first_socket.settimeout.assert_called_once_with(10.0)
        first_socket.connect.assert_called_once_with(first_address[4])
        first_socket.close.assert_called_once_with()
        second_socket.connect.assert_called_once_with(second_address[4])
        self.assertEqual([call(6.0), call(4.0), call(3.0)], second_socket.settimeout.call_args_list)
        connection_factory.assert_called_once_with(
            "background.example",
            80,
            timeout=5.0,
        )
        connection.request.assert_called_once_with(
            "GET",
            "/image.png?size=large",
            headers={
                "User-Agent": "SimpMC-Motd/2.0.0",
                "Accept": "image/png,image/jpeg,image/webp",
                "Connection": "close",
            },
        )

    def test_exhausted_deadline_prevents_trying_the_next_address(self) -> None:
        addresses = [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 80)),
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("1.1.1.1", 80)),
        ]
        first_socket = Mock()
        first_socket.connect.side_effect = OSError("first address unavailable")
        with (
            patch("socket.getaddrinfo", return_value=addresses),
            patch(
                "simpmc_motd.rendering.background.socket.socket",
                return_value=first_socket,
            ) as socket_factory,
            patch(
                "simpmc_motd.rendering.background.time.monotonic",
                side_effect=[100.0, 111.0],
            ),
            self.assertRaisesRegex(TimeoutError, "下载超时"),
        ):
            _open_public_response(
                "http://background.example/image.png",
                deadline=110.0,
            )
        socket_factory.assert_called_once_with(socket.AF_INET, socket.SOCK_STREAM, 6)
        first_socket.close.assert_called_once_with()

    def test_slow_tls_handshake_is_owned_by_absolute_deadline_guard(self) -> None:
        public = (
            socket.AF_INET,
            socket.SOCK_STREAM,
            socket.IPPROTO_TCP,
            "",
            ("93.184.216.34", 443),
        )
        raw_socket = Mock()

        class SlowHandshakeSocket:
            def __init__(self) -> None:
                self.closed = threading.Event()
                self.timeouts: list[float] = []

            def settimeout(self, timeout: float) -> None:
                self.timeouts.append(timeout)

            def do_handshake(self) -> None:
                if not self.closed.wait(timeout=2.0):
                    raise OSError("slow TLS handshake outlived its deadline")
                raise OSError("TLS socket closed by deadline guard")

            def shutdown(self, _how: int) -> None:
                self.closed.set()

            def close(self) -> None:
                self.closed.set()

        wrapped_socket = SlowHandshakeSocket()
        context = Mock()
        context.wrap_socket.return_value = wrapped_socket
        with (
            patch("socket.getaddrinfo", return_value=[public]),
            patch("simpmc_motd.rendering.background.socket.socket", return_value=raw_socket),
            patch("simpmc_motd.rendering.background.time.monotonic", return_value=100.0),
            patch(
                "simpmc_motd.rendering.background.ssl.create_default_context",
                return_value=context,
            ),
            self.assertRaisesRegex(TimeoutError, "下载超时"),
        ):
            _open_public_response(
                "https://background.example/image.png",
                deadline=100.2,
            )

        self.assertTrue(wrapped_socket.closed.is_set())
        context.wrap_socket.assert_called_once_with(
            raw_socket,
            server_hostname="background.example",
            do_handshake_on_connect=False,
        )

    def test_body_eof_after_absolute_deadline_is_timeout(self) -> None:
        clock = [100.0]
        body = _decode_data_uri(fallback_background_data_uri(16, 16))
        response = Mock()
        response.status = 200
        response.headers.get.return_value = str(len(body))
        response.headers.get_content_type.return_value = "image/png"
        reads = iter((body[:24], b""))

        def read1(_size: int) -> bytes:
            # Linux commonly reports EOF when another thread shuts down the
            # deadline-owned socket, while Windows raises OSError instead.
            chunk = next(reads)
            clock[0] = 100.1 if chunk else 100.21
            return chunk

        response.read1.side_effect = read1
        connection = Mock()
        connection.sock = Mock()
        deadline_guard = Mock(expired=False)
        with (
            patch(
                "simpmc_motd.rendering.background.time.monotonic",
                side_effect=lambda: clock[0],
            ),
            patch(
                "simpmc_motd.rendering.background._open_public_response",
                return_value=(connection, response, deadline_guard),
            ),
            self.assertRaisesRegex(TimeoutError, "下载超时"),
        ):
            fetch_image_data_uri(
                "http://background.example/image.png",
                timeout=0.2,
                max_bytes=1024 * 1024,
            )

    def test_body_eof_before_content_length_is_rejected(self) -> None:
        body = _decode_data_uri(fallback_background_data_uri(16, 16))
        response = Mock()
        response.status = 200
        response.headers.get.return_value = str(len(body))
        response.headers.get_content_type.return_value = "image/png"
        response.read1.side_effect = (body[:24], b"")
        connection = Mock()
        connection.sock = Mock()
        deadline_guard = Mock(expired=False)

        with (
            patch(
                "simpmc_motd.rendering.background._open_public_response",
                return_value=(connection, response, deadline_guard),
            ),
            self.assertRaisesRegex(OSError, "正文不完整"),
        ):
            fetch_image_data_uri(
                "http://background.example/image.png",
                timeout=1.0,
                max_bytes=1024 * 1024,
            )


class BackgroundImageServiceTests(unittest.IsolatedAsyncioTestCase):
    async def test_concurrent_cache_misses_share_one_fetch(self) -> None:
        started = threading.Event()
        release = threading.Event()
        calls = 0
        image = fallback_background_data_uri(16, 16)

        def fetch(_url: str, _timeout: float, _max_bytes: int) -> str:
            nonlocal calls
            calls += 1
            started.set()
            if not release.wait(timeout=2.0):
                raise TimeoutError("test fetch was not released")
            return image

        service = BackgroundImageService(
            url=lambda: "https://example.com/background.png",
            ttl_seconds=lambda: 60,
            timeout_seconds=lambda: 1.0,
            max_bytes=lambda: 1024 * 1024,
            warn=lambda _message: None,
        )
        with patch("simpmc_motd.rendering.background.fetch_image_data_uri", side_effect=fetch):
            first = asyncio.create_task(service.get())
            self.assertTrue(await asyncio.to_thread(started.wait, 1.0))
            second = asyncio.create_task(service.get())
            release.set()
            first_result, second_result = await asyncio.gather(first, second)

        self.assertEqual(1, calls)
        self.assertEqual(image, first_result.image_url)
        self.assertEqual(image, second_result.image_url)

    async def test_failure_uses_fallback_and_redacts_url_secrets(self) -> None:
        warnings: list[str] = []
        service = BackgroundImageService(
            url=lambda: "https://example.com/background.png?token=secret#fragment",
            ttl_seconds=lambda: 60,
            timeout_seconds=lambda: 1.0,
            max_bytes=lambda: 1024 * 1024,
            warn=warnings.append,
        )
        with patch(
            "simpmc_motd.rendering.background.fetch_image_data_uri",
            side_effect=OSError("unavailable"),
        ):
            result = await service.get()

        self.assertTrue(result.is_fallback)
        self.assertTrue(result.image_url.startswith("data:image/png;base64,"))
        self.assertIn("https://example.com", result.warning)
        self.assertNotIn("background.png", result.warning)
        self.assertNotIn("secret", result.warning)
        self.assertEqual(1, len(warnings))


if __name__ == "__main__":
    unittest.main()
