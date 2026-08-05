from __future__ import annotations

import json
import math
import re
from collections.abc import Callable, Mapping
from types import MappingProxyType
from typing import Any, Final

WarningCallback = Callable[[str], None]


DEFAULTS: Final[Mapping[str, Any]] = MappingProxyType(
    {
        "server_name": "Minecraft Server",
        "host": "127.0.0.1",
        "port": 25565,
        "protocol_version": 760,
        "send_latency_ping": False,
        "query_interval_seconds": 300,
        "max_parallel_queries": 4,
        "render_cache_seconds": 45,
        "sample_reuse_seconds": 30,
        "background_image_url": "https://api.imlazy.ink/img",
        "background_opacity": 0.46,
        "background_overlay_opacity": 0.54,
        "background_cache_seconds": 3600,
        "background_fetch_timeout_seconds": 5.0,
        "background_max_bytes": 8 * 1024 * 1024,
        "enable_group_whitelist": False,
        "group_whitelist": "",
        "group_servers_json": "{}",
        "allow_private_chat": True,
        "use_default_server_for_unconfigured_groups": True,
        "timeout_seconds": 3.0,
        "chart_hours": 24,
        "retention_days": 30,
        "max_chart_points": 180,
    }
)

_MISSING: Final = object()
_TRUE_VALUES: Final = frozenset({"1", "true", "yes", "on"})
_FALSE_VALUES: Final = frozenset({"0", "false", "no", "off"})
_UNSAFE_URL_CHARS: Final = frozenset({'"', "'", "(", ")", "\\", "<", ">"})
_LIST_SEPARATOR_RE: Final = re.compile(r"[\s,，;；]+")


