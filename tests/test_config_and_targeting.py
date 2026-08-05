from __future__ import annotations

import json
import unittest
from typing import Any

from simpmc_motd.config import ConfigView
from simpmc_motd.models import ServerTarget
from simpmc_motd.targeting import (
    TargetResolver,
    group_id_from_scope,
    normalize_group_scope_key,
    parse_server_address,
)
from simpmc_motd.web_settings import (
    ConsoleSettingsError,
    serialize_console_settings,
    validate_console_settings,
)


class ConfigViewTests(unittest.TestCase):
    def test_all_24_schema_defaults(self) -> None:
        settings = ConfigView({})
        actual = settings.snapshot()
        actual["group_servers_json"] = dict(actual["group_servers_json"])

        expected = {
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
            "group_whitelist": frozenset(),
            "group_servers_json": {},
            "allow_private_chat": True,
            "use_default_server_for_unconfigured_groups": True,
            "timeout_seconds": 3.0,
            "chart_hours": 24,
            "retention_days": 30,
            "max_chart_points": 180,
        }

        self.assertEqual(24, len(expected))
        self.assertEqual(expected, actual)

    def test_values_are_read_dynamically(self) -> None:
        source: dict[str, Any] = {
            "server_name": "旧名称",
            "host": "old.example",
            "port": "25566",
            "send_latency_ping": "false",
            "chart_hours": 12,
        }
        settings = ConfigView(source)

        self.assertEqual("旧名称", settings.server_name)
        self.assertEqual("old.example", settings.host)
        self.assertEqual(25566, settings.port)
        self.assertFalse(settings.send_latency_ping)
        self.assertEqual(12, settings.chart_hours)

        source.update(
            {
                "server_name": "新名称",
                "host": "new.example",
                "port": 25577,
                "send_latency_ping": "yes",
                "chart_hours": 48,
            }
        )

        self.assertEqual("新名称", settings.server_name)
        self.assertEqual("new.example", settings.host)
        self.assertEqual(25577, settings.port)
        self.assertTrue(settings.send_latency_ping)
        self.assertEqual(48, settings.chart_hours)

    def test_invalid_and_oversized_values_fall_back_and_warn_once(self) -> None:
        source: dict[str, Any] = {
            "port": 2**100,
            "protocol_version": 2**100,
            "query_interval_seconds": 86_401,
            "max_parallel_queries": 33,
            "render_cache_seconds": 86_401,
            "sample_reuse_seconds": 3_601,
            "background_opacity": float("nan"),
            "background_overlay_opacity": -0.01,
            "background_cache_seconds": 7 * 86_400 + 1,
            "background_fetch_timeout_seconds": float("inf"),
            "background_max_bytes": 32 * 1024 * 1024 + 1,
            "timeout_seconds": 30.01,
            "chart_hours": 24 * 30 + 1,
            "retention_days": 3_651,
            "max_chart_points": 2_001,
            "send_latency_ping": "perhaps",
        }
        warnings: list[str] = []
        settings = ConfigView(source, warnings.append)
        expected = {
            "port": 25565,
            "protocol_version": 760,
            "query_interval_seconds": 300,
            "max_parallel_queries": 4,
            "render_cache_seconds": 45,
            "sample_reuse_seconds": 30,
            "background_opacity": 0.46,
            "background_overlay_opacity": 0.54,
            "background_cache_seconds": 3600,
            "background_fetch_timeout_seconds": 5.0,
            "background_max_bytes": 8 * 1024 * 1024,
            "timeout_seconds": 3.0,
            "chart_hours": 24,
            "retention_days": 30,
            "max_chart_points": 180,
            "send_latency_ping": False,
        }

        for property_name, default in expected.items():
            with self.subTest(property_name=property_name):
                self.assertEqual(default, getattr(settings, property_name))

        warning_count = len(warnings)
        self.assertEqual(len(expected), warning_count)
        for property_name in expected:
            getattr(settings, property_name)
        self.assertEqual(warning_count, len(warnings))

    def test_explicit_empty_background_url_disables_background(self) -> None:
        warnings: list[str] = []
        settings = ConfigView({"background_image_url": "   "}, warnings.append)

        self.assertEqual("", settings.background_image_url)
        self.assertEqual([], warnings)

    def test_group_json_and_whitelist_caches_invalidate_on_in_place_change(self) -> None:
        whitelist = ["100"]
        group_servers: dict[str, Any] = {
            "100": {"address": "one.example", "name": "一服"},
        }
        source: dict[str, Any] = {
            "port": 25565,
            "group_whitelist": whitelist,
            "group_servers_json": group_servers,
        }
        settings = ConfigView(source)

        first_whitelist = settings.whitelist_entries
        first_groups = settings.group_servers
        first_targets = settings.group_server_targets()
        self.assertIs(first_whitelist, settings.whitelist_entries)
        self.assertIs(first_groups, settings.group_servers)
        self.assertIs(first_targets, settings.group_server_targets())
        self.assertEqual(frozenset({"group:100"}), settings.whitelisted_scopes())
        self.assertEqual(25565, first_targets["group:100"].port)

        whitelist.append("group:200")
        group_servers["200"] = "two.example:25570"

        second_whitelist = settings.whitelist_entries
        second_groups = settings.group_servers
        second_targets = settings.group_server_targets()
        self.assertIsNot(first_whitelist, second_whitelist)
        self.assertIsNot(first_groups, second_groups)
        self.assertIsNot(first_targets, second_targets)
        self.assertEqual(
            frozenset({"group:100", "group:200"}),
            settings.whitelisted_scopes(),
        )
        self.assertEqual(25570, second_targets["group:200"].port)

        source["port"] = 25580
        third_targets = settings.group_server_targets()
        self.assertIsNot(second_targets, third_targets)
        self.assertEqual(25580, third_targets["group:100"].port)


