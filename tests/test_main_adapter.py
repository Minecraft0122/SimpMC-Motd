from __future__ import annotations

import asyncio
import importlib.util
import inspect
import json
import re
import sqlite3
import sys
import tempfile
import threading
import unittest
from contextlib import closing
from pathlib import Path
from types import ModuleType
from typing import Any
from unittest.mock import AsyncMock, patch

from simpmc_motd.models import BackgroundRenderImage, MinecraftStatus, ServerTarget
from simpmc_motd.storage import HistoryStore

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]


def _path_exists(path: Path) -> bool:
    return path.exists()


class _LoggerStub:
    def __init__(self) -> None:
        self.info_messages: list[str] = []
        self.warning_messages: list[str] = []
        self.exception_messages: list[str] = []

    def info(self, message: str) -> None:
        self.info_messages.append(message)

    def warning(self, message: str) -> None:
        self.warning_messages.append(message)

    def exception(self, message: str) -> None:
        self.exception_messages.append(message)


class _FilterStub:
    def __init__(self) -> None:
        self.commands: list[tuple[str, str]] = []
        self.regexes: list[tuple[str, str]] = []

    def command(self, name: str):
        def decorate(function):
            self.commands.append((name, function.__name__))
            return function

        return decorate

    def regex(self, pattern: str):
        def decorate(function):
            self.regexes.append((pattern, function.__name__))
            return function

        return decorate


class _ContextStub:
    def __init__(self) -> None:
        self.web_apis: list[tuple[str, Any, list[str], str]] = []

    def register_web_api(
        self,
        route: str,
        handler: Any,
        methods: list[str],
        description: str,
    ) -> None:
        for index, (registered_route, _handler, registered_methods, _description) in enumerate(
            self.web_apis
        ):
            if route == registered_route and methods == registered_methods:
                self.web_apis[index] = (route, handler, methods, description)
                return
        self.web_apis.append((route, handler, methods, description))


class _ConfigStub(dict):
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.save_count = 0
        self.save_error: Exception | None = None

    async def save_config_async(self, replace_config: dict[str, Any] | None = None) -> bool:
        self.save_count += 1
        if replace_config:
            self.update(replace_config)
        await asyncio.sleep(0)
        if self.save_error is not None:
            raise self.save_error
        return True


class _WebRequestStub:
    def __init__(self) -> None:
        self.payload: Any = None
        self.raw_body: bytes | None = None
        self.username: str | None = "console-admin"
        self.headers: dict[str, str] = {}

    async def json(self, default: Any = None) -> Any:
        return self.payload if self.payload is not None else default

    async def body(self) -> bytes:
        if self.raw_body is not None:
            return self.raw_body
        return json.dumps(self.payload, ensure_ascii=False).encode("utf-8")


class _StarStub:
    def __init__(self, context: Any) -> None:
        self.context = context

    async def html_render(self, *args: Any, **kwargs: Any) -> str:
        raise AssertionError("html_render must be replaced by the adapter smoke test")


class _EventStub:
    def __init__(
        self,
        *,
        group_id: str | None = "42",
        admin: bool = True,
        platform_id: str = "test-platform",
        session_id: str = "session-1",
    ) -> None:
        self._group_id = group_id
        self._admin = admin
        self._platform_id = platform_id
        self._session_id = session_id
        self.unified_msg_origin = f"origin:{session_id}"
        self.stopped = False

    def get_group_id(self) -> str | None:
        return self._group_id

    def get_platform_id(self) -> str:
        return self._platform_id

    def get_platform_name(self) -> str:
        return "test"

    def get_session_id(self) -> str:
        return self._session_id

    def is_admin(self) -> bool:
        return self._admin

    def stop_event(self) -> None:
        self.stopped = True

    @staticmethod
    def plain_result(text: str) -> tuple[str, str]:
        return "plain", text

    @staticmethod
    def image_result(url: str) -> tuple[str, str]:
        return "image", url


async def _collect(generator) -> list[tuple[str, str]]:
    return [item async for item in generator]


class MainAdapterSmokeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.data_root = Path(self.temporary_directory.name)
        self.logger = _LoggerStub()
        self.filter = _FilterStub()
        self.web_request = _WebRequestStub()
        self._saved_modules: dict[str, ModuleType | None] = {}
        self._plugins: list[Any] = []
        self._install_astrbot_stubs()

        self.module_name = f"_simpmc_motd_main_smoke_{id(self)}"
        main_path = REPOSITORY_ROOT / "main.py"
        spec = importlib.util.spec_from_file_location(self.module_name, main_path)
        if spec is None or spec.loader is None:
            self.fail("cannot create an import spec for main.py")
        module = importlib.util.module_from_spec(spec)
        self._remember_module(self.module_name, module)
        spec.loader.exec_module(module)
        self.main = module

        self.network_guard = patch(
            "simpmc_motd.rendering.background._open_public_response",
            side_effect=AssertionError("adapter smoke tests must not access the network"),
        )
        self.network_guard.start()

    async def asyncTearDown(self) -> None:
        for plugin in reversed(self._plugins):
            await plugin.terminate()
        self.network_guard.stop()
        for name, previous in reversed(tuple(self._saved_modules.items())):
            if previous is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = previous
        self.temporary_directory.cleanup()

    def _remember_module(self, name: str, module: ModuleType) -> None:
        if name not in self._saved_modules:
            self._saved_modules[name] = sys.modules.get(name)
        sys.modules[name] = module

    @staticmethod
    def _package(name: str) -> ModuleType:
        module = ModuleType(name)
        module.__path__ = []  # type: ignore[attr-defined]
        return module

    def _install_astrbot_stubs(self) -> None:
        astrbot = self._package("astrbot")
        api = self._package("astrbot.api")
        event = ModuleType("astrbot.api.event")
        star = ModuleType("astrbot.api.star")
        web = ModuleType("astrbot.api.web")
        core = self._package("astrbot.core")
        utils = self._package("astrbot.core.utils")
        path_module = ModuleType("astrbot.core.utils.astrbot_path")

        api.AstrBotConfig = dict
        api.logger = self.logger
        event.AstrMessageEvent = _EventStub
        event.filter = self.filter
        star.Context = _ContextStub
        star.Star = _StarStub
        web.request = self.web_request

        def json_response(data: Any = None, status_code: int = 200, **_kwargs):
            return {
                "status_code": status_code,
                "body": {} if data is None else data,
            }

        def error_response(message: str, status_code: int = 400, data: Any = None, **_kwargs):
            return {
                "status_code": status_code,
                "body": {"status": "error", "message": message, "data": data},
            }

        web.json_response = json_response
        web.error_response = error_response
        path_module.get_astrbot_data_path = lambda: str(self.data_root)

        astrbot.api = api
        astrbot.core = core
        api.event = event
        api.star = star
        api.web = web
        core.utils = utils
        utils.astrbot_path = path_module

        for name, module in (
            ("astrbot", astrbot),
            ("astrbot.api", api),
            ("astrbot.api.event", event),
            ("astrbot.api.star", star),
            ("astrbot.api.web", web),
            ("astrbot.core", core),
            ("astrbot.core.utils", utils),
            ("astrbot.core.utils.astrbot_path", path_module),
        ):
            self._remember_module(name, module)

    async def _make_plugin(self, config: dict[str, Any] | None = None):
        plugin_config = config if isinstance(config, _ConfigStub) else _ConfigStub(config or {})
        plugin = self.main.MinecraftMotdPlugin(_ContextStub(), plugin_config)
        self._plugins.append(plugin)
        await plugin.initialize()
        if plugin._legacy_migration_task is not None:
            await plugin._legacy_migration_task
        return plugin

    @staticmethod
    def _target() -> ServerTarget:
        return ServerTarget(
            scope_id="test-platform:group:42",
            scope_label="群 42",
            server_name="测试服",
            host="private.example",
            port=25570,
            configured=True,
        )

    @staticmethod
    def _status(*, ok: bool = True) -> MinecraftStatus:
        return MinecraftStatus(
            ok=ok,
            sampled_at=1_700_000_000.0,
            host="private.example",
            port=25570,
            online=3 if ok else None,
            max_players=10 if ok else None,
            motd_plain="Hello SimpMC" if ok else "",
            version_name="1.21",
            error="connection refused" if not ok else "",
            raw_json={"description": {"text": "Hello SimpMC"}} if ok else None,
        )

    def _prepare_motd_command(
        self,
        plugin: Any,
        *,
        target: ServerTarget,
        current: MinecraftStatus,
        background_warning: str = "",
    ) -> None:
        plugin._ensure_collector = AsyncMock(return_value=True)
        plugin._target_for_event = AsyncMock(return_value=target)
        plugin._current_status = AsyncMock(return_value=current)
        plugin.store.load_history = AsyncMock(
            return_value=[
                {
                    "sampled_at": current.sampled_at - 60,
                    "success": 1,
                    "online": 2,
                    "max_players": 10,
                    "latency_ms": None,
                },
                {
                    "sampled_at": current.sampled_at,
                    "success": 1,
                    "online": 3,
                    "max_players": 10,
                    "latency_ms": None,
                },
            ]
        )
        plugin.backgrounds.get = AsyncMock(
            return_value=BackgroundRenderImage(
                image_url="data:image/png;base64,local-test-image",
                warning=background_warning,
            )
        )

    async def test_direct_import_registers_regex_and_settings_page_apis(self) -> None:
        self.assertTrue(issubclass(self.main.MinecraftMotdPlugin, _StarStub))
        self.assertEqual([], self.filter.commands)
        self.assertEqual([(r"^/?motd$", "motd")], self.filter.regexes)
        matcher = re.compile(self.filter.regexes[0][0])
        for message in ("motd", "/motd", " motd "):
            self.assertIsNotNone(matcher.search(message.strip()), message)
        for message in ("MOTD", "/MOTD", "//motd", "motd now", "am motd"):
            self.assertIsNone(matcher.search(message.strip()), message)
        self.assertTrue(inspect.isasyncgenfunction(self.main.MinecraftMotdPlugin.motd))
        self.assertFalse(hasattr(self.main.MinecraftMotdPlugin, "setmotd"))
        self.assertFalse(hasattr(self.main.MinecraftMotdPlugin, "clearmotd"))

        plugin = await self._make_plugin({"background_image_url": " "})
        self.assertEqual(
            [
                ("/simpmc_motd/settings", ["GET"]),
                ("/simpmc_motd/settings/save", ["POST"]),
            ],
            [(route, methods) for route, _handler, methods, _desc in plugin.context.web_apis],
        )
        database = self.data_root / "plugin_data" / "SimpMC-Motd" / "history.sqlite3"
        self.assertTrue(database.is_file())
        self.assertIn("<!doctype html>", plugin.template)
        self.assertFalse(_path_exists(REPOSITORY_ROOT / "data"))
        metadata = (REPOSITORY_ROOT / "metadata.yaml").read_text(encoding="utf-8")
        self.assertIn("version: v2.0.0", metadata)
        self.assertIn('astrbot_version: ">=4.27.0"', metadata)

    async def test_get_web_settings_returns_plain_business_payload(self) -> None:
        plugin = await self._make_plugin({"background_image_url": " "})

        response = await plugin.get_web_settings()

        self.assertEqual(200, response["status_code"])
        self.assertEqual("v2.0.0", response["body"]["version"])
        self.assertIn("server_name", response["body"]["settings"])
        self.assertIn("group_servers", response["body"]["settings"])
        self.assertNotIn("status", response["body"])

    async def test_database_setup_runs_in_worker_during_initialize(self) -> None:
        database = self.data_root / "plugin_data" / "SimpMC-Motd" / "history.sqlite3"
        plugin = self.main.MinecraftMotdPlugin(
            _ContextStub(),
            {"background_image_url": " "},
        )
        self._plugins.append(plugin)
        self.assertFalse(database.exists(), "构造器不应同步执行 SQLite 建表")

        event_loop_thread = threading.get_ident()
        worker_threads: list[int] = []
        original_prepare = plugin._prepare_store

        def tracked_prepare():
            worker_threads.append(threading.get_ident())
            return original_prepare()

        with patch.object(plugin, "_prepare_store", side_effect=tracked_prepare):
            await plugin.initialize()
            await plugin.initialize()

        self.assertTrue(database.is_file())
        self.assertEqual(1, len(worker_threads), "初始化门闩应合并重复调用")
        self.assertNotEqual(event_loop_thread, worker_threads[0])

    async def test_cancelled_initialize_harvests_worker_and_closes_services(self) -> None:
        plugin = self.main.MinecraftMotdPlugin(
            _ContextStub(),
            {"background_image_url": " "},
        )
        self._plugins.append(plugin)
        prepare_entered = threading.Event()
        release_prepare = threading.Event()
        original_prepare = plugin._prepare_store

        def blocked_prepare():
            prepare_entered.set()
            if not release_prepare.wait(timeout=2.0):
                raise TimeoutError("test did not release database initialization")
            return original_prepare()

        try:
            with patch.object(plugin, "_prepare_store", side_effect=blocked_prepare):
                initialization = asyncio.create_task(plugin.initialize())
                self.assertTrue(await asyncio.to_thread(prepare_entered.wait, 1.0))
                initialization.cancel()
                for _attempt in range(20):
                    if plugin._closing:
                        break
                    await asyncio.sleep(0)
                self.assertTrue(plugin._closing)
                self.assertFalse(initialization.done())
                release_prepare.set()
                with self.assertRaises(asyncio.CancelledError):
                    await asyncio.wait_for(initialization, timeout=1.0)
        finally:
            release_prepare.set()

        self.assertTrue(plugin._terminated)
        self.assertIsNotNone(plugin.collector)
        self.assertTrue(plugin.collector.closed)
        self.assertFalse(plugin.collector.running)

    async def test_terminate_before_initialize_blocks_late_service_creation(self) -> None:
        plugin = self.main.MinecraftMotdPlugin(
            _ContextStub(),
            {"background_image_url": " "},
        )
        self._plugins.append(plugin)

        await plugin.terminate()

        with self.assertRaises(self.main._PluginClosingError):
            await plugin.initialize()
        self.assertIsNone(plugin._runtime_loop)
        with self.assertRaises(self.main._PluginClosingError):
            await plugin._ensure_ready()
        self.assertTrue(plugin._terminated)
        self.assertIsNone(plugin.store)
        self.assertIsNone(plugin.collector)

    async def test_failed_legacy_backup_keeps_using_source_and_allows_retry(self) -> None:
        legacy = self.data_root / "plugin_data" / "astrbot_plugin_mc_motd" / "history.sqlite3"
        destination = self.data_root / "plugin_data" / "SimpMC-Motd" / "history.sqlite3"
        legacy.parent.mkdir(parents=True)
        with closing(sqlite3.connect(legacy)) as connection, connection:
            connection.execute("CREATE TABLE marker (value TEXT NOT NULL)")
            connection.execute("INSERT INTO marker (value) VALUES ('preserved')")

        with patch.object(
            self.main,
            "migrate_legacy_database",
            side_effect=OSError("temporary backup failure"),
        ):
            plugin = await self._make_plugin({"background_image_url": " "})

        self.assertEqual(legacy, plugin.store.db_path)
        self.assertFalse(destination.exists())
        with closing(sqlite3.connect(legacy)) as connection:
            value = connection.execute("SELECT value FROM marker").fetchone()[0]
        self.assertEqual("preserved", value)
        self.assertTrue(any("下次启动重试" in message for message in self.logger.warning_messages))

    async def test_failed_existing_database_merge_keeps_destination_authoritative(self) -> None:
        legacy = self.data_root / "plugin_data" / "astrbot_plugin_mc_motd" / "history.sqlite3"
        destination = self.data_root / "plugin_data" / "SimpMC-Motd" / "history.sqlite3"
        legacy.parent.mkdir(parents=True)
        destination.parent.mkdir(parents=True)
        for path, value in ((legacy, "legacy"), (destination, "destination")):
            with closing(sqlite3.connect(path)) as connection, connection:
                connection.execute("CREATE TABLE marker (value TEXT NOT NULL)")
                connection.execute("INSERT INTO marker (value) VALUES (?)", (value,))

        with patch.object(
            self.main,
            "migrate_legacy_database",
            side_effect=OSError("temporary merge failure"),
        ):
            plugin = await self._make_plugin({"background_image_url": " "})

        self.assertEqual(destination, plugin.store.db_path)
        with closing(sqlite3.connect(destination)) as connection:
            value = connection.execute("SELECT value FROM marker").fetchone()[0]
        self.assertEqual("destination", value)
        self.assertTrue(
            any("继续使用现有新数据库" in message for message in self.logger.warning_messages)
        )

    async def test_v1_group_binding_is_migrated_to_console_config_once(self) -> None:
        database = self.data_root / "plugin_data" / "SimpMC-Motd" / "history.sqlite3"
        legacy_store = HistoryStore(database)
        legacy_target = ServerTarget(
            scope_id="test-platform:group:42",
            scope_label="群 42",
            server_name="旧绑定服",
            host="legacy.example",
            port=25570,
            configured=True,
        )
        await legacy_store.upsert_server(legacy_target)
        config = _ConfigStub({"background_image_url": " "})

        plugin = await self._make_plugin(config)

        groups = json.loads(config["group_servers_json"])
        self.assertEqual(
            {
                "address": "legacy.example:25570",
                "name": "旧绑定服",
            },
            groups["test-platform:group:42"],
        )
        self.assertEqual(1, config.save_count)
        row = await plugin.store.get_server("test-platform:group:42")
        self.assertEqual(0, row["configured"])
        resolved = await plugin.targets.resolve(
            "test-platform:group:42",
            "群 42",
            "42",
            False,
        )
        self.assertEqual("legacy.example", resolved.host)
        self.assertTrue(resolved.configured)

    async def test_failed_v1_binding_config_save_keeps_legacy_row_for_retry(self) -> None:
        database = self.data_root / "plugin_data" / "SimpMC-Motd" / "history.sqlite3"
        legacy_store = HistoryStore(database)
        legacy_target = ServerTarget(
            scope_id="test-platform:group:42",
            scope_label="群 42",
            server_name="待迁移服",
            host="retry.example",
            port=25570,
            configured=True,
        )
        await legacy_store.upsert_server(legacy_target)
        config = _ConfigStub({"background_image_url": " "})
        plugin = self.main.MinecraftMotdPlugin(_ContextStub(), config)
        self._plugins.append(plugin)
        collector_states_during_save: list[bool] = []

        async def failed_save(replace_config: dict[str, Any] | None = None) -> bool:
            config.save_count += 1
            if replace_config:
                config.update(replace_config)
            collector_states_during_save.append(plugin.collector.running)
            await asyncio.sleep(0)
            raise OSError("temporary config failure")

        config.save_config_async = failed_save
        await plugin.initialize()
        self.assertIsNotNone(plugin._legacy_migration_task)
        await plugin._legacy_migration_task

        self.assertNotIn("group_servers_json", config)
        self.assertEqual([False], collector_states_during_save)
        self.assertTrue(plugin.collector.running)
        row = await plugin.store.get_server("test-platform:group:42")
        self.assertEqual(1, row["configured"])
        self.assertTrue(any("下次启动重试" in item for item in self.logger.warning_messages))

    async def test_v1_private_binding_is_retired_without_console_import(self) -> None:
        database = self.data_root / "plugin_data" / "SimpMC-Motd" / "history.sqlite3"
        legacy_store = HistoryStore(database)
        legacy_target = ServerTarget(
            scope_id="test-platform:private:alice",
            scope_label="私聊 alice",
            server_name="旧私聊绑定",
            host="private-binding.example",
            port=25570,
            configured=True,
        )
        await legacy_store.upsert_server(legacy_target)
        config = _ConfigStub({"background_image_url": " "})

        plugin = await self._make_plugin(config)

        self.assertNotIn("group_servers_json", config)
        self.assertEqual(0, config.save_count)
        row = await plugin.store.get_server("test-platform:private:alice")
        self.assertEqual(0, row["configured"])
        resolved = await plugin.targets.resolve(
            "test-platform:private:alice",
            "私聊 alice",
            None,
            True,
        )
        self.assertEqual("127.0.0.1", resolved.host)

    async def test_termination_gate_prevents_collector_resurrection(self) -> None:
        plugin = await self._make_plugin({"background_image_url": " "})
        collector = plugin.collector
        self.assertIsNotNone(collector)
        self.assertTrue(collector.running)

        close_entered = asyncio.Event()
        release_close = asyncio.Event()
        original_close = collector.close

        async def blocked_close() -> None:
            close_entered.set()
            await release_close.wait()
            await original_close()

        with patch.object(collector, "close", side_effect=blocked_close):
            termination = asyncio.create_task(plugin.terminate())
            await asyncio.wait_for(close_entered.wait(), timeout=1.0)
            self.assertFalse(await plugin._ensure_collector())
            release_close.set()
            await asyncio.wait_for(termination, timeout=1.0)

        await collector.ensure_started()
        self.assertTrue(collector.closed)
        self.assertFalse(collector.running)
        self.assertTrue(plugin._terminated)

    async def test_duplicate_plugin_instance_retires_previous_collector(self) -> None:
        first = self.main.MinecraftMotdPlugin(
            _ContextStub(),
            {"background_image_url": " "},
        )
        second = self.main.MinecraftMotdPlugin(
            _ContextStub(),
            {"background_image_url": " "},
        )
        self._plugins.extend((first, second))

        await asyncio.gather(first.initialize(), second.initialize())
        await asyncio.sleep(0)

        collectors = (first.collector, second.collector)
        self.assertTrue(all(collector is not None for collector in collectors))
        winner = next(collector for collector in collectors if collector.running)
        loser = next(collector for collector in collectors if collector.closed)
        self.assertIsNot(winner, loser)
        self.assertFalse(loser.running)

        losing_plugin = first if first.collector is loser else second
        await losing_plugin.terminate()
        self.assertTrue(winner.running)

    async def test_failed_replacement_keeps_healthy_runtime_owner(self) -> None:
        healthy = await self._make_plugin({"background_image_url": " "})
        replacement_context = _ContextStub()
        replacement = self.main.MinecraftMotdPlugin(
            replacement_context,
            _ConfigStub({"background_image_url": " "}),
        )
        self._plugins.append(replacement)

        with (
            patch.object(
                replacement,
                "_prepare_store",
                side_effect=OSError("database unavailable"),
            ),
            self.assertRaisesRegex(OSError, "database unavailable"),
        ):
            await replacement.initialize()

        self.assertTrue(healthy.collector.running)
        self.assertTrue(healthy._owns_runtime_slot())
        self.assertFalse(replacement._owns_runtime_slot())
        self.assertEqual([], replacement_context.web_apis)
        self.assertEqual(2, len(healthy.context.web_apis))

    async def test_ready_fallback_candidate_takes_over_when_newer_candidate_fails(
        self,
    ) -> None:
        older = self.main.MinecraftMotdPlugin(
            _ContextStub(),
            _ConfigStub({"background_image_url": " "}),
        )
        newer = self.main.MinecraftMotdPlugin(
            _ContextStub(),
            _ConfigStub({"background_image_url": " "}),
        )
        self._plugins.extend((older, newer))
        older_prepare_entered = threading.Event()
        newer_prepare_entered = threading.Event()
        release_older = threading.Event()
        release_newer = threading.Event()
        original_older_prepare = older._prepare_store

        def blocked_older_prepare():
            older_prepare_entered.set()
            if not release_older.wait(timeout=2.0):
                raise TimeoutError("test did not release older candidate")
            return original_older_prepare()

        def failed_newer_prepare():
            newer_prepare_entered.set()
            if not release_newer.wait(timeout=2.0):
                raise TimeoutError("test did not release newer candidate")
            raise OSError("newer preparation failed")

        try:
            with (
                patch.object(older, "_prepare_store", side_effect=blocked_older_prepare),
                patch.object(newer, "_prepare_store", side_effect=failed_newer_prepare),
            ):
                older_initialization = asyncio.create_task(older.initialize())
                self.assertTrue(await asyncio.to_thread(older_prepare_entered.wait, 1.0))
                newer_initialization = asyncio.create_task(newer.initialize())
                self.assertTrue(await asyncio.to_thread(newer_prepare_entered.wait, 1.0))
                release_older.set()
                for _attempt in range(100):
                    if older.collector is not None:
                        break
                    await asyncio.sleep(0.01)
                self.assertIsNotNone(older.collector)
                self.assertFalse(older_initialization.done())

                early_event = _EventStub()
                self.assertEqual([], await _collect(older.motd(early_event)))
                self.assertFalse(early_event.stopped)
                self.assertFalse(older.collector.closed)

                release_newer.set()
                with self.assertRaisesRegex(OSError, "newer preparation failed"):
                    await asyncio.wait_for(newer_initialization, timeout=1.0)
                await asyncio.wait_for(older_initialization, timeout=1.0)
        finally:
            release_older.set()
            release_newer.set()

        self.assertTrue(older._owns_runtime_slot())
        self.assertTrue(older.collector.running)
        self.assertFalse(newer._owns_runtime_slot())

    async def test_web_api_registration_failure_restores_healthy_owner_routes(self) -> None:
        shared_context = _ContextStub()
        healthy = self.main.MinecraftMotdPlugin(
            shared_context,
            _ConfigStub({"background_image_url": " "}),
        )
        replacement = self.main.MinecraftMotdPlugin(
            shared_context,
            _ConfigStub({"background_image_url": " "}),
        )
        self._plugins.extend((healthy, replacement))
        await healthy.initialize()
        original_register = shared_context.register_web_api
        failed = False

        def fail_replacement_post(route, handler, methods, description):
            nonlocal failed
            if (
                not failed
                and route.endswith("/settings/save")
                and getattr(handler, "__self__", None) is replacement
            ):
                failed = True
                raise RuntimeError("route registration failed")
            return original_register(route, handler, methods, description)

        with (
            patch.object(shared_context, "register_web_api", side_effect=fail_replacement_post),
            self.assertRaisesRegex(RuntimeError, "route registration failed"),
        ):
            await replacement.initialize()

        self.assertTrue(healthy._owns_runtime_slot())
        self.assertTrue(healthy.collector.running)
        self.assertFalse(replacement._owns_runtime_slot())
        self.assertEqual(2, len(shared_context.web_apis))
        self.assertTrue(
            all(
                getattr(handler, "__self__", None) is healthy
                for _, handler, _, _ in shared_context.web_apis
            )
        )

    async def test_collector_start_failure_keeps_healthy_runtime_owner(self) -> None:
        shared_context = _ContextStub()
        healthy = self.main.MinecraftMotdPlugin(
            shared_context,
            _ConfigStub({"background_image_url": " "}),
        )
        replacement = self.main.MinecraftMotdPlugin(
            shared_context,
            _ConfigStub({"background_image_url": " "}),
        )
        self._plugins.extend((healthy, replacement))
        await healthy.initialize()

        with (
            patch.object(self.main.StatusCollector, "start", return_value=False),
            self.assertRaisesRegex(RuntimeError, "could not be started"),
        ):
            await replacement.initialize()

        self.assertTrue(healthy._owns_runtime_slot())
        self.assertTrue(healthy.collector.running)
        self.assertFalse(replacement._owns_runtime_slot())
        self.assertTrue(
            all(
                getattr(handler, "__self__", None) is healthy
                for _, handler, _, _ in shared_context.web_apis
            )
        )

    async def test_post_commit_migration_does_not_block_initialize_completion(self) -> None:
        healthy = await self._make_plugin({"background_image_url": " "})
        replacement = self.main.MinecraftMotdPlugin(
            _ContextStub(),
            _ConfigStub({"background_image_url": " "}),
        )
        self._plugins.append(replacement)
        migration_entered = asyncio.Event()
        release_migration = asyncio.Event()

        async def blocked_migration() -> None:
            migration_entered.set()
            await release_migration.wait()

        with patch.object(
            replacement,
            "_migrate_legacy_command_bindings",
            side_effect=blocked_migration,
        ):
            try:
                initialization = asyncio.create_task(replacement.initialize())
                await asyncio.wait_for(initialization, timeout=1.0)
                await asyncio.wait_for(migration_entered.wait(), timeout=1.0)
                self.assertTrue(replacement._owns_runtime_slot())
                self.assertTrue(replacement.collector.running)
                self.assertTrue(healthy._superseded)
            finally:
                release_migration.set()
            await asyncio.wait_for(replacement._legacy_migration_task, timeout=1.0)

    async def test_runtime_takeover_waits_for_old_owner_settings_transaction(self) -> None:
        config = _ConfigStub({"background_image_url": " ", "server_name": "旧名称"})
        old = await self._make_plugin(config)
        payload = self.main.serialize_console_settings(old.settings)
        payload["server_name"] = "接管前完成的保存"
        self.web_request.payload = {"settings": payload}
        save_entered = asyncio.Event()
        release_save = asyncio.Event()

        async def blocked_save(replace_config: dict[str, Any] | None = None) -> bool:
            config.save_count += 1
            if replace_config:
                config.update(replace_config)
            save_entered.set()
            await release_save.wait()
            return True

        config.save_config_async = blocked_save
        save_task = asyncio.create_task(old.save_web_settings())
        try:
            await asyncio.wait_for(save_entered.wait(), timeout=1.0)
            replacement = self.main.MinecraftMotdPlugin(_ContextStub(), config)
            self._plugins.append(replacement)
            initialization = asyncio.create_task(replacement.initialize())
            for _attempt in range(100):
                if replacement.collector is not None:
                    break
                await asyncio.sleep(0.01)

            self.assertIsNotNone(replacement.collector)
            self.assertFalse(initialization.done())
            self.assertTrue(old._owns_runtime_slot())
        finally:
            release_save.set()

        save_response = await asyncio.wait_for(save_task, timeout=1.0)
        await asyncio.wait_for(initialization, timeout=1.0)

        self.assertEqual(200, save_response["status_code"])
        self.assertEqual("接管前完成的保存", config["server_name"])
        self.assertTrue(replacement._owns_runtime_slot())
        self.assertTrue(replacement.collector.running)
        self.assertTrue(old._superseded)
        self.assertEqual(503, (await old.save_web_settings())["status_code"])

    async def test_terminated_old_instance_cannot_displace_healthy_owner(self) -> None:
        old = self.main.MinecraftMotdPlugin(
            _ContextStub(),
            _ConfigStub({"background_image_url": " "}),
        )
        self._plugins.append(old)
        await old.terminate()
        healthy = await self._make_plugin({"background_image_url": " "})

        with self.assertRaises(self.main._PluginClosingError):
            await old.initialize()

        self.assertTrue(healthy.collector.running)
        self.assertTrue(healthy._owns_runtime_slot())
        self.assertFalse(old._owns_runtime_slot())

    async def test_slow_older_initialize_cannot_replace_newer_runtime_owner(self) -> None:
        first = self.main.MinecraftMotdPlugin(
            _ContextStub(),
            {"background_image_url": " "},
        )
        second = self.main.MinecraftMotdPlugin(
            _ContextStub(),
            {"background_image_url": " "},
        )
        self._plugins.extend((first, second))
        first_prepare_entered = threading.Event()
        release_first_prepare = threading.Event()
        original_first_prepare = first._prepare_store

        def blocked_first_prepare():
            first_prepare_entered.set()
            if not release_first_prepare.wait(timeout=2.0):
                raise TimeoutError("test did not release the older initialization")
            return original_first_prepare()

        try:
            with patch.object(first, "_prepare_store", side_effect=blocked_first_prepare):
                first_initialization = asyncio.create_task(first.initialize())
                self.assertTrue(
                    await asyncio.to_thread(first_prepare_entered.wait, 1.0),
                )
                await second.initialize()
                release_first_prepare.set()
                await asyncio.wait_for(first_initialization, timeout=1.0)
        finally:
            release_first_prepare.set()

        self.assertTrue(second.collector.running)
        self.assertTrue(await second._ensure_collector())
        self.assertTrue(first.collector.closed)
        self.assertFalse(first.collector.running)

        await second.terminate()
        self.assertFalse(first.collector.running)

    async def test_cancelled_terminate_finishes_service_close_before_propagating(self) -> None:
        plugin = await self._make_plugin({"background_image_url": " "})
        collector = plugin.collector
        self.assertIsNotNone(collector)
        close_entered = asyncio.Event()
        release_close = asyncio.Event()
        original_close = collector.close

        async def blocked_close() -> None:
            close_entered.set()
            await release_close.wait()
            await original_close()

        with patch.object(collector, "close", side_effect=blocked_close):
            termination = asyncio.create_task(plugin.terminate())
            await asyncio.wait_for(close_entered.wait(), timeout=1.0)
            termination.cancel()
            await asyncio.sleep(0)
            self.assertFalse(termination.done())
            release_close.set()
            with self.assertRaises(asyncio.CancelledError):
                await asyncio.wait_for(termination, timeout=1.0)

        self.assertTrue(plugin._terminated)
        self.assertTrue(collector.closed)
        self.assertFalse(collector.running)

    async def test_motd_command_preserves_render_privacy_and_warning_contract(self) -> None:
        plugin = await self._make_plugin(
            {
                "background_image_url": " ",
                "render_cache_seconds": 0,
            }
        )
        target = self._target()
        current = self._status()
        self._prepare_motd_command(
            plugin,
            target=target,
            current=current,
            background_warning="背景使用了本地回退。",
        )
        plugin.html_render = AsyncMock(return_value="render://status.png")

        event = _EventStub()
        results = await _collect(plugin.motd(event))

        self.assertEqual(
            [
                ("image", "render://status.png"),
                ("plain", "背景使用了本地回退。"),
            ],
            results,
        )
        self.assertTrue(event.stopped)
        plugin._current_status.assert_awaited_once_with(target, allow_reuse=True)
        plugin.store.load_history.assert_awaited_once_with(
            target.scope_id,
            target.host,
            target.port,
            plugin.settings.chart_hours,
        )

        render_call = plugin.html_render.await_args
        self.assertIsNotNone(render_call)
        template, data = render_call.args
        self.assertEqual(plugin.template, template)
        self.assertNotIn("host", data)
        self.assertNotIn("port", data)
        self.assertNotIn("background_warning", data)
        self.assertNotIn(target.host, repr(data))
        self.assertNotIn(str(target.port), repr(data))
        self.assertTrue(render_call.kwargs["return_url"])
        self.assertEqual(
            {"x": 0, "y": 0, "width": 790, "height": 500},
            render_call.kwargs["options"]["clip"],
        )

    async def test_render_exception_yields_plain_status_fallback(self) -> None:
        plugin = await self._make_plugin(
            {
                "background_image_url": " ",
                "render_cache_seconds": 0,
            }
        )
        target = self._target()
        current = self._status()
        self._prepare_motd_command(plugin, target=target, current=current)
        plugin.html_render = AsyncMock(side_effect=RuntimeError("renderer unavailable"))

        results = await _collect(plugin.motd(_EventStub()))

        self.assertEqual(1, len(results))
        result_type, text = results[0]
        self.assertEqual("plain", result_type)
        self.assertIn("测试服 当前在线：3/10", text)
        self.assertIn("地址：private.example:25570", text)
        self.assertIn("MOTD：Hello SimpMC", text)
        self.assertIn("图片渲染失败（RuntimeError），已返回文字状态", text)
        self.assertNotIn("renderer unavailable", text)
        self.assertTrue(
            any("render status image failed" in item for item in self.logger.exception_messages)
        )

        render_call = plugin.html_render.await_args
        self.assertIsNotNone(render_call)
        data = render_call.args[1]
        self.assertNotIn("host", data)
        self.assertNotIn("port", data)
        self.assertNotIn("background_warning", data)

    async def test_query_exception_yields_generic_plain_fallback(self) -> None:
        plugin = await self._make_plugin(
            {
                "background_image_url": " ",
                "render_cache_seconds": 0,
            }
        )
        target = self._target()
        plugin._ensure_collector = AsyncMock(return_value=True)
        plugin._target_for_event = AsyncMock(return_value=target)
        plugin._current_status = AsyncMock(side_effect=OSError("query unavailable"))
        plugin.html_render = AsyncMock(return_value="must-not-render")

        results = await _collect(plugin.motd(_EventStub()))

        self.assertEqual(1, len(results))
        self.assertEqual("plain", results[0][0])
        self.assertIn("测试服 查询或渲染失败", results[0][1])
        self.assertIn("地址：private.example:25570", results[0][1])
        self.assertIn("图片渲染失败（OSError），已返回文字状态", results[0][1])
        self.assertNotIn("query unavailable", results[0][1])
        plugin.html_render.assert_not_awaited()

    async def test_settings_web_api_validates_saves_and_restarts_runtime(self) -> None:
        config = _ConfigStub({"background_image_url": " "})
        plugin = await self._make_plugin(config)
        payload = self.main.serialize_console_settings(plugin.settings)
        payload.update(
            {
                "server_name": "新默认服",
                "host": "new.example",
                "port": 25570,
                "max_parallel_queries": 7,
                "group_whitelist": "42",
                "group_servers": [
                    {
                        "scope": "test-platform:group:42",
                        "name": "测试服",
                        "address": "private.example:25571",
                    }
                ],
            }
        )
        self.web_request.payload = {"settings": payload}

        with (
            patch.object(plugin.collector, "stop", new=AsyncMock()) as stop,
            patch.object(
                plugin.collector,
                "ensure_started",
                new=AsyncMock(),
            ) as ensure_started,
            patch.object(plugin.status_service, "refresh_settings") as refresh,
        ):
            response = await plugin.save_web_settings()

        self.assertEqual(200, response["status_code"])
        self.assertTrue(response["body"]["saved"])
        self.assertTrue(response["body"]["applied"])
        self.assertEqual("新默认服", config["server_name"])
        self.assertEqual("new.example", config["host"])
        self.assertEqual(25570, config["port"])
        self.assertIn("test-platform:group:42", config["group_servers_json"])
        self.assertEqual(1, config.save_count)
        stop.assert_awaited_once()
        ensure_started.assert_awaited_once()
        refresh.assert_called_once_with()

        self.web_request.payload = {"settings": {**payload, "unknown": "value"}}
        rejected = await plugin.save_web_settings()
        self.assertEqual(400, rejected["status_code"])
        self.assertIn("未知设置字段", rejected["body"]["message"])
        self.assertEqual(1, config.save_count)

    async def test_settings_web_api_rejects_oversized_body_before_parsing(self) -> None:
        config = _ConfigStub({"background_image_url": " "})
        plugin = await self._make_plugin(config)
        self.web_request.payload = {
            "settings": self.main.serialize_console_settings(plugin.settings)
        }
        self.web_request.headers["Content-Length"] = str(self.main._MAX_SETTINGS_REQUEST_BYTES + 1)

        response = await plugin.save_web_settings()

        self.assertEqual(413, response["status_code"])
        self.assertIn("过大", response["body"]["message"])
        self.assertEqual(0, config.save_count)

    async def test_settings_web_api_checks_actual_body_size(self) -> None:
        config = _ConfigStub({"background_image_url": " "})
        plugin = await self._make_plugin(config)
        self.web_request.headers["Content-Length"] = "16"
        self.web_request.raw_body = b"x" * (self.main._MAX_SETTINGS_REQUEST_BYTES + 1)

        response = await plugin.save_web_settings()

        self.assertEqual(413, response["status_code"])
        self.assertIn("过大", response["body"]["message"])
        self.assertEqual(0, config.save_count)

    async def test_settings_restart_failure_is_visible_to_plugin_page(self) -> None:
        config = _ConfigStub({"background_image_url": " "})
        plugin = await self._make_plugin(config)
        payload = self.main.serialize_console_settings(plugin.settings)
        payload["server_name"] = "已持久化但待重载"
        self.web_request.payload = {"settings": payload}
        original_stop = plugin.collector.stop

        with (
            patch.object(
                plugin.collector,
                "stop",
                new=AsyncMock(side_effect=original_stop),
            ),
            patch.object(
                plugin.collector,
                "ensure_started",
                new=AsyncMock(side_effect=RuntimeError("restart failed")),
            ),
        ):
            response = await plugin.save_web_settings()

        self.assertEqual(200, response["status_code"])
        self.assertTrue(response["body"]["saved"])
        self.assertFalse(response["body"]["applied"])
        self.assertEqual("collector_restart_failed", response["body"]["warning_code"])
        self.assertEqual("已持久化但待重载", config["server_name"])

    async def test_settings_restart_retry_reports_final_recovery_success(self) -> None:
        config = _ConfigStub({"background_image_url": " "})
        plugin = await self._make_plugin(config)
        payload = self.main.serialize_console_settings(plugin.settings)
        payload["server_name"] = "自动恢复成功"
        self.web_request.payload = {"settings": payload}
        original_start = plugin.collector.ensure_started
        attempts = 0

        async def fail_once_then_start() -> None:
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise RuntimeError("first restart failed")
            await original_start()

        with patch.object(
            plugin.collector,
            "ensure_started",
            new=AsyncMock(side_effect=fail_once_then_start),
        ):
            response = await plugin.save_web_settings()

        self.assertEqual(200, response["status_code"])
        self.assertTrue(response["body"]["saved"])
        self.assertTrue(response["body"]["applied"])
        self.assertEqual("", response["body"]["warning_code"])
        self.assertEqual(2, attempts)
        self.assertTrue(plugin.collector.running)

    async def test_settings_save_recovers_an_unexpectedly_stopped_collector(self) -> None:
        config = _ConfigStub({"background_image_url": " "})
        plugin = await self._make_plugin(config)
        await plugin.collector.stop()
        self.assertFalse(plugin.collector.running)
        self.web_request.payload = {
            "settings": self.main.serialize_console_settings(plugin.settings)
        }

        response = await plugin.save_web_settings()

        self.assertEqual(200, response["status_code"])
        self.assertTrue(response["body"]["applied"])
        self.assertTrue(plugin.collector.running)

    async def test_cancelled_settings_request_finishes_shielded_transaction(self) -> None:
        config = _ConfigStub({"background_image_url": " ", "server_name": "旧名称"})
        plugin = await self._make_plugin(config)
        payload = self.main.serialize_console_settings(plugin.settings)
        payload["server_name"] = "取消后仍完整保存"
        self.web_request.payload = {"settings": payload}
        save_entered = asyncio.Event()
        release_save = asyncio.Event()

        async def blocked_save(replace_config: dict[str, Any] | None = None) -> bool:
            config.save_count += 1
            if replace_config:
                config.update(replace_config)
            save_entered.set()
            await release_save.wait()
            return True

        config.save_config_async = blocked_save
        with (
            patch.object(plugin.collector, "stop", new=AsyncMock()),
            patch.object(plugin.collector, "ensure_started", new=AsyncMock()),
        ):
            request_task = asyncio.create_task(plugin.save_web_settings())
            await asyncio.wait_for(save_entered.wait(), timeout=1.0)
            request_task.cancel()
            release_save.set()
            with self.assertRaises(asyncio.CancelledError):
                await asyncio.wait_for(request_task, timeout=1.0)

        self.assertEqual("取消后仍完整保存", config["server_name"])
        self.assertEqual(1, config.save_count)
        self.assertEqual(0, plugin._active_operations)

    async def test_concurrent_settings_transactions_are_serialized(self) -> None:
        config = _ConfigStub({"background_image_url": " "})
        plugin = await self._make_plugin(config)
        await plugin.collector.stop()
        active_saves = 0
        maximum_active_saves = 0

        async def tracked_save(replace_config: dict[str, Any] | None = None) -> bool:
            nonlocal active_saves, maximum_active_saves
            config.save_count += 1
            active_saves += 1
            maximum_active_saves = max(maximum_active_saves, active_saves)
            try:
                await asyncio.sleep(0.01)
                if replace_config:
                    config.update(replace_config)
                return True
            finally:
                active_saves -= 1

        config.save_config_async = tracked_save
        first = self.main.validate_console_settings(
            self.main.serialize_console_settings(plugin.settings)
        )
        second_payload = self.main.serialize_console_settings(plugin.settings)
        second_payload["server_name"] = "第二次保存"
        second = self.main.validate_console_settings(second_payload)

        await asyncio.gather(
            plugin._apply_web_settings(first),
            plugin._apply_web_settings(second),
        )

        self.assertEqual(1, maximum_active_saves)
        self.assertEqual(2, config.save_count)
        self.assertEqual("第二次保存", config["server_name"])

    async def test_settings_save_failure_rolls_back_in_memory_config(self) -> None:
        config = _ConfigStub({"background_image_url": " ", "server_name": "旧名称"})
        plugin = await self._make_plugin(config)
        payload = self.main.serialize_console_settings(plugin.settings)
        payload["server_name"] = "不能保存的新名称"
        self.web_request.payload = {"settings": payload}
        collector_states_during_save: list[bool] = []

        async def failed_save(replace_config: dict[str, Any] | None = None) -> bool:
            config.save_count += 1
            if replace_config:
                config.update(replace_config)
            collector_states_during_save.append(plugin.collector.running)
            await asyncio.sleep(0)
            raise OSError("disk full")

        config.save_config_async = failed_save

        response = await plugin.save_web_settings()

        self.assertEqual(500, response["status_code"])
        self.assertEqual("旧名称", config["server_name"])
        self.assertEqual([False], collector_states_during_save)
        self.assertTrue(plugin.collector.running)
        self.assertTrue(
            any("save WebUI settings failed" in item for item in self.logger.exception_messages)
        )

    async def test_failed_save_does_not_overwrite_newer_in_memory_change(self) -> None:
        config = _ConfigStub({"background_image_url": " ", "server_name": "旧名称"})
        plugin = await self._make_plugin(config)
        payload = self.main.serialize_console_settings(plugin.settings)
        payload["server_name"] = "本次失败的保存"
        self.web_request.payload = {"settings": payload}
        save_entered = asyncio.Event()
        release_save = asyncio.Event()

        async def failed_save(replace_config: dict[str, Any] | None = None) -> bool:
            if replace_config:
                config.update(replace_config)
            save_entered.set()
            await release_save.wait()
            raise OSError("disk full")

        config.save_config_async = failed_save
        request_task = asyncio.create_task(plugin.save_web_settings())
        await asyncio.wait_for(save_entered.wait(), timeout=1.0)
        config["server_name"] = "并发的新配置"
        release_save.set()

        response = await asyncio.wait_for(request_task, timeout=1.0)

        self.assertEqual(500, response["status_code"])
        self.assertEqual("并发的新配置", config["server_name"])

    async def test_superseded_save_preserves_the_newer_config_snapshot(self) -> None:
        config = _ConfigStub(
            {
                "background_image_url": " ",
                "server_name": "旧名称",
                "host": "old.example",
            }
        )
        plugin = await self._make_plugin(config)
        payload = self.main.serialize_console_settings(plugin.settings)
        payload["server_name"] = "被后续快照保留的名称"
        self.web_request.payload = {"settings": payload}

        async def superseded_save(
            replace_config: dict[str, Any] | None = None,
        ) -> bool:
            if replace_config:
                config.update(replace_config)
            # Simulate another AstrBot config writer basing its newer snapshot
            # on this one, then winning the optimistic save revision.
            config["host"] = "newer.example"
            return False

        config.save_config_async = superseded_save
        response = await plugin.save_web_settings()

        self.assertEqual(409, response["status_code"])
        self.assertEqual("被后续快照保留的名称", config["server_name"])
        self.assertEqual("newer.example", config["host"])
        self.assertTrue(plugin.collector.running)

    async def test_settings_get_waits_for_failed_save_rollback(self) -> None:
        config = _ConfigStub({"background_image_url": " ", "server_name": "已提交名称"})
        plugin = await self._make_plugin(config)
        payload = self.main.serialize_console_settings(plugin.settings)
        payload["server_name"] = "未提交名称"
        self.web_request.payload = {"settings": payload}
        save_entered = asyncio.Event()
        release_save = asyncio.Event()

        async def failed_save(replace_config: dict[str, Any] | None = None) -> bool:
            if replace_config:
                config.update(replace_config)
            save_entered.set()
            await release_save.wait()
            raise OSError("disk full")

        config.save_config_async = failed_save
        save_task = asyncio.create_task(plugin.save_web_settings())
        await asyncio.wait_for(save_entered.wait(), timeout=1.0)
        get_task = asyncio.create_task(plugin.get_web_settings())
        await asyncio.sleep(0)
        self.assertFalse(get_task.done())

        release_save.set()
        save_response, get_response = await asyncio.gather(save_task, get_task)

        self.assertEqual(500, save_response["status_code"])
        self.assertEqual("已提交名称", get_response["body"]["settings"]["server_name"])

    async def test_motd_keeps_one_committed_snapshot_during_settings_save(self) -> None:
        config = _ConfigStub(
            {
                "background_image_url": " ",
                "host": "old.example",
            }
        )
        plugin = await self._make_plugin(config)
        payload = self.main.serialize_console_settings(plugin.settings)
        payload["host"] = "uncommitted.example"
        self.web_request.payload = {"settings": payload}
        command_entered = asyncio.Event()
        release_command = asyncio.Event()
        save_entered = asyncio.Event()
        release_save = asyncio.Event()
        rendered_targets: list[ServerTarget] = []

        async def blocked_collector_check() -> bool:
            command_entered.set()
            await release_command.wait()
            return True

        async def capture_render(target: ServerTarget):
            rendered_targets.append(target)
            return "render://status.png", "", None

        async def failed_save(replace_config: dict[str, Any] | None = None) -> bool:
            if replace_config:
                config.update(replace_config)
            save_entered.set()
            await release_save.wait()
            raise OSError("disk full")

        config.save_config_async = failed_save
        plugin._ensure_collector = blocked_collector_check
        plugin._render_status = capture_render
        event = _EventStub()
        command_task = asyncio.create_task(_collect(plugin.motd(event)))
        save_task: asyncio.Task[Any] | None = None
        try:
            await asyncio.wait_for(command_entered.wait(), timeout=1.0)
            save_task = asyncio.create_task(plugin.save_web_settings())
            await asyncio.sleep(0.05)
            self.assertFalse(save_entered.is_set())

            release_command.set()
            command_results = await asyncio.wait_for(command_task, timeout=1.0)
            await asyncio.wait_for(save_entered.wait(), timeout=1.0)
            self.assertEqual("uncommitted.example", config["host"])
            release_save.set()
            save_response = await asyncio.wait_for(save_task, timeout=1.0)
        finally:
            release_command.set()
            release_save.set()
            if not command_task.done():
                command_task.cancel()
            if save_task is not None and not save_task.done():
                save_task.cancel()
            await asyncio.gather(
                *(task for task in (command_task, save_task) if task is not None),
                return_exceptions=True,
            )

        self.assertTrue(event.stopped)
        self.assertEqual([("image", "render://status.png")], command_results)
        self.assertEqual(1, len(rendered_targets))
        self.assertEqual("old.example", rendered_targets[0].host)
        self.assertEqual(500, save_response["status_code"])
        self.assertEqual("old.example", config["host"])

    async def test_motd_readers_remain_concurrent_without_a_settings_writer(self) -> None:
        plugin = await self._make_plugin({"background_image_url": " "})
        both_entered = asyncio.Event()
        release_commands = asyncio.Event()
        entered = 0

        async def blocked_collector_check() -> bool:
            nonlocal entered
            entered += 1
            if entered == 2:
                both_entered.set()
            await release_commands.wait()
            return True

        async def render(_target: ServerTarget):
            return "render://status.png", "", None

        plugin._ensure_collector = blocked_collector_check
        plugin._target_for_event = AsyncMock(return_value=self._target())
        plugin._render_status = render
        commands = [
            asyncio.create_task(_collect(plugin.motd(_EventStub(session_id=f"session-{index}"))))
            for index in range(2)
        ]
        try:
            await asyncio.wait_for(both_entered.wait(), timeout=1.0)
            release_commands.set()
            results = await asyncio.wait_for(asyncio.gather(*commands), timeout=1.0)
        finally:
            release_commands.set()
            for command in commands:
                if not command.done():
                    command.cancel()
            await asyncio.gather(*commands, return_exceptions=True)

        self.assertEqual(
            [[("image", "render://status.png")], [("image", "render://status.png")]],
            results,
        )
        self.assertEqual(0, plugin._active_settings_readers)

    async def test_settings_transaction_returns_its_committed_response_snapshot(self) -> None:
        config = _ConfigStub({"background_image_url": " ", "server_name": "初始名称"})
        plugin = await self._make_plugin(config)
        await plugin.collector.stop()
        first_payload = self.main.serialize_console_settings(plugin.settings)
        first_payload["server_name"] = "第一次保存"
        second_payload = dict(first_payload)
        second_payload["server_name"] = "第二次未完成保存"
        first = self.main.validate_console_settings(first_payload)
        second = self.main.validate_console_settings(second_payload)
        first_entered = asyncio.Event()
        release_first = asyncio.Event()
        second_entered = asyncio.Event()
        release_second = asyncio.Event()
        calls = 0

        async def staged_save(replace_config: dict[str, Any] | None = None) -> bool:
            nonlocal calls
            calls += 1
            if replace_config:
                config.update(replace_config)
            if calls == 1:
                first_entered.set()
                await release_first.wait()
            else:
                second_entered.set()
                await release_second.wait()
            return True

        config.save_config_async = staged_save
        first_task = asyncio.create_task(plugin._apply_web_settings(first))
        second_task: asyncio.Task[tuple[bool, str, dict[str, Any]]] | None = None
        try:
            await asyncio.wait_for(first_entered.wait(), timeout=1.0)
            second_task = asyncio.create_task(plugin._apply_web_settings(second))
            release_first.set()
            await asyncio.wait_for(second_entered.wait(), timeout=1.0)

            first_result = await asyncio.wait_for(first_task, timeout=1.0)
            self.assertEqual("第一次保存", first_result[2]["server_name"])
            self.assertEqual("第二次未完成保存", config["server_name"])

            release_second.set()
            second_result = await asyncio.wait_for(second_task, timeout=1.0)
        finally:
            release_first.set()
            release_second.set()
            tasks = tuple(task for task in (first_task, second_task) if task is not None)
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

        self.assertEqual("第二次未完成保存", second_result[2]["server_name"])
        self.assertEqual("第二次未完成保存", config["server_name"])

    async def test_collector_target_snapshot_blocks_settings_mutation(self) -> None:
        config = _ConfigStub({"background_image_url": " "})
        plugin = await self._make_plugin(config)
        await plugin.collector.stop()
        provider_entered = asyncio.Event()
        release_provider = asyncio.Event()
        save_entered = asyncio.Event()

        async def blocked_targets() -> list[ServerTarget]:
            provider_entered.set()
            await release_provider.wait()
            return []

        async def tracked_save(replace_config: dict[str, Any] | None = None) -> bool:
            save_entered.set()
            if replace_config:
                config.update(replace_config)
            return True

        config.save_config_async = tracked_save
        normalized = self.main.validate_console_settings(
            self.main.serialize_console_settings(plugin.settings)
        )
        snapshot_task: asyncio.Task[list[ServerTarget]] | None = None
        save_task: asyncio.Task[tuple[bool, str]] | None = None
        try:
            with patch.object(
                plugin.targets,
                "collector_targets",
                side_effect=blocked_targets,
            ):
                snapshot_task = asyncio.create_task(plugin._collector_targets())
                await asyncio.wait_for(provider_entered.wait(), timeout=1.0)
                save_task = asyncio.create_task(plugin._apply_web_settings(normalized))
                await asyncio.sleep(0.05)
                self.assertFalse(save_entered.is_set())
                release_provider.set()
                self.assertEqual([], await asyncio.wait_for(snapshot_task, timeout=1.0))
                applied, warning_code, saved_settings = await asyncio.wait_for(
                    save_task,
                    timeout=1.0,
                )
                self.assertTrue(applied)
                self.assertEqual("", warning_code)
                self.assertEqual(config["server_name"], saved_settings["server_name"])
        finally:
            release_provider.set()
            tasks = tuple(task for task in (snapshot_task, save_task) if task is not None)
            for task in tasks:
                if not task.done():
                    task.cancel()
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)

        self.assertTrue(save_entered.is_set())
        self.assertTrue(plugin.collector.running)

    async def test_terminate_waits_for_an_active_command_to_finish(self) -> None:
        plugin = await self._make_plugin({"background_image_url": " "})
        render_entered = asyncio.Event()
        release_render = asyncio.Event()

        async def blocked_render(_target: ServerTarget):
            render_entered.set()
            await release_render.wait()
            return "render://status.png", "", None

        plugin._ensure_collector = AsyncMock(return_value=True)
        plugin._target_for_event = AsyncMock(return_value=self._target())
        plugin._render_status = blocked_render
        event = _EventStub(group_id="42", admin=True)
        command = asyncio.create_task(_collect(plugin.motd(event)))
        await asyncio.wait_for(render_entered.wait(), timeout=1.0)
        termination = asyncio.create_task(plugin.terminate())
        await asyncio.sleep(0)

        self.assertFalse(termination.done())
        self.assertEqual(1, plugin._active_operations)
        self.assertTrue(event.stopped)
        release_render.set()
        results = await asyncio.wait_for(command, timeout=1.0)
        await asyncio.wait_for(termination, timeout=1.0)

        self.assertEqual(("image", "render://status.png"), results[0])
        self.assertEqual(0, plugin._active_operations)
        self.assertTrue(plugin.collector.closed)


if __name__ == "__main__":
    unittest.main()
