from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from typing import Any, Final

from .config import DEFAULTS, ConfigView
from .targeting import group_id_from_scope, normalize_group_scope_key, parse_server_address

_GROUP_SERVERS_FIELD: Final = "group_servers"
_STORAGE_GROUP_SERVERS_FIELD: Final = "group_servers_json"
_FORM_FIELDS: Final = tuple(key for key in DEFAULTS if key != _STORAGE_GROUP_SERVERS_FIELD)
_EXPECTED_FIELDS: Final = frozenset((*_FORM_FIELDS, _GROUP_SERVERS_FIELD))
_MAX_GROUPS: Final = 500
_MAX_WHITELIST_ENTRIES: Final = 1000
_MAX_SCOPE_LENGTH: Final = 256
_MAX_WHITELIST_TEXT_LENGTH: Final = 64 * 1024


class ConsoleSettingsError(ValueError):
    """A safe validation error that may be returned to the WebUI."""


def serialize_console_settings(settings: ConfigView) -> dict[str, Any]:
    """Return the validated, JSON-safe form model consumed by ``pages/settings``."""

    snapshot = settings.snapshot()
    payload = {key: snapshot[key] for key in _FORM_FIELDS}
    payload["group_whitelist"] = "\n".join(sorted(settings.whitelisted_scopes()))
    payload[_GROUP_SERVERS_FIELD] = [
        {
            "scope": target.scope_id,
            "name": target.server_name,
            "address": _format_address(target.host, target.port),
        }
        for target in sorted(
            settings.group_server_targets().values(),
            key=lambda item: item.scope_id.casefold(),
        )
    ]
    return payload


