from __future__ import annotations

import asyncio
import base64
import http.client
import ipaddress
import mimetypes
import socket
import ssl
import struct
import threading
import time
import zlib
from collections.abc import Callable
from contextlib import suppress
from functools import lru_cache
from urllib.parse import urljoin, urlsplit, urlunsplit

from ..constants import PLUGIN_NAME, PLUGIN_VERSION
from ..models import BackgroundCacheEntry, BackgroundRenderImage

ALLOWED_IMAGE_TYPES = {
    "image/jpeg",
    "image/png",
    "image/webp",
}
MAX_IMAGE_DIMENSION = 8192
MAX_IMAGE_PIXELS = 20_000_000
MAX_REDIRECTS = 5


def _consume_task_exception(task: asyncio.Task[str]) -> None:
    if not task.cancelled():
        task.exception()


def _detect_image_type(data: bytes) -> str:
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if len(data) >= 12 and data.startswith(b"RIFF") and data[8:12] == b"WEBP":
        return "image/webp"
    return ""


def _jpeg_dimensions(data: bytes) -> tuple[int, int]:
    offset = 2
    while offset + 4 <= len(data):
        if data[offset] != 0xFF:
            offset += 1
            continue
        marker = data[offset + 1]
        offset += 2
        if marker in {0xD8, 0xD9} or 0xD0 <= marker <= 0xD7:
            continue
        if offset + 2 > len(data):
            break
        segment_length = int.from_bytes(data[offset : offset + 2], "big")
        if segment_length < 2 or offset + segment_length > len(data):
            break
        if (
            marker
            in {
                0xC0,
                0xC1,
                0xC2,
                0xC3,
                0xC5,
                0xC6,
                0xC7,
                0xC9,
                0xCA,
                0xCB,
                0xCD,
                0xCE,
                0xCF,
            }
            and segment_length >= 7
        ):
            height = int.from_bytes(data[offset + 3 : offset + 5], "big")
            width = int.from_bytes(data[offset + 5 : offset + 7], "big")
            return width, height
        offset += segment_length
    return 0, 0


def _webp_dimensions(data: bytes) -> tuple[int, int]:
    chunk = data[12:16]
    if chunk == b"VP8X" and len(data) >= 30:
        width = 1 + int.from_bytes(data[24:27], "little")
        height = 1 + int.from_bytes(data[27:30], "little")
        return width, height
    if chunk == b"VP8L" and len(data) >= 25 and data[20] == 0x2F:
        bits = int.from_bytes(data[21:25], "little")
        return (bits & 0x3FFF) + 1, ((bits >> 14) & 0x3FFF) + 1
    if (
        chunk == b"VP8 "
        and (frame := data.find(b"\x9d\x01\x2a", 20)) >= 0
        and frame + 7 <= len(data)
    ):
        width = int.from_bytes(data[frame + 3 : frame + 5], "little") & 0x3FFF
        height = int.from_bytes(data[frame + 5 : frame + 7], "little") & 0x3FFF
        return width, height
    return 0, 0


def _webp_is_animated(data: bytes) -> bool:
    if len(data) >= 21 and data[12:16] == b"VP8X" and data[20] & 0x02:
        return True
    return b"ANIM" in data


def _validate_image_dimensions(data: bytes, content_type: str) -> None:
    if content_type == "image/png" and len(data) >= 24 and data[12:16] == b"IHDR":
        width, height = struct.unpack(">II", data[16:24])
    elif content_type == "image/jpeg":
        width, height = _jpeg_dimensions(data)
    elif content_type == "image/webp" and not _webp_is_animated(data):
        width, height = _webp_dimensions(data)
    else:
        width, height = 0, 0
    if (
        width < 1
        or height < 1
        or width > MAX_IMAGE_DIMENSION
        or height > MAX_IMAGE_DIMENSION
        or width * height > MAX_IMAGE_PIXELS
    ):
        raise ValueError("背景图片尺寸无效或超过像素限制")


