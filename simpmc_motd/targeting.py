from __future__ import annotations

from collections.abc import Mapping
from dataclasses import replace
from types import MappingProxyType
from typing import Any, Protocol

from .config import ConfigView
from .models import ServerTarget

_MISSING_ROW = object()


class TargetStore(Protocol):
    async def get_server(self, scope_id: str) -> Any | None: ...

    async def list_servers(self) -> list[Any]: ...

    async def upsert_server(self, target: ServerTarget) -> None: ...

    async def copy_scope_history(self, source_scope: str, destination_scope: str) -> None: ...


def parse_server_address(address: str, default_port: int) -> tuple[str, int]:
    """Parse ``host[:port]`` or ``[ipv6][:port]`` and validate its bounds."""

    if isinstance(default_port, bool):
        raise ValueError("默认端口必须是 1-65535 之间的整数")
    try:
        parsed_default_port = int(default_port)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError("默认端口必须是 1-65535 之间的整数") from exc
    if parsed_default_port < 1 or parsed_default_port > 65535:
        raise ValueError("默认端口必须在 1-65535 之间")

    if not isinstance(address, str):
        raise ValueError("服务器地址必须是字符串")
    value = address.strip()
    if not value:
        raise ValueError("服务器地址不能为空")
    if "://" in value or "/" in value or any(character.isspace() for character in value):
        raise ValueError("请填写 host[:port]，不要包含协议、路径或空格")

    host = value
    port = parsed_default_port
    if value.startswith("["):
        end = value.find("]")
        if end <= 1:
            raise ValueError("IPv6 地址格式应为 [::1]:25565")
        host = value[1:end]
        rest = value[end + 1 :]
        if rest:
            if not rest.startswith(":") or not rest[1:]:
                raise ValueError("IPv6 地址端口格式应为 [::1]:25565")
            port = _parse_port(rest[1:])
    elif value.count(":") == 1:
        host_part, port_part = value.rsplit(":", 1)
        if not port_part:
            raise ValueError("服务器端口不能为空")
        host = host_part
        port = _parse_port(port_part)

    host = host.strip()
    try:
        encoded_host = host.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise ValueError("服务器地址必须是有效的 Unicode 主机名") from exc
    if (
        not host
        or len(host) > 255
        or len(encoded_host) > 255
        or any(ord(character) < 32 or ord(character) == 127 for character in host)
    ):
        raise ValueError("服务器地址长度不合法")
    if port < 1 or port > 65535:
        raise ValueError("端口必须在 1-65535 之间")
    return host, port