class ConfigView:
    """A validated, dynamically-read view over an AstrBot-like config mapping.

    The wrapped object is intentionally typed as ``Any`` so this module has no
    AstrBot dependency. Scalar values are read on every property access. Only
    the parsed whitelist and ``group_servers_json`` structures are cached, and
    their cache keys are derived from the current raw values so in-place config
    changes are still observed.
    """

    def __init__(
        self,
        source: Any,
        warning: WarningCallback | None = None,
    ) -> None:
        self._source = source
        self._warning = warning
        self._warned: set[str] = set()
        self._whitelist_token: object = _MISSING
        self._whitelist_cache: frozenset[str] = frozenset()
        self._group_servers_token: object = _MISSING
        self._group_servers_cache: Mapping[str, Any] = MappingProxyType({})
        self._group_targets_source: object = _MISSING
        self._group_targets_port: int | None = None
        self._group_targets_cache: Mapping[str, Any] = MappingProxyType({})
        self._normalized_whitelist_source: object = _MISSING
        self._normalized_whitelist_cache: frozenset[str] = frozenset()

    @property
    def source(self) -> Any:
        return self._source

    def warn_once(self, code: str, message: str) -> None:
        """Send one warning for each stable code without letting logging fail config reads."""

        if code in self._warned:
            return
        self._warned.add(code)
        if self._warning is None:
            return
        try:
            self._warning(message)
        except Exception:
            # Configuration access must not fail because a logger/callback failed.
            return

    def _raw(self, key: str) -> Any:
        source = self._source
        if source is None:
            return _MISSING

        getter = getattr(source, "get", None)
        if callable(getter):
            try:
                return getter(key, _MISSING)
            except TypeError:
                try:
                    value = getter(key)
                except (KeyError, AttributeError, TypeError):
                    return _MISSING
                return value
            except (KeyError, AttributeError):
                return _MISSING

        try:
            return getattr(source, key)
        except (AttributeError, TypeError):
            return _MISSING

    def _value(self, key: str) -> Any:
        value = self._raw(key)
        if value is _MISSING or value is None:
            return DEFAULTS[key]
        return value

    @staticmethod
    def _token(value: Any) -> tuple[str, str]:
        try:
            return (
                "json",
                json.dumps(
                    value,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ),
            )
        except (TypeError, ValueError, OverflowError):
            return "repr", repr(value)

    def _invalid(self, key: str, value: Any, expected: str) -> None:
        token = self._token(value)[1]
        self.warn_once(
            f"invalid:{key}:{token}",
            f"配置 {key} 的值无效（期望 {expected}），已回退默认值 {DEFAULTS[key]!r}。",
        )

    def _string(
        self,
        key: str,
        *,
        strip: bool = True,
        maximum_length: int | None = None,
    ) -> str:
        value = self._value(key)
        if not isinstance(value, str):
            self._invalid(key, value, "字符串")
            return str(DEFAULTS[key])
        parsed = value.strip() if strip else value
        if not parsed:
            return str(DEFAULTS[key])
        if maximum_length is not None and len(parsed) > maximum_length:
            self._invalid(key, value, f"不超过 {maximum_length} 个字符的字符串")
            return str(DEFAULTS[key])
        return parsed

    def _integer(
        self,
        key: str,
        *,
        minimum: int | None = None,
        maximum: int | None = None,
    ) -> int:
        value = self._value(key)
        parsed: int | None = None
        if isinstance(value, bool):
            parsed = None
        elif isinstance(value, int):
            parsed = value
        elif isinstance(value, float) and math.isfinite(value) and value.is_integer():
            parsed = int(value)
        elif isinstance(value, str):
            try:
                parsed = int(value.strip(), 10)
            except (TypeError, ValueError):
                parsed = None

        if (
            parsed is None
            or (minimum is not None and parsed < minimum)
            or (maximum is not None and parsed > maximum)
        ):
            bounds = _bounds_description("整数", minimum, maximum)
            self._invalid(key, value, bounds)
            return int(DEFAULTS[key])
        return parsed

    def _float(
        self,
        key: str,
        *,
        minimum: float | None = None,
        maximum: float | None = None,
    ) -> float:
        value = self._value(key)
        parsed: float | None = None
        if not isinstance(value, bool):
            try:
                parsed = float(value)
            except (TypeError, ValueError, OverflowError):
                parsed = None

        if (
            parsed is None
            or not math.isfinite(parsed)
            or (minimum is not None and parsed < minimum)
            or (maximum is not None and parsed > maximum)
        ):
            bounds = _bounds_description("数字", minimum, maximum)
            self._invalid(key, value, bounds)
            return float(DEFAULTS[key])
        return parsed

    def _boolean(self, key: str) -> bool:
        value = self._value(key)
        if isinstance(value, bool):
            return value
        if isinstance(value, int) and not isinstance(value, bool) and value in (0, 1):
            return bool(value)
        if isinstance(value, str):
            normalized = value.strip().lower()
            if normalized in _TRUE_VALUES:
                return True
            if normalized in _FALSE_VALUES:
                return False
        self._invalid(key, value, "布尔值 true/false")
        return bool(DEFAULTS[key])

    @property
    def server_name(self) -> str:
        return self._string("server_name", maximum_length=128)

    @property
    def host(self) -> str:
        value = self._string("host", maximum_length=255)
        try:
            encoded = value.encode("utf-8")
        except UnicodeEncodeError:
            encoded = b"x" * 256
        if len(encoded) > 255 or any(ord(char) < 32 or ord(char) == 127 for char in value):
            self._invalid("host", value, "UTF-8 长度不超过 255 字节的主机名")
            return str(DEFAULTS["host"])
        return value

    @property
    def port(self) -> int:
        return self._integer("port", minimum=1, maximum=65535)

    @property
    def protocol_version(self) -> int:
        return self._integer("protocol_version", minimum=1, maximum=2_147_483_647)

    @property
    def send_latency_ping(self) -> bool:
        return self._boolean("send_latency_ping")

    @property
    def query_interval_seconds(self) -> int:
        return self._integer("query_interval_seconds", minimum=30, maximum=86_400)

    @property
    def max_parallel_queries(self) -> int:
        return self._integer("max_parallel_queries", minimum=1, maximum=32)

    @property
    def render_cache_seconds(self) -> int:
        return self._integer("render_cache_seconds", minimum=0, maximum=86_400)

    @property
    def sample_reuse_seconds(self) -> int:
        return self._integer("sample_reuse_seconds", minimum=0, maximum=3_600)

    @property
    def background_image_url(self) -> str:
        raw = self._raw("background_image_url")
        if raw is _MISSING or raw is None:
            return str(DEFAULTS["background_image_url"])
        if not isinstance(raw, str):
            self._invalid("background_image_url", raw, "HTTP(S) URL 或空字符串")
            return str(DEFAULTS["background_image_url"])

        value = raw.strip()
        if not value:
            # Unlike generic string settings, an explicit empty URL disables it.
            return ""
        if len(value) > 2048:
            self._invalid("background_image_url", value, "不超过 2048 个字符的 URL")
            return ""
        if not value.startswith(("https://", "http://")):
            self.warn_once(
                f"invalid:background_image_url:{value}",
                "配置 background_image_url 必须以 http:// 或 https:// 开头，已禁用远程背景图。",
            )
            return ""
        if any(
            character in _UNSAFE_URL_CHARS or character.isspace() or ord(character) < 32
            for character in value
        ):
            self.warn_once(
                f"unsafe:background_image_url:{value}",
                "配置 background_image_url 包含不安全字符，已禁用远程背景图。",
            )
            return ""
        return value

    @property
    def background_opacity(self) -> float:
        return self._float("background_opacity", minimum=0.0, maximum=1.0)

    @property
    def background_overlay_opacity(self) -> float:
        return self._float(
            "background_overlay_opacity",
            minimum=0.0,
            maximum=1.0,
        )

    @property
    def background_cache_seconds(self) -> int:
        return self._integer("background_cache_seconds", minimum=0, maximum=7 * 86_400)

    @property
    def background_fetch_timeout_seconds(self) -> float:
        return self._float("background_fetch_timeout_seconds", minimum=0.2, maximum=30.0)

    @property
    def background_max_bytes(self) -> int:
        return self._integer("background_max_bytes", minimum=128 * 1024, maximum=32 * 1024 * 1024)

    @property
    def enable_group_whitelist(self) -> bool:
        return self._boolean("enable_group_whitelist")

    @property
    def allow_private_chat(self) -> bool:
        return self._boolean("allow_private_chat")

    @property
    def use_default_server_for_unconfigured_groups(self) -> bool:
        return self._boolean("use_default_server_for_unconfigured_groups")

    @property
    def timeout_seconds(self) -> float:
        return self._float("timeout_seconds", minimum=0.5, maximum=30.0)

    @property
    def chart_hours(self) -> int:
        return self._integer("chart_hours", minimum=1, maximum=24 * 30)

    @property
    def retention_days(self) -> int:
        return self._integer("retention_days", minimum=1, maximum=3650)

    @property
    def max_chart_points(self) -> int:
        return self._integer("max_chart_points", minimum=20, maximum=2000)

    @property
    def whitelist_entries(self) -> frozenset[str]:
        raw = self._raw("group_whitelist")
        if raw is _MISSING or raw is None or raw == "":
            raw = DEFAULTS["group_whitelist"]
        token = self._token(raw)
        if token == self._whitelist_token:
            return self._whitelist_cache

        if isinstance(raw, str):
            items = _LIST_SEPARATOR_RE.split(raw)
        elif isinstance(raw, (list, tuple, set, frozenset)):
            items = list(raw)
        else:
            self._invalid("group_whitelist", raw, "字符串或字符串列表")
            items = []

        parsed = frozenset(text for item in items if (text := str(item).strip()))
        self._whitelist_token = token
        self._whitelist_cache = parsed
        return parsed

    @property
    def group_whitelist(self) -> frozenset[str]:
        """Compatibility alias for the parsed whitelist entries."""

        return self.whitelist_entries

    @property
    def group_servers(self) -> Mapping[str, Any]:
        raw = self._raw("group_servers_json")
        if raw is _MISSING or raw is None or raw == "":
            raw = DEFAULTS["group_servers_json"]
        token = self._token(raw)
        if token == self._group_servers_token:
            return self._group_servers_cache

        parsed: Any
        if isinstance(raw, str):
            try:
                parsed = json.loads(raw)
            except (TypeError, ValueError, json.JSONDecodeError) as exc:
                self.warn_once(
                    f"invalid:group_servers_json:{token[1]}",
                    f"配置 group_servers_json 不是有效 JSON 对象，已忽略：{exc}",
                )
                parsed = {}
        elif isinstance(raw, Mapping):
            try:
                # Make an immutable snapshot so callers cannot mutate config state.
                parsed = json.loads(json.dumps(raw, ensure_ascii=False, sort_keys=True))
            except (TypeError, ValueError, OverflowError) as exc:
                self.warn_once(
                    f"invalid:group_servers_json:{token[1]}",
                    f"配置 group_servers_json 包含无法解析的值，已忽略：{exc}",
                )
                parsed = {}
        else:
            self._invalid("group_servers_json", raw, "JSON 对象")
            parsed = {}

        if not isinstance(parsed, dict):
            self._invalid("group_servers_json", raw, "JSON 对象")
            parsed = {}

        cache = _freeze_json_value(parsed)
        self._group_servers_token = token
        self._group_servers_cache = cache
        return cache

    @property
    def group_servers_json(self) -> Mapping[str, Any]:
        """Compatibility alias for the parsed backend server mapping."""

        return self.group_servers

    def whitelisted_scopes(self) -> frozenset[str]:
        """Return cached whitelist entries normalized to stable scope IDs."""

        entries = self.whitelist_entries
        if entries is self._normalized_whitelist_source:
            return self._normalized_whitelist_cache
        scopes = frozenset(
            normalized for entry in entries if (normalized := _normalize_group_scope_key(entry))
        )
        self._normalized_whitelist_source = entries
        self._normalized_whitelist_cache = scopes
        return scopes

    def group_server_targets(self) -> Mapping[str, Any]:
        """Return cached, validated ``ServerTarget`` objects for backend config.

        The import is intentionally local: ``targeting`` depends on ConfigView,
        while ConfigView remains importable without the models/service layer.
        """

        source = self.group_servers
        default_port = self.port
        if source is self._group_targets_source and default_port == self._group_targets_port:
            return self._group_targets_cache

        from .targeting import build_group_server_targets

        targets = build_group_server_targets(self)
        self._group_targets_source = source
        self._group_targets_port = default_port
        self._group_targets_cache = targets
        return targets

    def snapshot(self) -> dict[str, Any]:
        """Return a validated point-in-time snapshot of every schema setting."""

        return {
            "server_name": self.server_name,
            "host": self.host,
            "port": self.port,
            "protocol_version": self.protocol_version,
            "send_latency_ping": self.send_latency_ping,
            "query_interval_seconds": self.query_interval_seconds,
            "max_parallel_queries": self.max_parallel_queries,
            "render_cache_seconds": self.render_cache_seconds,
            "sample_reuse_seconds": self.sample_reuse_seconds,
            "background_image_url": self.background_image_url,
            "background_opacity": self.background_opacity,
            "background_overlay_opacity": self.background_overlay_opacity,
            "background_cache_seconds": self.background_cache_seconds,
            "background_fetch_timeout_seconds": self.background_fetch_timeout_seconds,
            "background_max_bytes": self.background_max_bytes,
            "enable_group_whitelist": self.enable_group_whitelist,
            "group_whitelist": self.whitelist_entries,
            "group_servers_json": self.group_servers,
            "allow_private_chat": self.allow_private_chat,
            "use_default_server_for_unconfigured_groups": (
                self.use_default_server_for_unconfigured_groups
            ),
            "timeout_seconds": self.timeout_seconds,
            "chart_hours": self.chart_hours,
            "retention_days": self.retention_days,
            "max_chart_points": self.max_chart_points,
        }


def _bounds_description(
    kind: str,
    minimum: int | float | None,
    maximum: int | float | None,
) -> str:
    if minimum is not None and maximum is not None:
        return f"{kind}，范围 {minimum}-{maximum}"
    if minimum is not None:
        return f"{kind}，不得小于 {minimum}"
    if maximum is not None:
        return f"{kind}，不得大于 {maximum}"
    return kind


def _freeze_json_value(value: Any) -> Any:
    if isinstance(value, dict):
        return MappingProxyType({str(key): _freeze_json_value(item) for key, item in value.items()})
    if isinstance(value, list):
        return tuple(_freeze_json_value(item) for item in value)
    return value


def _normalize_group_scope_key(value: Any) -> str:
    key = str(value or "").strip()
    if not key:
        return ""
    if key.startswith("group:"):
        return key
    if ":group:" in key:
        platform, group_id = key.split(":group:", 1)
        if platform and group_id:
            return f"{platform}:group:{group_id}"
        return ""
    if key.startswith("private:") or ":private:" in key:
        return key
    return f"group:{key}"


PluginConfig = ConfigView
Settings = ConfigView


__all__ = [
    "ConfigView",
    "DEFAULTS",
    "PluginConfig",
    "Settings",
    "WarningCallback",
]