def validate_console_settings(payload: Any) -> dict[str, Any]:
    """Validate a complete settings form and return AstrBot storage primitives.

    The custom page is an untrusted client. It cannot choose arbitrary config
    keys, smuggle non-JSON values into ``AstrBotConfig``, or rely on ConfigView's
    runtime fallback behavior to persist invalid values.
    """

    if not isinstance(payload, Mapping):
        raise ConsoleSettingsError("请求中的 settings 必须是 JSON 对象。")

    supplied = frozenset(str(key) for key in payload)
    missing = sorted(_EXPECTED_FIELDS - supplied)
    unknown = sorted(supplied - _EXPECTED_FIELDS)
    if missing:
        raise ConsoleSettingsError(f"缺少设置字段：{', '.join(missing)}。")
    if unknown:
        raise ConsoleSettingsError(f"包含未知设置字段：{', '.join(unknown)}。")

    candidate = {key: payload[key] for key in _FORM_FIELDS}
    _validate_required_text(candidate, "server_name", "服务器名称", 128)
    _validate_required_text(candidate, "host", "默认服务器地址", 255)
    normalized_whitelist = _normalize_whitelist(candidate["group_whitelist"])
    candidate["group_whitelist"] = "\n".join(normalized_whitelist)
    candidate[_STORAGE_GROUP_SERVERS_FIELD] = "{}"

    warnings: list[str] = []
    view = ConfigView(candidate, warnings.append)
    snapshot = view.snapshot()
    if warnings:
        raise ConsoleSettingsError(warnings[0])

    parsed_host, parsed_port = parse_server_address(
        str(candidate["host"]),
        int(snapshot["port"]),
    )
    if parsed_port != snapshot["port"]:
        raise ConsoleSettingsError("默认服务器地址和端口请分别填写，不要在地址中填写另一端口。")

    group_mapping = _normalize_group_servers(
        payload[_GROUP_SERVERS_FIELD],
        int(snapshot["port"]),
    )
    normalized: dict[str, Any] = {
        key: snapshot[key] for key in _FORM_FIELDS if key != "group_whitelist"
    }
    normalized["host"] = parsed_host
    normalized["group_whitelist"] = "\n".join(normalized_whitelist)
    normalized[_STORAGE_GROUP_SERVERS_FIELD] = json.dumps(
        group_mapping,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return normalized


def _validate_required_text(
    candidate: Mapping[str, Any],
    key: str,
    label: str,
    maximum_length: int,
) -> None:
    value = candidate[key]
    if not isinstance(value, str) or not value.strip():
        raise ConsoleSettingsError(f"{label}不能为空。")
    if len(value.strip()) > maximum_length:
        raise ConsoleSettingsError(f"{label}不能超过 {maximum_length} 个字符。")


def _normalize_whitelist(raw: Any) -> tuple[str, ...]:
    if isinstance(raw, str):
        if len(raw) > _MAX_WHITELIST_TEXT_LENGTH:
            raise ConsoleSettingsError("群白名单内容过长。")
        items: Sequence[Any] = raw.replace("，", ",").replace("；", ",").split(",")
        items = [part for item in items for part in str(item).split()]
    elif isinstance(raw, (list, tuple)):
        items = raw
    else:
        raise ConsoleSettingsError("群白名单必须是文本或字符串列表。")

    if len(items) > _MAX_WHITELIST_ENTRIES:
        raise ConsoleSettingsError(f"群白名单最多允许 {_MAX_WHITELIST_ENTRIES} 项。")

    scopes: set[str] = set()
    for raw_scope in items:
        if not isinstance(raw_scope, str):
            raise ConsoleSettingsError("群白名单中的每一项都必须是字符串。")
        scope = _normalize_group_scope(raw_scope, "群白名单")
        if scope:
            scopes.add(scope)
    return tuple(sorted(scopes))


def _normalize_group_servers(raw: Any, default_port: int) -> dict[str, dict[str, str]]:
    if not isinstance(raw, list):
        raise ConsoleSettingsError("群服务器配置必须是列表。")
    if len(raw) > _MAX_GROUPS:
        raise ConsoleSettingsError(f"群服务器配置最多允许 {_MAX_GROUPS} 项。")

    servers: dict[str, dict[str, str]] = {}
    canonical_scopes: set[str] = set()
    allowed_keys = frozenset({"scope", "name", "address"})
    for index, row in enumerate(raw, start=1):
        if not isinstance(row, Mapping):
            raise ConsoleSettingsError(f"第 {index} 条群服务器配置必须是对象。")
        unknown = sorted(str(key) for key in row if str(key) not in allowed_keys)
        if unknown:
            raise ConsoleSettingsError(
                f"第 {index} 条群服务器配置包含未知字段：{', '.join(unknown)}。"
            )
        missing = sorted(key for key in ("scope", "address") if key not in row)
        if missing:
            raise ConsoleSettingsError(f"第 {index} 条群服务器配置缺少字段：{', '.join(missing)}。")

        raw_scope = row["scope"]
        if not isinstance(raw_scope, str):
            raise ConsoleSettingsError(f"第 {index} 条配置的群作用域必须是字符串。")
        scope = _normalize_group_scope(raw_scope, f"第 {index} 条配置")
        if not scope:
            raise ConsoleSettingsError(f"第 {index} 条配置的群作用域不能为空。")
        canonical_scope = scope.casefold()
        if canonical_scope in canonical_scopes:
            raise ConsoleSettingsError(f"群作用域 {scope} 重复。")
        canonical_scopes.add(canonical_scope)

        address = row["address"]
        if not isinstance(address, str):
            raise ConsoleSettingsError(f"第 {index} 条配置的服务器地址必须是字符串。")
        try:
            host, port = parse_server_address(address, default_port)
        except ValueError as exc:
            raise ConsoleSettingsError(f"第 {index} 条配置的服务器地址无效：{exc}") from exc

        raw_name = row.get("name", "")
        if not isinstance(raw_name, str):
            raise ConsoleSettingsError(f"第 {index} 条配置的显示名称必须是字符串。")
        name = raw_name.strip()
        if len(name) > 128:
            raise ConsoleSettingsError(f"第 {index} 条配置的显示名称不能超过 128 个字符。")
        canonical_address = _format_address(host, port)
        servers[scope] = {
            "address": canonical_address,
            "name": name or canonical_address,
        }
    return servers


def _normalize_group_scope(raw_scope: str, label: str) -> str:
    value = raw_scope.strip()
    if not value:
        return ""
    if len(value) > _MAX_SCOPE_LENGTH:
        raise ConsoleSettingsError(f"{label}的群作用域不能超过 {_MAX_SCOPE_LENGTH} 个字符。")
    if any(character.isspace() or ord(character) < 32 for character in value):
        raise ConsoleSettingsError(f"{label}的群作用域不能包含空白或控制字符。")
    scope = normalize_group_scope_key(value)
    if not scope or not group_id_from_scope(scope):
        raise ConsoleSettingsError(f"{label}的群作用域格式无效。")
    return scope


def _format_address(host: str, port: int) -> str:
    escaped_host = f"[{host}]" if ":" in host and not host.startswith("[") else host
    return f"{escaped_host}:{port}"


__all__ = [
    "ConsoleSettingsError",
    "serialize_console_settings",
    "validate_console_settings",
]