@lru_cache(maxsize=4)
def fallback_background_data_uri(width: int = 316, height: int = 200) -> str:
    if width < 1 or height < 1 or width * height > 1_000_000:
        raise ValueError("内置背景尺寸不合法")
    rows = bytearray()
    for y in range(height):
        rows.append(0)
        for x in range(width):
            nx = x / max(1, width - 1)
            ny = y / max(1, height - 1)
            diagonal = ((x + y * 2) // 26) % 2
            band = max(0.0, 1.0 - abs((nx * 1.25 + ny * 0.55) - 0.92) * 2.4)
            red = int(34 + 38 * nx + 22 * ny + 34 * band + 18 * diagonal)
            green = int(62 + 48 * nx + 30 * ny + 68 * band + 12 * diagonal)
            blue = int(84 + 54 * nx + 48 * ny + 50 * band + 24 * diagonal)
            rows.extend((min(255, red), min(255, green), min(255, blue)))

    def chunk(kind: bytes, payload: bytes) -> bytes:
        return (
            struct.pack(">I", len(payload))
            + kind
            + payload
            + struct.pack(">I", zlib.crc32(kind + payload) & 0xFFFFFFFF)
        )

    png = (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(bytes(rows), 9))
        + chunk(b"IEND", b"")
    )
    return f"data:image/png;base64,{base64.b64encode(png).decode('ascii')}"