class FakeTargetStore:
    def __init__(self, rows: list[dict[str, Any]] | None = None) -> None:
        self.rows = {str(row["scope_id"]): dict(row) for row in (rows or [])}
        self.upserts: list[ServerTarget] = []
        self.history_copies: list[tuple[str, str]] = []

    async def get_server(self, scope_id: str) -> dict[str, Any] | None:
        return self.rows.get(scope_id)

    async def list_servers(self) -> list[dict[str, Any]]:
        return list(self.rows.values())

    async def upsert_server(self, target: ServerTarget) -> None:
        self.upserts.append(target)
        self.rows[target.scope_id] = target_row(target)

    async def copy_scope_history(self, source_scope: str, destination_scope: str) -> None:
        self.history_copies.append((source_scope, destination_scope))


def target_row(target: ServerTarget) -> dict[str, Any]:
    return {
        "scope_id": target.scope_id,
        "scope_label": target.scope_label,
        "server_name": target.server_name,
        "host": target.host,
        "port": target.port,
        "configured": 1 if target.configured else 0,
    }


class ConsoleSettingsTests(unittest.TestCase):
    def test_form_round_trip_normalizes_scopes_addresses_and_types(self) -> None:
        payload = serialize_console_settings(ConfigView({}))
        payload.update(
            {
                "port": "25570",
                "max_parallel_queries": "8",
                "group_whitelist": "42, qq:group:99",
                "group_servers": [
                    {
                        "scope": "42",
                        "name": "生存服",
                        "address": "[2001:db8::1]:25571",
                    }
                ],
            }
        )

        normalized = validate_console_settings(payload)
        stored_groups = json.loads(normalized["group_servers_json"])

        self.assertEqual(25570, normalized["port"])
        self.assertEqual(8, normalized["max_parallel_queries"])
        self.assertEqual("group:42\nqq:group:99", normalized["group_whitelist"])
        self.assertEqual(
            {
                "group:42": {
                    "address": "[2001:db8::1]:25571",
                    "name": "生存服",
                }
            },
            stored_groups,
        )

    def test_form_rejects_unknown_missing_and_invalid_values(self) -> None:
        valid = serialize_console_settings(ConfigView({}))

        with self.assertRaisesRegex(ConsoleSettingsError, "未知设置字段"):
            validate_console_settings({**valid, "dangerous": True})

        missing = dict(valid)
        missing.pop("host")
        with self.assertRaisesRegex(ConsoleSettingsError, "缺少设置字段"):
            validate_console_settings(missing)

        invalid = dict(valid)
        invalid["port"] = 70000
        with self.assertRaisesRegex(ConsoleSettingsError, "配置 port"):
            validate_console_settings(invalid)

        boolean_integer = dict(valid)
        boolean_integer["max_parallel_queries"] = True
        with self.assertRaisesRegex(ConsoleSettingsError, "配置 max_parallel_queries"):
            validate_console_settings(boolean_integer)

        non_finite = dict(valid)
        non_finite["timeout_seconds"] = float("inf")
        with self.assertRaisesRegex(ConsoleSettingsError, "配置 timeout_seconds"):
            validate_console_settings(non_finite)

        duplicate = dict(valid)
        duplicate["group_servers"] = [
            {"scope": "QQ:group:42", "name": "A", "address": "one.example"},
            {"scope": "qq:group:42", "name": "B", "address": "two.example"},
        ]
        with self.assertRaisesRegex(ConsoleSettingsError, "重复"):
            validate_console_settings(duplicate)