def _parse_port(value: Any) -> int:
    if isinstance(value, bool):
        raise ValueError("端口必须在 1-65535 之间")
    try:
        return int(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError("端口必须是 1-65535 之间的整数") from exc


def group_id_from_scope(scope_id: str) -> str:
    if scope_id.startswith("private:") or ":private:" in scope_id:
        return ""
    if scope_id.startswith("group:"):
        return scope_id.removeprefix("group:")
    if ":group:" in scope_id:
        return scope_id.split(":group:", 1)[1]
    return ""


def normalize_group_scope_key(value: Any) -> str:
    key = str(value or "").strip()
    if not key:
        return ""
    if key.startswith("private:") or ":private:" in key:
        return key
    if key.startswith("group:"):
        return key
    if ":group:" in key:
        platform, group_id = key.split(":group:", 1)
        if platform and group_id:
            return f"{platform}:group:{group_id}"
        return ""
    return f"group:{key}"


def build_group_server_targets(config: ConfigView) -> Mapping[str, ServerTarget]:
    """Validate cached backend config entries and materialize server targets."""

    targets: dict[str, ServerTarget] = {}
    for raw_key, value in config.group_servers.items():
        scope_id = normalize_group_scope_key(raw_key)
        group_id = group_id_from_scope(scope_id)
        if not scope_id or not group_id:
            config.warn_once(
                f"invalid:group_server_key:{raw_key!r}",
                f"忽略无效的 group_servers_json 群配置键：{raw_key!r}",
            )
            continue

        try:
            host, port, server_name = _parse_backend_server(value, config.port)
        except (TypeError, ValueError, OverflowError) as exc:
            config.warn_once(
                f"invalid:group_server:{raw_key!r}:{value!r}",
                f"忽略群 {raw_key!r} 的无效服务器配置：{exc}",
            )
            continue

        targets[scope_id] = ServerTarget(
            scope_id=scope_id,
            scope_label=f"群 {group_id}",
            server_name=server_name,
            host=host,
            port=port,
            configured=True,
        )
    return MappingProxyType(targets)


def _parse_backend_server(value: Any, default_port: int) -> tuple[str, int, str]:
    if isinstance(value, str):
        host, port = parse_server_address(value, default_port)
        return host, port, f"{host}:{port}"

    if not isinstance(value, Mapping):
        raise ValueError("配置值必须是地址字符串或对象")

    address = value.get("address")
    if address is not None and str(address).strip():
        if not isinstance(address, str):
            raise ValueError("address 必须是字符串")
        host, port = parse_server_address(address, default_port)
    else:
        host_value = value.get("host")
        if not isinstance(host_value, str) or not host_value.strip():
            raise ValueError("缺少 address 或 host")
        port_value = value.get("port", default_port)
        if port_value is None or port_value == "":
            port_value = default_port
        port = _parse_port(port_value)
        if port < 1 or port > 65535:
            raise ValueError("端口必须在 1-65535 之间")
        host, port = parse_server_address(host_value, port)

    raw_name = value.get("name") or value.get("server_name")
    server_name = str(raw_name).strip()[:128] if raw_name is not None else ""
    return host, port, server_name or f"{host}:{port}"


def server_target_from_row(row: Any) -> ServerTarget:
    """Convert sqlite rows or mapping-like repository records to a target."""

    return ServerTarget(
        scope_id=str(_row_value(row, "scope_id")),
        scope_label=str(_row_value(row, "scope_label")),
        server_name=str(_row_value(row, "server_name")),
        host=str(_row_value(row, "host")),
        port=int(_row_value(row, "port")),
        configured=_configured_value(_row_value(row, "configured", True)),
    )


def _row_value(row: Any, key: str, default: Any = _MISSING_ROW) -> Any:
    if isinstance(row, Mapping):
        if default is _MISSING_ROW:
            return row[key]
        return row.get(key, default)

    try:
        return row[key]
    except (KeyError, IndexError):
        if default is not _MISSING_ROW:
            return default
        raise


def _configured_value(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"0", "false", "no", "off", ""}:
            return False
        if normalized in {"1", "true", "yes", "on"}:
            return True
    return bool(value)


class TargetResolver:
    """Resolve console-owned and default targets with stable precedence.

    Rows in ``servers`` are history/materialization markers only in v2. They
    must never become writable configuration through a chat-side code path.
    """

    def __init__(self, store: TargetStore, config: ConfigView) -> None:
        self.store = store
        self.config = config

    def group_server_targets(self) -> Mapping[str, ServerTarget]:
        return self.config.group_server_targets()

    def backend_target(
        self,
        scope_id: str,
        group_id: str = "",
        scope_label: str = "",
    ) -> ServerTarget | None:
        normalized = normalize_group_scope_key(scope_id)
        targets = self.group_server_targets()
        target = targets.get(normalized)
        if target is None:
            resolved_group_id = group_id or group_id_from_scope(normalized)
            if resolved_group_id:
                target = targets.get(f"group:{resolved_group_id}")
        if target is None:
            return None
        if target.scope_id == normalized:
            return target
        return replace(
            target,
            scope_id=normalized,
            scope_label=scope_label or target.scope_label,
        )

    def whitelisted_scopes(self) -> frozenset[str]:
        return self.config.whitelisted_scopes()

    def is_scope_allowed(
        self,
        scope_id: str,
        group_id: str = "",
        is_private: bool = False,
    ) -> bool:
        if is_private:
            return self.config.allow_private_chat

        normalized = normalize_group_scope_key(scope_id)
        resolved_group_id = group_id or group_id_from_scope(normalized)
        if self.backend_target(normalized, resolved_group_id) is not None:
            return True
        if not self.config.enable_group_whitelist:
            return True

        whitelist = self.whitelisted_scopes()
        legacy_scope = f"group:{resolved_group_id}" if resolved_group_id else ""
        return normalized in whitelist or legacy_scope in whitelist

    def default_target(self, scope_id: str, scope_label: str) -> ServerTarget:
        return ServerTarget(
            scope_id=scope_id,
            scope_label=scope_label,
            server_name=self.config.server_name,
            host=self.config.host,
            port=self.config.port,
            configured=False,
        )

    async def resolve(
        self,
        scope_id: str,
        scope_label: str,
        group_id: str = "",
        is_private: bool = False,
    ) -> ServerTarget:
        if not self.is_scope_allowed(scope_id, group_id, is_private):
            raise PermissionError("当前群未在 MOTD 白名单中，不能使用查询。")

        configured_target = self.backend_target(scope_id, group_id, scope_label)
        if configured_target is not None:
            normalized = normalize_group_scope_key(scope_id)
            legacy_scope = f"group:{group_id or group_id_from_scope(normalized)}"
            backend_targets = self.group_server_targets()
            if normalized not in backend_targets and legacy_scope in backend_targets:
                # Remember which concrete platform used a legacy wildcard so
                # the collector can keep platform-isolated history. The row is
                # non-authoritative and becomes inert if the backend wildcard
                # is later removed.
                await self.store.upsert_server(replace(configured_target, configured=False))
            return configured_target

        row = await self.store.get_server(scope_id)
        if row is not None:
            # v1 rows may still contain configured=true after an interrupted
            # migration. They are intentionally non-authoritative in v2.
            if not self.config.use_default_server_for_unconfigured_groups:
                raise LookupError("当前群还没有配置 MOTD 查询地址。")
            return self.default_target(scope_id, scope_label)

        # v1.3.x used group:{id} across every platform. Keep its history
        # discoverable under the current platform scope, but never recover the
        # row's old host/port as configuration.
        resolved_group_id = group_id or group_id_from_scope(scope_id)
        legacy_scope = f"group:{resolved_group_id}" if resolved_group_id else ""
        if legacy_scope and legacy_scope != scope_id:
            legacy_row = await self.store.get_server(legacy_scope)
            if legacy_row is not None:
                await self.store.copy_scope_history(legacy_scope, scope_id)
                await self.store.upsert_server(self.default_target(scope_id, scope_label))

        if not self.config.use_default_server_for_unconfigured_groups:
            raise LookupError("当前群还没有配置 MOTD 查询地址。")
        target = self.default_target(scope_id, scope_label)
        normalized = normalize_group_scope_key(scope_id)
        resolved_group_id = group_id or group_id_from_scope(normalized)
        legacy_scope = f"group:{resolved_group_id}" if resolved_group_id else ""
        whitelist = self.whitelisted_scopes()
        if (
            self.config.enable_group_whitelist
            and ":group:" in normalized
            and normalized not in whitelist
            and legacy_scope in whitelist
        ):
            await self.store.upsert_server(target)
        return target

    async def target_for_scope(
        self,
        scope_id: str,
        scope_label: str,
        group_id: str = "",
        is_private: bool = False,
    ) -> ServerTarget:
        """Named alias used by adapters that already resolved an event scope."""

        return await self.resolve(scope_id, scope_label, group_id, is_private)

    async def collector_targets(self) -> list[ServerTarget]:
        targets: dict[str, ServerTarget] = dict(self.group_server_targets())

        rows = await self.store.list_servers()
        for row in rows:
            try:
                stored_target = server_target_from_row(row)
            except (KeyError, TypeError, ValueError, OverflowError) as exc:
                self.config.warn_once(
                    f"invalid:stored_target:{row!r}",
                    f"忽略无法解析的已存服务器配置：{exc}",
                )
                continue

            if stored_target.scope_id in targets:
                continue
            group_id = group_id_from_scope(stored_target.scope_id)
            is_private = not bool(group_id)
            if not self.is_scope_allowed(stored_target.scope_id, group_id, is_private):
                continue

            backend_target = self.backend_target(
                stored_target.scope_id,
                group_id,
                stored_target.scope_label,
            )
            if backend_target is not None:
                targets[stored_target.scope_id] = backend_target
            elif (
                self.config.use_default_server_for_unconfigured_groups
                and self.config.enable_group_whitelist
                and (
                    stored_target.scope_id in self.whitelisted_scopes()
                    or f"group:{group_id}" in self.whitelisted_scopes()
                )
            ):
                targets[stored_target.scope_id] = self.default_target(
                    stored_target.scope_id,
                    stored_target.scope_label,
                )
            # Legacy default snapshots are intentionally ignored. Explicitly
            # whitelisted scopes are added below from the current global config.

        if self.config.use_default_server_for_unconfigured_groups:
            for scope_id in self.whitelisted_scopes():
                if scope_id in targets:
                    continue
                group_id = group_id_from_scope(scope_id)
                if group_id:
                    targets[scope_id] = self.default_target(scope_id, f"群 {group_id}")

        return list(targets.values())


__all__ = [
    "TargetResolver",
    "TargetStore",
    "build_group_server_targets",
    "group_id_from_scope",
    "normalize_group_scope_key",
    "parse_server_address",
    "server_target_from_row",
]