def _validate_http_url(url: str) -> None:
    if any(character.isspace() or ord(character) < 32 for character in url):
        raise ValueError("背景图 URL 不能包含空白或控制字符")
    parsed = urlsplit(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("背景图 URL 必须是有效的 http:// 或 https:// 地址")
    if parsed.username or parsed.password:
        raise ValueError("背景图 URL 不能包含用户名或密码")
    try:
        _ = parsed.port
    except ValueError as exc:
        raise ValueError("背景图 URL 端口无效") from exc


def _resolve_public_addresses(
    hostname: str,
    port: int,
) -> list[tuple[int, int, int, tuple]]:
    """Resolve once and return every address only when all are globally routable."""

    try:
        addresses = socket.getaddrinfo(
            hostname,
            port,
            type=socket.SOCK_STREAM,
        )
    except OSError as exc:
        raise ValueError(f"背景图主机名无法解析：{hostname}") from exc
    if not addresses:
        raise ValueError(f"背景图主机名没有可用地址：{hostname}")

    validated: list[tuple[int, int, int, tuple]] = []
    for family, socket_type, protocol, _canonical_name, socket_address in addresses:
        raw_address = str(socket_address[0]).split("%", 1)[0]
        try:
            address = ipaddress.ip_address(raw_address)
        except ValueError as exc:
            raise ValueError("背景图主机名解析结果不是有效 IP 地址") from exc
        if not address.is_global:
            raise ValueError(f"背景图 URL 不允许访问非公网地址：{address}")
        validated.append((family, socket_type, protocol, socket_address))
    return validated


def _resolve_public_address(
    hostname: str,
    port: int,
) -> tuple[int, int, int, tuple]:
    """Compatibility helper returning the first strictly validated address."""

    return _resolve_public_addresses(hostname, port)[0]


def _remaining_timeout(deadline: float) -> float:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise TimeoutError("背景图下载超时")
    return remaining


class _SocketDeadlineGuard:
    """Force-close a blocking HTTP socket when its absolute deadline expires."""

    def __init__(self, active_socket: socket.socket, timeout: float) -> None:
        self._lock = threading.Lock()
        self._socket: socket.socket | ssl.SSLSocket | None = active_socket
        self._expired = False
        self._timer = threading.Timer(max(0.0, timeout), self._expire)
        self._timer.daemon = True
        self._timer.start()

    @staticmethod
    def _abort(active_socket: socket.socket | ssl.SSLSocket) -> None:
        with suppress(OSError):
            active_socket.shutdown(socket.SHUT_RDWR)
        with suppress(OSError):
            active_socket.close()

    def _expire(self) -> None:
        with self._lock:
            self._expired = True
            active_socket = self._socket
            self._socket = None
        if active_socket is not None:
            self._abort(active_socket)

    def replace_socket(self, active_socket: socket.socket | ssl.SSLSocket) -> None:
        with self._lock:
            if not self._expired:
                self._socket = active_socket
                return
        self._abort(active_socket)

    @property
    def expired(self) -> bool:
        with self._lock:
            return self._expired

    def cancel(self) -> None:
        self._timer.cancel()
        with self._lock:
            self._socket = None


def _open_public_response(
    url: str,
    deadline: float,
) -> tuple[
    http.client.HTTPConnection,
    http.client.HTTPResponse,
    _SocketDeadlineGuard,
]:
    _validate_http_url(url)
    parsed = urlsplit(url)
    hostname = parsed.hostname or ""
    ascii_hostname = hostname.encode("idna").decode("ascii")
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    addresses = _resolve_public_addresses(
        ascii_hostname,
        port,
    )
    context = ssl.create_default_context() if parsed.scheme == "https" else None
    last_error: Exception | None = None

    for family, socket_type, protocol, socket_address in addresses:
        try:
            remaining = _remaining_timeout(deadline)
        except TimeoutError as exc:
            raise TimeoutError("背景图下载超时") from last_error or exc
        active_socket: socket.socket | ssl.SSLSocket | None = None
        connection: http.client.HTTPConnection | None = None
        deadline_guard: _SocketDeadlineGuard | None = None
        try:
            active_socket = socket.socket(family, socket_type, protocol)
            deadline_guard = _SocketDeadlineGuard(active_socket, remaining)
            active_socket.settimeout(remaining)
            active_socket.connect(socket_address)
            if context is not None:
                active_socket.settimeout(_remaining_timeout(deadline))
                active_socket = context.wrap_socket(
                    active_socket,
                    server_hostname=ascii_hostname,
                    do_handshake_on_connect=False,
                )
                deadline_guard.replace_socket(active_socket)
                active_socket.settimeout(_remaining_timeout(deadline))
                active_socket.do_handshake()
                connection = http.client.HTTPSConnection(
                    ascii_hostname,
                    port,
                    timeout=_remaining_timeout(deadline),
                    context=context,
                )
            else:
                connection = http.client.HTTPConnection(
                    ascii_hostname,
                    port,
                    timeout=_remaining_timeout(deadline),
                )
            connection.sock = active_socket
            active_socket = None

            target = parsed.path or "/"
            if parsed.query:
                target = f"{target}?{parsed.query}"
            connection.sock.settimeout(_remaining_timeout(deadline))
            connection.request(
                "GET",
                target,
                headers={
                    "User-Agent": f"{PLUGIN_NAME}/{PLUGIN_VERSION}",
                    "Accept": "image/png,image/jpeg,image/webp",
                    "Connection": "close",
                },
            )
            connection.sock.settimeout(_remaining_timeout(deadline))
            return connection, connection.getresponse(), deadline_guard
        except Exception as exc:
            if deadline_guard is not None and deadline_guard.expired:
                last_error = TimeoutError("背景图下载超时")
            else:
                last_error = exc
            if deadline_guard is not None:
                deadline_guard.cancel()
            if connection is not None:
                connection.close()
            elif active_socket is not None:
                active_socket.close()

    try:
        _remaining_timeout(deadline)
    except TimeoutError as exc:
        raise TimeoutError("背景图下载超时") from last_error or exc
    if last_error is not None:
        raise last_error
    raise OSError(f"背景图主机名没有可连接地址：{ascii_hostname}")


def _read_response_body(
    response: http.client.HTTPResponse,
    connection: http.client.HTTPConnection,
    deadline: float,
    max_bytes: int,
) -> bytes:
    chunks: list[bytes] = []
    total = 0
    while total <= max_bytes:
        remaining = _remaining_timeout(deadline)
        if connection.sock is not None:
            connection.sock.settimeout(remaining)
        chunk = response.read1(min(64 * 1024, max_bytes + 1 - total))
        if not chunk:
            break
        chunks.append(chunk)
        total += len(chunk)
    return b"".join(chunks)


def display_url(url: str) -> str:
    """Return only the URL origin for a privacy-safe chat warning."""

    try:
        parsed = urlsplit(url)
        hostname = parsed.hostname or ""
        if parsed.scheme not in {"http", "https"} or not hostname:
            return "<无效 URL>"
        try:
            if ipaddress.ip_address(hostname).version == 6:
                hostname = f"[{hostname}]"
        except ValueError:
            pass
        port = parsed.port
        host = f"{hostname}:{port}" if port is not None else hostname
        return urlunsplit((parsed.scheme, host, "", "", ""))
    except (TypeError, ValueError):
        return "<无效 URL>"


def fetch_image_data_uri(url: str, timeout: float, max_bytes: int) -> str:
    _validate_http_url(url)
    deadline = time.monotonic() + max(0.2, timeout)
    current_url = url
    data = b""
    content_type = ""
    for redirect_count in range(MAX_REDIRECTS + 1):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("背景图下载超时")
        connection, response, deadline_guard = _open_public_response(current_url, deadline)
        try:
            if response.status in {301, 302, 303, 307, 308}:
                location = response.getheader("Location")
                if not location:
                    raise ValueError("背景图重定向缺少 Location")
                if redirect_count >= MAX_REDIRECTS:
                    raise ValueError("背景图重定向次数过多")
                next_url = urljoin(current_url, location)
                _validate_http_url(next_url)
                if urlsplit(current_url).scheme == "https" and urlsplit(next_url).scheme == "http":
                    raise ValueError("背景图 URL 不允许从 HTTPS 降级重定向到 HTTP")
                current_url = next_url
                continue
            if response.status < 200 or response.status >= 300:
                raise OSError(f"背景地址返回 HTTP {response.status}")

            content_length = response.headers.get("Content-Length")
            if content_length:
                try:
                    declared_length = int(content_length)
                except (TypeError, ValueError, OverflowError):
                    declared_length = 0
                if declared_length > max_bytes:
                    raise ValueError("背景图超过大小限制")
            declared_type = response.headers.get_content_type().lower()
            if declared_type not in ALLOWED_IMAGE_TYPES:
                guessed = (mimetypes.guess_type(current_url)[0] or "").lower()
                if guessed not in ALLOWED_IMAGE_TYPES:
                    raise ValueError(f"背景地址返回了不支持的图片类型: {declared_type}")
            data = _read_response_body(response, connection, deadline, max_bytes)
        except Exception as exc:
            if deadline_guard.expired or time.monotonic() >= deadline:
                raise TimeoutError("背景图下载超时") from exc
            raise
        finally:
            deadline_guard.cancel()
            response.close()
            connection.close()

        if len(data) > max_bytes:
            raise ValueError("背景图超过大小限制")
        content_type = _detect_image_type(data)
        if content_type not in ALLOWED_IMAGE_TYPES:
            raise ValueError("背景地址返回的数据不是受支持的位图")
        _validate_image_dimensions(data, content_type)
        break
    encoded = base64.b64encode(data).decode("ascii")
    return f"data:{content_type};base64,{encoded}"


class BackgroundImageService:
    def __init__(
        self,
        url: Callable[[], str],
        ttl_seconds: Callable[[], int],
        timeout_seconds: Callable[[], float],
        max_bytes: Callable[[], int],
        warn: Callable[[str], None],
    ) -> None:
        self._url = url
        self._ttl_seconds = ttl_seconds
        self._timeout_seconds = timeout_seconds
        self._max_bytes = max_bytes
        self._warn = warn
        self._cache: BackgroundCacheEntry | None = None
        self._inflight_url = ""
        self._inflight_task: asyncio.Task[str] | None = None
        self._inflight_lock = asyncio.Lock()

    async def get(self) -> BackgroundRenderImage:
        url = self._url()
        if not url:
            return BackgroundRenderImage(
                image_url=fallback_background_data_uri(),
                is_fallback=True,
            )
        ttl = self._ttl_seconds()
        now = time.time()
        cache = self._cache if ttl > 0 else None
        if cache and cache.source_url == url and now - cache.created_at <= ttl:
            return BackgroundRenderImage(image_url=cache.image_url)

        timeout = self._timeout_seconds()
        async with self._inflight_lock:
            task = self._inflight_task
            if task is not None and self._inflight_url != url:
                task.cancel()
                task = None
            if task is None or task.cancelled() or self._inflight_url != url:
                task = asyncio.create_task(
                    asyncio.to_thread(
                        fetch_image_data_uri,
                        url,
                        timeout,
                        self._max_bytes(),
                    ),
                    name="SimpMC-Motd background fetch",
                )
                task.add_done_callback(_consume_task_exception)
                self._inflight_url = url
                self._inflight_task = task
        try:
            image_url = await asyncio.wait_for(
                asyncio.shield(task),
                timeout=timeout + 0.5,
            )
        except Exception as exc:
            public_url = display_url(url)
            warning = f"背景图 URL 无法访问：{public_url}\n错误：{type(exc).__name__}: {exc}"
            if cache and cache.source_url == url:
                self._warn(f"背景图刷新失败，继续使用旧缓存: {exc}")
                return BackgroundRenderImage(
                    image_url=cache.image_url,
                    warning=warning + "\n已使用旧背景缓存。",
                )
            self._warn(f"背景图预取失败，改用内置背景: {exc}")
            return BackgroundRenderImage(
                image_url=fallback_background_data_uri(),
                is_fallback=True,
                warning=warning + "\n已使用内置背景。",
            )
        finally:
            if task.done():
                async with self._inflight_lock:
                    if self._inflight_task is task:
                        self._inflight_task = None
                        self._inflight_url = ""

        if ttl > 0:
            self._cache = BackgroundCacheEntry(now, url, image_url)
        return BackgroundRenderImage(image_url=image_url)

    def clear(self) -> None:
        self._cache = None
        task = self._inflight_task
        self._inflight_task = None
        self._inflight_url = ""
        if task is not None and not task.done():
            task.cancel()