class TargetResolverTests(unittest.IsolatedAsyncioTestCase):
    async def test_console_then_default_precedence_ignores_v1_database_binding(self) -> None:
        backend_db_row = ServerTarget(
            "group:backend",
            "群 backend",
            "数据库中的同群绑定",
            "db-shadow.example",
            25561,
            True,
        )
        stored = ServerTarget(
            "group:stored",
            "群 stored",
            "数据库绑定",
            "stored.example",
            25562,
            True,
        )
        store = FakeTargetStore([target_row(backend_db_row), target_row(stored)])
        settings = ConfigView(
            {
                "server_name": "全局默认",
                "host": "default.example",
                "port": 25563,
                "group_servers_json": {
                    "backend": {
                        "address": "backend.example:25564",
                        "name": "后台配置",
                    }
                },
            }
        )
        resolver = TargetResolver(store, settings)

        backend = await resolver.resolve("group:backend", "群 backend", "backend", False)
        database = await resolver.resolve("group:stored", "群 stored", "stored", False)
        default = await resolver.resolve("group:new", "群 new", "new", False)

        self.assertEqual(
            ServerTarget(
                "group:backend",
                "群 backend",
                "后台配置",
                "backend.example",
                25564,
                True,
            ),
            backend,
        )
        self.assertEqual("default.example", database.host)
        self.assertFalse(database.configured)
        self.assertEqual(
            ServerTarget(
                "group:new",
                "群 new",
                "全局默认",
                "default.example",
                25563,
                False,
            ),
            default,
        )
        self.assertEqual([], store.upserts, "临时默认目标不应写入 servers 表")

    async def test_whitelist_backend_override_and_private_chat_policy(self) -> None:
        source: dict[str, Any] = {
            "enable_group_whitelist": True,
            "group_whitelist": "allowed",
            "allow_private_chat": False,
            "group_servers_json": {"backend": "backend.example:25565"},
        }
        resolver = TargetResolver(FakeTargetStore(), ConfigView(source))

        self.assertTrue(resolver.is_scope_allowed("group:allowed", "allowed", False))
        self.assertFalse(resolver.is_scope_allowed("group:denied", "denied", False))
        self.assertTrue(
            resolver.is_scope_allowed("group:backend", "backend", False),
            "后台显式配置应覆盖群白名单",
        )
        self.assertFalse(
            resolver.is_scope_allowed(
                "qq:private:session-1",
                is_private=True,
            )
        )

        with self.assertRaises(PermissionError):
            await resolver.resolve("group:denied", "群 denied", "denied", False)
        with self.assertRaises(PermissionError):
            await resolver.resolve(
                "qq:private:session-1",
                "私聊会话",
                is_private=True,
            )

        source["allow_private_chat"] = True
        private_target = await resolver.resolve(
            "qq:private:session-1",
            "私聊会话",
            is_private=True,
        )
        self.assertFalse(private_target.configured)

    async def test_legacy_default_rows_do_not_enter_collector(self) -> None:
        legacy = ServerTarget(
            "group:legacy",
            "群 legacy",
            "旧默认",
            "old-default.example",
            25560,
            False,
        )
        explicit = ServerTarget(
            "group:explicit",
            "群 explicit",
            "显式绑定",
            "explicit.example",
            25561,
            True,
        )
        store = FakeTargetStore([target_row(legacy), target_row(explicit)])
        source: dict[str, Any] = {
            "server_name": "新默认",
            "host": "new-default.example",
            "port": 25562,
        }
        resolver = TargetResolver(store, ConfigView(source))

        resolved = await resolver.resolve("group:legacy", "群 legacy", "legacy", False)
        collected = await resolver.collector_targets()

        self.assertEqual("new-default.example", resolved.host)
        self.assertEqual(25562, resolved.port)
        self.assertFalse(resolved.configured)
        self.assertEqual([], collected)
        self.assertEqual([], store.upserts)

    async def test_explicitly_whitelisted_default_target_is_collected(self) -> None:
        store = FakeTargetStore()
        settings = ConfigView(
            {
                "enable_group_whitelist": True,
                "group_whitelist": "sampled",
                "use_default_server_for_unconfigured_groups": True,
                "server_name": "采样默认服",
                "host": "sample.example",
                "port": 25570,
            }
        )
        resolver = TargetResolver(store, settings)

        collected = await resolver.collector_targets()

        self.assertEqual(
            [
                ServerTarget(
                    "group:sampled",
                    "群 sampled",
                    "采样默认服",
                    "sample.example",
                    25570,
                    False,
                )
            ],
            collected,
        )
        self.assertEqual([], store.upserts)

    async def test_platform_scopes_do_not_collide_and_legacy_backend_keys_are_wildcards(
        self,
    ) -> None:
        store = FakeTargetStore()
        resolver = TargetResolver(
            store,
            ConfigView(
                {
                    "group_servers_json": {
                        "qq:group:42": "qq.example:25565",
                        "42": "wildcard.example:25566",
                    }
                }
            ),
        )

        qq = await resolver.resolve("qq:group:42", "QQ群 42", "42", False)
        telegram = await resolver.resolve(
            "telegram:group:42",
            "Telegram 群 42",
            "42",
            False,
        )

        self.assertEqual("qq.example", qq.host)
        self.assertEqual("qq:group:42", qq.scope_id)
        self.assertEqual("wildcard.example", telegram.host)
        self.assertEqual("telegram:group:42", telegram.scope_id)
        self.assertEqual(
            ["telegram:group:42"],
            [target.scope_id for target in store.upserts],
        )
        collected = await resolver.collector_targets()
        self.assertIn("telegram:group:42", {target.scope_id for target in collected})
        self.assertEqual("qq:group:42", normalize_group_scope_key("qq:group:42"))
        self.assertEqual("group:42", normalize_group_scope_key("42"))

    async def test_legacy_binding_history_is_copied_but_address_is_not_reactivated(self) -> None:
        legacy = ServerTarget(
            "group:7",
            "旧群 7",
            "Legacy",
            "legacy.example",
            25565,
            True,
        )
        store = FakeTargetStore([target_row(legacy)])
        resolver = TargetResolver(store, ConfigView({}))

        qq = await resolver.resolve("qq:group:7", "QQ群 7", "7", False)
        telegram = await resolver.resolve(
            "telegram:group:7",
            "Telegram 群 7",
            "7",
            False,
        )

        self.assertEqual("qq:group:7", qq.scope_id)
        self.assertEqual("telegram:group:7", telegram.scope_id)
        self.assertEqual("127.0.0.1", qq.host)
        self.assertEqual("127.0.0.1", telegram.host)
        self.assertFalse(qq.configured)
        self.assertFalse(telegram.configured)
        self.assertEqual(
            ["qq:group:7", "telegram:group:7"],
            [target.scope_id for target in store.upserts],
        )
        self.assertEqual(
            [
                ("group:7", "qq:group:7"),
                ("group:7", "telegram:group:7"),
            ],
            store.history_copies,
        )

    async def test_platform_tombstone_prevents_cleared_legacy_binding_from_resurrecting(
        self,
    ) -> None:
        legacy = ServerTarget(
            "group:7",
            "旧群 7",
            "Legacy",
            "legacy.example",
            25565,
            True,
        )
        tombstone = ServerTarget(
            "qq:group:7",
            "QQ群 7",
            "当前默认",
            "default.example",
            25570,
            False,
        )
        store = FakeTargetStore([target_row(legacy), target_row(tombstone)])
        resolver = TargetResolver(
            store,
            ConfigView(
                {
                    "server_name": "当前默认",
                    "host": "default.example",
                    "port": 25570,
                }
            ),
        )

        resolved = await resolver.resolve("qq:group:7", "QQ群 7", "7", False)

        self.assertEqual("default.example", resolved.host)
        self.assertFalse(resolved.configured)
        self.assertEqual([], store.upserts)
        self.assertEqual([], store.history_copies)

    async def test_platform_qualified_whitelist_does_not_allow_same_id_elsewhere(self) -> None:
        resolver = TargetResolver(
            FakeTargetStore(),
            ConfigView(
                {
                    "enable_group_whitelist": True,
                    "group_whitelist": "qq:group:99",
                }
            ),
        )
        self.assertTrue(resolver.is_scope_allowed("qq:group:99", "99", False))
        self.assertFalse(resolver.is_scope_allowed("telegram:group:99", "99", False))

    async def test_legacy_whitelist_remembers_seen_platform_for_isolated_collection(self) -> None:
        store = FakeTargetStore()
        resolver = TargetResolver(
            store,
            ConfigView(
                {
                    "enable_group_whitelist": True,
                    "group_whitelist": "group:88",
                    "host": "default.example",
                }
            ),
        )

        target = await resolver.resolve("qq:group:88", "QQ群 88", "88", False)
        collected = await resolver.collector_targets()

        self.assertFalse(target.configured)
        self.assertEqual(["qq:group:88"], [item.scope_id for item in store.upserts])
        self.assertIn("qq:group:88", {item.scope_id for item in collected})


class ServerAddressTests(unittest.TestCase):
    def test_private_session_containing_group_marker_stays_private(self) -> None:
        scope = "qq:private:session:group:42"
        self.assertEqual("", group_id_from_scope(scope))
        self.assertEqual(scope, normalize_group_scope_key(scope))

    def test_ipv6_addresses(self) -> None:
        self.assertEqual(("::1", 25565), parse_server_address("[::1]", 25565))
        self.assertEqual(
            ("2001:db8::1234", 25570),
            parse_server_address("[2001:db8::1234]:25570", 25565),
        )
        self.assertEqual(("::1", 25565), parse_server_address("::1", 25565))

    def test_host_limit_is_measured_in_utf8_bytes(self) -> None:
        exactly_255_bytes = "你" * 85
        over_255_bytes = "你" * 86

        self.assertEqual(
            (exactly_255_bytes, 25565),
            parse_server_address(exactly_255_bytes, 25565),
        )
        with self.assertRaisesRegex(ValueError, "长度"):
            parse_server_address(over_255_bytes, 25565)


if __name__ == "__main__":
    unittest.main()
