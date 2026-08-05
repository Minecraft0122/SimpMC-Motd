from __future__ import annotations

import asyncio
import json
import os
import weakref
from collections.abc import Mapping
from contextlib import suppress
from dataclasses import replace
from functools import wraps
from pathlib import Path
from typing import Any

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star
from astrbot.api.web import error_response, json_response, request

try:
    from astrbot.core.utils.astrbot_path import get_astrbot_data_path
except ImportError:  # Compatibility with older/minimal AstrBot runtimes.
    get_astrbot_data_path = None

if __package__:
    from .simpmc_motd.collector import StatusCollector
    from .simpmc_motd.concurrency import KeyedLocks
    from .simpmc_motd.config import ConfigView
    from .simpmc_motd.constants import PLUGIN_ID, PLUGIN_NAME, PLUGIN_VERSION
    from .simpmc_motd.minecraft.client import query_minecraft_status
    from .simpmc_motd.minecraft.components import motd_to_html
    from .simpmc_motd.models import MinecraftStatus, ServerTarget
    from .simpmc_motd.rendering.background import (
        BackgroundImageService,
        fallback_background_data_uri,
        fetch_image_data_uri,
    )
    from .simpmc_motd.rendering.cache import RenderCache
    from .simpmc_motd.rendering.chart import build_chart
    from .simpmc_motd.rendering.presenter import StatusPresenter
    from .simpmc_motd.status_service import StatusService
    from .simpmc_motd.storage import (
        HistoryStore,
        migrate_legacy_database,
        row_to_status,
        row_to_target,
    )
    from .simpmc_motd.targeting import (
        TargetResolver,
        group_id_from_scope,
        normalize_group_scope_key,
        parse_server_address,
        server_target_from_row,
    )
    from .simpmc_motd.text import normalize_unicode
    from .simpmc_motd.web_settings import (
        ConsoleSettingsError,
        serialize_console_settings,
        validate_console_settings,
    )
else:  # Allows local smoke tests that import ``main`` directly.
    from simpmc_motd.collector import StatusCollector
    from simpmc_motd.concurrency import KeyedLocks
    from simpmc_motd.config import ConfigView
    from simpmc_motd.constants import PLUGIN_ID, PLUGIN_NAME, PLUGIN_VERSION
    from simpmc_motd.minecraft.client import query_minecraft_status
    from simpmc_motd.minecraft.components import motd_to_html
    from simpmc_motd.models import MinecraftStatus, ServerTarget
    from simpmc_motd.rendering.background import (
        BackgroundImageService,
        fallback_background_data_uri,
        fetch_image_data_uri,
    )
    from simpmc_motd.rendering.cache import RenderCache
    from simpmc_motd.rendering.chart import build_chart
    from simpmc_motd.rendering.presenter import StatusPresenter
    from simpmc_motd.status_service import StatusService
    from simpmc_motd.storage import (
        HistoryStore,
        migrate_legacy_database,
        row_to_status,
        row_to_target,
    )
    from simpmc_motd.targeting import (
        TargetResolver,
        group_id_from_scope,
        normalize_group_scope_key,
        parse_server_address,
        server_target_from_row,
    )
    from simpmc_motd.text import normalize_unicode
    from simpmc_motd.web_settings import (
        ConsoleSettingsError,
        serialize_console_settings,
        validate_console_settings,
    )


class _PluginClosingError(RuntimeError):
    pass


class _ConfigSaveSuperseded(RuntimeError):
    pass


_RUNTIME_OWNER_REGISTRY_ATTRIBUTE = "_simpmc_motd_runtime_owners_v1"
_RUNTIME_CANDIDATE_REGISTRY_ATTRIBUTE = "_simpmc_motd_runtime_candidates_v1"
_RUNTIME_CANDIDATE_COUNTER_ATTRIBUTE = "_simpmc_motd_runtime_candidate_counter_v1"
_RUNTIME_CANDIDATE_EVENTS_ATTRIBUTE = "_simpmc_motd_runtime_candidate_events_v1"
_RUNTIME_SETTINGS_LOCKS_ATTRIBUTE = "_simpmc_motd_runtime_settings_locks_v1"
_CONFIG_MISSING = object()
_MAX_SETTINGS_REQUEST_BYTES = 1024 * 1024


def _runtime_path_key(path: Path) -> str:
    try:
        return os.path.normcase(str(path.resolve()))
    except OSError:
        return os.path.normcase(str(path.absolute()))


def _runtime_owner_registry(
    loop: asyncio.AbstractEventLoop,
) -> weakref.WeakValueDictionary[str, Any]:
    registry = getattr(loop, _RUNTIME_OWNER_REGISTRY_ATTRIBUTE, None)
    if not isinstance(registry, weakref.WeakValueDictionary):
        registry = weakref.WeakValueDictionary()
        setattr(loop, _RUNTIME_OWNER_REGISTRY_ATTRIBUTE, registry)
    return registry


def _runtime_candidate_registry(
    loop: asyncio.AbstractEventLoop,
) -> dict[str, dict[int, weakref.ReferenceType[Any]]]:
    registry = getattr(loop, _RUNTIME_CANDIDATE_REGISTRY_ATTRIBUTE, None)
    if not isinstance(registry, dict):
        registry = {}
        setattr(loop, _RUNTIME_CANDIDATE_REGISTRY_ATTRIBUTE, registry)
    return registry


def _next_runtime_candidate_token(loop: asyncio.AbstractEventLoop) -> int:
    token = int(getattr(loop, _RUNTIME_CANDIDATE_COUNTER_ATTRIBUTE, 0)) + 1
    setattr(loop, _RUNTIME_CANDIDATE_COUNTER_ATTRIBUTE, token)
    return token


def _runtime_candidate_events(
    loop: asyncio.AbstractEventLoop,
) -> dict[str, asyncio.Event]:
    registry = getattr(loop, _RUNTIME_CANDIDATE_EVENTS_ATTRIBUTE, None)
    if not isinstance(registry, dict):
        registry = {}
        setattr(loop, _RUNTIME_CANDIDATE_EVENTS_ATTRIBUTE, registry)
    return registry


def _runtime_candidate_event(
    loop: asyncio.AbstractEventLoop,
    runtime_key: str,
) -> asyncio.Event:
    registry = _runtime_candidate_events(loop)
    event = registry.get(runtime_key)
    if event is None:
        event = asyncio.Event()
        registry[runtime_key] = event
    return event


def _signal_runtime_candidate_change(
    loop: asyncio.AbstractEventLoop,
    runtime_key: str,
) -> None:
    event = _runtime_candidate_events(loop).pop(runtime_key, None)
    if event is not None:
        event.set()


def _runtime_settings_locks(
    loop: asyncio.AbstractEventLoop,
) -> dict[str, asyncio.Lock]:
    registry = getattr(loop, _RUNTIME_SETTINGS_LOCKS_ATTRIBUTE, None)
    if not isinstance(registry, dict):
        registry = {}
        setattr(loop, _RUNTIME_SETTINGS_LOCKS_ATTRIBUTE, registry)
    return registry


def _mutable_json_value(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _mutable_json_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_mutable_json_value(item) for item in value]
    return value


def _server_address(host: str, port: int) -> str:
    escaped_host = f"[{host}]" if ":" in host and not host.startswith("[") else host
    return f"{escaped_host}:{port}"


def _lifecycle_command(handler):
    """Track an async-generator command so terminate can drain it safely."""

    @wraps(handler)
    async def guarded(self, *args, **kwargs):
        if not await self._begin_operation(settings_reader=True):
            return
        try:
            async for result in handler(self, *args, **kwargs):
                yield result
        finally:
            # A settings writer holds the runtime lock while waiting for readers,
            # so reader exit must only use the lifecycle lock.
            await self._end_operation(settings_reader=True)

    return guarded


class MinecraftMotdPlugin(Star):
    """Thin AstrBot adapter around the independently testable plugin services."""

    def __init__(self, context: Context, config: AstrBotConfig):
        # The one-argument base constructor works on both older AstrBot builds
        # and current versions; the injected plugin config remains owned here.
        super().__init__(context)
        self._context = context
        self.config = config
        self._plugin_root = Path(__file__).resolve().parent
        if get_astrbot_data_path is not None:
            data_root = Path(get_astrbot_data_path()) / "plugin_data"
            plugin_data_path = data_root / PLUGIN_NAME
            self._database_path = plugin_data_path / "history.sqlite3"
            self._legacy_database: Path | None = (
                data_root / "astrbot_plugin_mc_motd" / "history.sqlite3"
            )
        else:
            plugin_data_path = self._plugin_root / "data"
            self._database_path = plugin_data_path / "history.sqlite3"
            self._legacy_database = None

        self.settings = ConfigView(config, warning=self._warning)
        self.store: HistoryStore | None = None
        self.targets: TargetResolver | None = None
        self.status_service: StatusService | None = None
        self.collector: StatusCollector | None = None
        self._lifecycle_lock = asyncio.Lock()
        self._runtime_key = _runtime_path_key(self._database_path)
        self._runtime_loop: asyncio.AbstractEventLoop | None = None
        self._runtime_candidate_loop: asyncio.AbstractEventLoop | None = None
        self._runtime_candidate_token: int | None = None
        self._superseded = False
        self._closing = False
        self._terminated = False
        self._termination_task: asyncio.Task[None] | None = None
        self._active_operations = 0
        self._operations_drained = asyncio.Event()
        self._operations_drained.set()
        self._active_settings_readers = 0
        self._settings_readers_drained = asyncio.Event()
        self._settings_readers_drained.set()
        self._ready_task: asyncio.Task[None] | None = None
        self._legacy_migration_task: asyncio.Task[None] | None = None
        self._web_apis_registered = False
        self.backgrounds = BackgroundImageService(
            url=lambda: self.settings.background_image_url,
            ttl_seconds=lambda: self.settings.background_cache_seconds,
            timeout_seconds=lambda: self.settings.background_fetch_timeout_seconds,
            max_bytes=lambda: self.settings.background_max_bytes,
            warn=self._warning,
        )
        self.presenter = StatusPresenter(self.settings, self.backgrounds)
        self.render_cache = RenderCache()
        self._render_locks = KeyedLocks()
        self._render_semaphore = asyncio.Semaphore(2)
        self.template = (self._plugin_root / "templates" / "status.html").read_text(
            encoding="utf-8"
        )

    @staticmethod
    def _info(message: str) -> None:
        logger.info(f"[{PLUGIN_NAME}] {message}")

    @staticmethod
    def _warning(message: str) -> None:
        logger.warning(f"[{PLUGIN_NAME}] {message}")

    @staticmethod
    def _exception(message: str) -> None:
        logger.exception(f"[{PLUGIN_NAME}] {message}")

    async def initialize(self) -> None:
        async with self._lifecycle_lock:
            if self._closing or self._terminated or self._superseded:
                raise _PluginClosingError("SimpMC-Motd cannot be initialized again")
        if self._owns_runtime_slot():
            self._register_web_apis()
            await self._ensure_collector()
            self._schedule_legacy_binding_migration()
            return

        candidate_token = self._register_runtime_candidate()
        try:
            await self._ensure_ready()
            async with self._lifecycle_lock:
                if self._closing:
                    raise _PluginClosingError("SimpMC-Motd is shutting down")
            if self._owns_runtime_slot():
                await self._ensure_collector()
                return
            if not await self._commit_runtime_ownership(candidate_token):
                self._retire_runtime_owner()
                return
            self._schedule_legacy_binding_migration()
        except asyncio.CancelledError:
            # AstrBot may discard the instance immediately after a cancelled
            # initialize(). Harvest the shielded worker and permanently close
            # any services it creates before allowing that cleanup to proceed.
            try:
                await self.terminate()
            finally:
                self._withdraw_runtime_candidate(candidate_token)
                self._release_runtime_ownership()
            raise
        except Exception:
            try:
                await self.terminate()
            finally:
                self._withdraw_runtime_candidate(candidate_token)
                self._release_runtime_ownership()
            raise

    def _register_runtime_candidate(self) -> int:
        """Record initialize-entry order without disturbing the healthy owner."""

        loop = asyncio.get_running_loop()
        existing_token = self._runtime_candidate_token
        if existing_token is not None and self._runtime_candidate_loop is loop:
            bucket = _runtime_candidate_registry(loop).get(self._runtime_key, {})
            reference = bucket.get(existing_token)
            if reference is not None and reference() is self:
                return existing_token

        token = _next_runtime_candidate_token(loop)
        registry = _runtime_candidate_registry(loop)
        bucket = registry.setdefault(self._runtime_key, {})
        bucket[token] = weakref.ref(self)
        self._runtime_candidate_loop = loop
        self._runtime_candidate_token = token
        _signal_runtime_candidate_change(loop, self._runtime_key)
        return token

    def _withdraw_runtime_candidate(self, token: int) -> None:
        loop = self._runtime_candidate_loop
        if loop is None:
            return
        registry = _runtime_candidate_registry(loop)
        bucket = registry.get(self._runtime_key)
        removed = False
        if bucket is not None:
            reference = bucket.get(token)
            if reference is not None and reference() is self:
                bucket.pop(token, None)
                removed = True
            for stale_token, stale_reference in tuple(bucket.items()):
                if stale_reference() is None:
                    bucket.pop(stale_token, None)
                    removed = True
            if not bucket:
                registry.pop(self._runtime_key, None)
        if self._runtime_candidate_token == token:
            self._runtime_candidate_loop = None
            self._runtime_candidate_token = None
        if removed:
            _signal_runtime_candidate_change(loop, self._runtime_key)

    async def _commit_runtime_ownership(self, token: int) -> bool:
        """Commit the newest ready candidate without discarding a viable fallback."""

        loop = self._runtime_candidate_loop
        if loop is None:
            return False

        while True:
            change_event: asyncio.Event | None = None
            async with self._runtime_settings_lock(loop):
                if self._closing or self._terminated or self._superseded:
                    self._withdraw_runtime_candidate(token)
                    return False

                registry = _runtime_candidate_registry(loop)
                bucket = registry.get(self._runtime_key, {})
                removed_stale = False
                for stale_token, reference in tuple(bucket.items()):
                    if reference() is None:
                        bucket.pop(stale_token, None)
                        removed_stale = True
                if removed_stale:
                    _signal_runtime_candidate_change(loop, self._runtime_key)

                reference = bucket.get(token)
                if reference is None or reference() is not self:
                    self._withdraw_runtime_candidate(token)
                    return False

                owners = _runtime_owner_registry(loop)
                previous = owners.get(self._runtime_key)
                latest_token = max(bucket, default=None)
                if latest_token != token:
                    if previous is not None:
                        self._withdraw_runtime_candidate(token)
                        return False
                    change_event = _runtime_candidate_event(loop, self._runtime_key)
                else:
                    try:
                        self._register_web_apis()
                    except Exception:
                        self._restore_previous_web_apis(previous)
                        raise

                    collector = self.collector
                    if collector is None or collector.closed or collector.running:
                        self._restore_previous_web_apis(previous)
                        raise RuntimeError("prepared collector is not startable")
                    owners[self._runtime_key] = self
                    self._runtime_loop = loop
                    try:
                        started = collector.start()
                    except Exception:
                        if previous is None:
                            owners.pop(self._runtime_key, None)
                        else:
                            owners[self._runtime_key] = previous
                        self._runtime_loop = None
                        self._restore_previous_web_apis(previous)
                        raise
                    if not started:
                        if previous is None:
                            owners.pop(self._runtime_key, None)
                        else:
                            owners[self._runtime_key] = previous
                        self._runtime_loop = None
                        self._restore_previous_web_apis(previous)
                        raise RuntimeError("prepared collector could not be started")

                    registry.pop(self._runtime_key, None)
                    self._runtime_candidate_loop = None
                    self._runtime_candidate_token = None
                    _signal_runtime_candidate_change(loop, self._runtime_key)
                    if previous is not None and previous is not self:
                        retire = getattr(previous, "_retire_runtime_owner", None)
                        if callable(retire):
                            try:
                                retire()
                            except Exception as exc:
                                self._warning(f"退役旧运行实例失败：{exc}")
                    return True

            assert change_event is not None
            await change_event.wait()

    def _restore_previous_web_apis(self, previous: Any) -> None:
        if previous is None or previous is self:
            return
        restore = getattr(previous, "_register_web_apis", None)
        if not callable(restore):
            return
        try:
            restore(force=True)
        except TypeError:
            with suppress(Exception):
                restore()
        except Exception as exc:
            self._warning(f"恢复原运行实例的 Web API 失败：{exc}")

    def _retire_runtime_owner(self) -> None:
        self._superseded = True
        collector = self.collector
        if collector is not None:
            collector._retire_for_replacement()

    def _register_web_apis(self, *, force: bool = False) -> None:
        if self._web_apis_registered and not force:
            return
        self._context.register_web_api(
            f"/{PLUGIN_ID}/settings",
            self.get_web_settings,
            ["GET"],
            "Read SimpMC-Motd settings",
        )
        self._context.register_web_api(
            f"/{PLUGIN_ID}/settings/save",
            self.save_web_settings,
            ["POST"],
            "Validate and save SimpMC-Motd settings",
        )
        self._web_apis_registered = True

    def _runtime_settings_lock(
        self,
        loop: asyncio.AbstractEventLoop | None = None,
    ) -> asyncio.Lock:
        if loop is None:
            loop = asyncio.get_running_loop()
        registry = _runtime_settings_locks(loop)
        lock = registry.get(self._runtime_key)
        if lock is None:
            lock = asyncio.Lock()
            registry[self._runtime_key] = lock
        return lock

    def _schedule_legacy_binding_migration(self) -> None:
        task = self._legacy_migration_task
        if task is not None and not task.done():
            return

        async def run_migration() -> None:
            try:
                await self._migrate_legacy_command_bindings()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self._exception(f"migrate legacy MOTD bindings failed: {exc}")

        try:
            self._legacy_migration_task = asyncio.create_task(
                run_migration(),
                name="SimpMC-Motd legacy binding migration",
            )
        except RuntimeError as exc:
            self._warning(f"无法安排旧版 MOTD 绑定迁移：{exc}")

    def _owns_runtime_slot(self) -> bool:
        loop = self._runtime_loop
        if loop is None:
            return False
        return _runtime_owner_registry(loop).get(self._runtime_key) is self

    def _release_runtime_ownership(self) -> None:
        loop = self._runtime_loop
        self._runtime_loop = None
        if loop is None:
            return
        registry = _runtime_owner_registry(loop)
        if registry.get(self._runtime_key) is self:
            registry.pop(self._runtime_key, None)

    def _prepare_store(self) -> tuple[HistoryStore, str, str]:
        """Perform startup SQLite work outside the AstrBot event loop."""

        database_path = self._database_path
        info_message = ""
        warning_message = ""
        legacy_database = self._legacy_database
        if legacy_database is not None:
            destination_existed = database_path.exists()
            try:
                if migrate_legacy_database(legacy_database, database_path):
                    info_message = f"已迁移旧版数据库：{legacy_database}"
            except Exception as exc:
                if destination_existed or database_path.exists():
                    # Keep the already-active destination authoritative. The
                    # absent migration marker makes the merge retry next start.
                    warning_message = (
                        f"旧版数据库合并失败，继续使用现有新数据库并将在下次启动重试：{exc}"
                    )
                else:
                    # Do not create an empty destination after a transient
                    # backup failure: its existence would suppress retries.
                    database_path = legacy_database
                    warning_message = (
                        f"旧版数据库迁移失败，暂时继续使用旧数据库并将在下次启动重试：{exc}"
                    )
        return HistoryStore(database_path), info_message, warning_message

    async def _initialize_services(self) -> None:
        store, info_message, warning_message = await asyncio.to_thread(self._prepare_store)
        targets = TargetResolver(store, self.settings)
        status_service = StatusService(store, self.settings)
        collector = StatusCollector(
            targets=self._collector_targets,
            status_service=status_service,
            store=store,
            settings=self.settings,
            info=self._info,
            warning=self._warning,
            exception=self._exception,
        )
        self.store = store
        self.targets = targets
        self.status_service = status_service
        self.collector = collector
        if info_message:
            self._info(info_message)
        if warning_message:
            self._warning(warning_message)

    async def _ensure_ready(self) -> None:
        async with self._lifecycle_lock:
            if self._closing:
                raise _PluginClosingError("SimpMC-Motd is shutting down")
            task = self._ready_task
            if task is None:
                task = asyncio.create_task(
                    self._initialize_services(),
                    name="SimpMC-Motd initialization",
                )
                self._ready_task = task
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            raise
        except Exception:
            async with self._lifecycle_lock:
                if self._ready_task is task:
                    self._ready_task = None
            raise
        async with self._lifecycle_lock:
            if self._closing:
                raise _PluginClosingError("SimpMC-Motd is shutting down")

    async def _ensure_collector(self) -> bool:
        try:
            await self._ensure_ready()
        except _PluginClosingError:
            return False
        async with self._lifecycle_lock:
            if self._closing:
                return False
            assert self.collector is not None
            if not self._owns_runtime_slot():
                return False
            await self.collector.ensure_started()
            return not self.collector.closed

    async def _begin_operation(self, *, settings_reader: bool = False) -> bool:
        # AstrBot binds event handlers before awaiting initialize().  A plugin
        # candidate can therefore receive traffic while it is still waiting to
        # become the committed runtime owner.  Reject that traffic without
        # touching its prepared collector; only the elected owner may start an
        # operation or stop propagation for a command.
        async with self._runtime_settings_lock(), self._lifecycle_lock:
            if self._closing or self._superseded or not self._owns_runtime_slot():
                return False
            self._active_operations += 1
            self._operations_drained.clear()
            if settings_reader:
                self._active_settings_readers += 1
                self._settings_readers_drained.clear()
            return True

    async def _end_operation(self, *, settings_reader: bool = False) -> None:
        async with self._lifecycle_lock:
            self._active_operations = max(0, self._active_operations - 1)
            if self._active_operations == 0:
                self._operations_drained.set()
            if settings_reader:
                self._active_settings_readers = max(0, self._active_settings_readers - 1)
                if self._active_settings_readers == 0:
                    self._settings_readers_drained.set()

    async def _migrate_legacy_command_bindings(self) -> None:
        """Move v1 group-command bindings into the console-owned config once."""

        store = self.store
        if store is None:
            return
        async with self._runtime_settings_lock():
            if self._superseded or self._closing or not self._owns_runtime_slot():
                return
            await self._migrate_legacy_command_bindings_locked(store)

    async def _migrate_legacy_command_bindings_locked(self, store: HistoryStore) -> None:
        """Run the migration while ownership transitions and settings writes are blocked."""

        rows = await store.list_servers()
        configured_targets: list[ServerTarget] = []
        existing_targets = self.settings.group_server_targets()
        group_servers = _mutable_json_value(self.settings.group_servers)
        if not isinstance(group_servers, dict):
            group_servers = {}
        imported = 0

        for row in rows:
            try:
                target = server_target_from_row(row)
            except (KeyError, TypeError, ValueError, OverflowError) as exc:
                self._warning(f"忽略无法迁移的旧 MOTD 绑定：{exc}")
                continue
            if not target.configured:
                continue

            configured_targets.append(target)
            scope_id = normalize_group_scope_key(target.scope_id)
            group_id = group_id_from_scope(scope_id)
            if not group_id:
                self._warning(f"旧绑定 {target.scope_id!r} 不是群作用域，v2 不再使用该会话绑定。")
                continue

            wildcard_scope = f"group:{group_id}"
            if scope_id in existing_targets or wildcard_scope in existing_targets:
                continue
            group_servers[scope_id] = {
                "address": _server_address(target.host, target.port),
                "name": target.server_name,
            }
            imported += 1

        if imported:
            encoded = json.dumps(
                group_servers,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            await self._settings_readers_drained.wait()
            if self._superseded or self._closing or not self._owns_runtime_slot():
                return

            collector = self.collector
            collector_was_running = collector is not None and collector.running
            try:
                if collector_was_running:
                    assert collector is not None
                    await collector.stop()
            except Exception as exc:
                if collector_was_running:
                    await self._restart_collector_after_settings(collector)
                self._warning(f"旧版群绑定迁移暂停后台采样器失败，将在下次启动重试：{exc}")
                return

            old_value = self.config.get("group_servers_json", _CONFIG_MISSING)
            try:
                save_config_async = getattr(self.config, "save_config_async", None)
                if not callable(save_config_async):
                    raise RuntimeError("AstrBotConfig.save_config_async is unavailable")
                committed = await save_config_async({"group_servers_json": encoded})
                if committed is False:
                    self._refresh_live_settings()
                    if collector_was_running:
                        await self._restart_collector_after_settings(collector)
                    self._warning("旧版群绑定迁移被更新的配置覆盖，将在下次启动重试。")
                    return
            except Exception as exc:
                if self.config.get("group_servers_json", _CONFIG_MISSING) == encoded:
                    if old_value is _CONFIG_MISSING:
                        self.config.pop("group_servers_json", None)
                    else:
                        self.config["group_servers_json"] = old_value
                self._refresh_live_settings()
                if collector_was_running:
                    await self._restart_collector_after_settings(collector)
                self._warning(f"旧版群绑定迁移到控制台配置失败，将在下次启动重试：{exc}")
                return

            self._refresh_live_settings()
            if not await self._restart_collector_after_settings(collector):
                self._warning("旧版群绑定已经迁移，但后台采样器未能立即恢复，请重载插件。")

        retired = 0
        for target in configured_targets:
            try:
                await store.upsert_server(replace(target, configured=False))
                retired += 1
            except Exception as exc:
                self._warning(f"旧版群绑定 {target.scope_id!r} 标记迁移完成失败：{exc}")

        if imported or retired:
            self._info(
                f"已将 {imported} 个旧版群绑定导入控制台配置，并停用 {retired} 个数据库绑定。"
            )
        if imported:
            self._warning("请前往插件页面 settings 复核从旧版自动迁移的群服映射。")

    async def get_web_settings(self):
        """Return the complete settings form to the authenticated plugin page."""

        if not await self._begin_operation():
            return error_response("插件正在停止，请稍后重试。", status_code=503)
        try:
            async with self._runtime_settings_lock():
                if self._closing or self._superseded or not self._owns_runtime_slot():
                    return error_response("插件正在切换运行实例，请稍后重试。", status_code=503)
                return json_response(
                    {
                        "version": f"v{PLUGIN_VERSION}",
                        "settings": serialize_console_settings(self.settings),
                    }
                )
        except Exception as exc:
            self._exception(f"read WebUI settings failed: {exc}")
            return error_response("读取设置失败，请查看 AstrBot 日志。", status_code=500)
        finally:
            await self._end_operation()

    async def save_web_settings(self):
        """Validate and persist settings submitted by the authenticated plugin page."""

        if not await self._begin_operation():
            return error_response("插件正在停止，请稍后重试。", status_code=503)
        try:
            content_length = request.headers.get("Content-Length")
            if content_length is not None:
                try:
                    declared_length = int(content_length)
                    oversized = declared_length < 0 or declared_length > _MAX_SETTINGS_REQUEST_BYTES
                except (TypeError, ValueError):
                    oversized = True
                if oversized:
                    return error_response(
                        "设置请求内容过大。",
                        status_code=413,
                    )
            raw_body = await request.body()
            if len(raw_body) > _MAX_SETTINGS_REQUEST_BYTES:
                return error_response("设置请求内容过大。", status_code=413)
            try:
                body = json.loads(raw_body)
            except (json.JSONDecodeError, UnicodeDecodeError, TypeError, ValueError):
                return error_response("请求正文必须是有效的 JSON 对象。")
            if not isinstance(body, Mapping) or "settings" not in body:
                return error_response("请求必须包含 settings JSON 对象。")
            try:
                normalized = validate_console_settings(body["settings"])
            except ConsoleSettingsError as exc:
                return error_response(str(exc))

            username = request.username or "AstrBot 管理员"
            transaction = asyncio.create_task(
                self._apply_web_settings(normalized),
                name="SimpMC-Motd settings transaction",
            )
            try:
                applied, warning_code, saved_settings = await asyncio.shield(transaction)
            except asyncio.CancelledError:
                while not transaction.done():
                    try:
                        await asyncio.shield(transaction)
                    except asyncio.CancelledError:
                        continue
                    except Exception:
                        break
                raise
            except _ConfigSaveSuperseded:
                return error_response(
                    "配置已被另一个保存请求更新，请重新读取后再修改。",
                    status_code=409,
                )
            except _PluginClosingError:
                return error_response("插件正在切换运行实例，请稍后重试。", status_code=503)
            except Exception as exc:
                self._exception(f"save WebUI settings failed: {exc}")
                return error_response("保存设置失败，请查看 AstrBot 日志。", status_code=500)

            self._info(f"{username} 已通过控制台更新 MOTD 设置。")
            return json_response(
                {
                    "version": f"v{PLUGIN_VERSION}",
                    "settings": saved_settings,
                    "saved": True,
                    "applied": applied,
                    "message": "设置已保存。",
                    "warning_code": warning_code,
                }
            )
        finally:
            await self._end_operation()

    async def _apply_web_settings(
        self,
        normalized: Mapping[str, Any],
    ) -> tuple[bool, str, dict[str, Any]]:
        """Persist a config snapshot and apply it to the live runtime exactly once."""

        async with self._runtime_settings_lock():
            if self._closing or self._superseded or not self._owns_runtime_slot():
                raise _PluginClosingError("SimpMC-Motd runtime ownership changed")
            # The writer owns the runtime lock, so no new MOTD reader can enter.
            # Existing readers leave through the lifecycle lock and set this
            # event without needing the runtime lock, avoiding a writer/reader
            # deadlock while allowing concurrent MOTD renders normally.
            await self._settings_readers_drained.wait()
            if self._closing or self._superseded or not self._owns_runtime_slot():
                raise _PluginClosingError("SimpMC-Motd runtime ownership changed")

            collector = self.collector
            collector_was_running = collector is not None and collector.running
            try:
                # AstrBotConfig.save_config_async() updates the shared mapping
                # before its awaited disk write completes.  Stop the collector
                # first so it cannot consume that provisional state if the write
                # subsequently fails and is rolled back.
                if collector_was_running:
                    assert collector is not None
                    await collector.stop()
            except Exception:
                if collector_was_running:
                    await self._restart_collector_after_settings(collector)
                raise

            old_values = {key: self.config.get(key, _CONFIG_MISSING) for key in normalized}
            try:
                save_config_async = getattr(self.config, "save_config_async", None)
                if not callable(save_config_async):
                    raise RuntimeError("AstrBotConfig.save_config_async is unavailable")
                committed = await save_config_async(dict(normalized))
            except Exception:
                for key, old_value in old_values.items():
                    if self.config.get(key, _CONFIG_MISSING) != normalized[key]:
                        continue
                    if old_value is _CONFIG_MISSING:
                        self.config.pop(key, None)
                    else:
                        self.config[key] = old_value
                self._refresh_live_settings()
                if collector_was_running:
                    await self._restart_collector_after_settings(collector)
                raise

            if committed is False:
                # A newer AstrBot config snapshot already won.  Do not roll its
                # fields back to this request's old values; refresh and let the
                # page reload the authoritative snapshot after the 409 response.
                self._refresh_live_settings()
                if collector_was_running:
                    await self._restart_collector_after_settings(collector)
                raise _ConfigSaveSuperseded

            self._refresh_live_settings()
            applied = await self._restart_collector_after_settings(collector)
            saved_settings = serialize_console_settings(self.settings)
            return (
                applied,
                "" if applied else "collector_restart_failed",
                saved_settings,
            )

    def _refresh_live_settings(self) -> None:
        """Invalidate runtime state after the authoritative config changes."""

        self.render_cache.clear()
        self.backgrounds.clear()
        if self.status_service is not None:
            self.status_service.refresh_settings()

    async def _restart_collector_after_settings(
        self,
        collector: StatusCollector | None,
    ) -> bool:
        """Start the current collector, retrying once and reporting its final state."""

        if collector is None:
            self._warning("应用设置失败：后台采样器不可用。")
            return False

        failures: list[Exception] = []
        for _attempt in range(2):
            async with self._lifecycle_lock:
                can_restart = (
                    collector is self.collector
                    and not collector.closed
                    and not self._closing
                    and not self._superseded
                    and self._owns_runtime_slot()
                )
            if not can_restart:
                break
            try:
                await collector.ensure_started()
            except Exception as exc:
                failures.append(exc)
            if collector.running:
                if failures:
                    self._warning(f"后台采样器首次重启失败，但自动恢复已经成功：{failures[-1]}")
                return True
            failures.append(RuntimeError("collector did not restart"))

        if failures:
            self._exception(f"restart collector after settings save failed: {failures[-1]}")
        else:
            self._warning("应用设置失败：运行实例已切换或后台采样器已经关闭。")
        return False

    def _scope_from_event(self, event: AstrMessageEvent) -> tuple[str, str]:
        group_id = event.get_group_id()
        if group_id:
            platform = (
                normalize_unicode(
                    event.get_platform_id() or event.get_platform_name() or "unknown",
                    128,
                ).strip()
                or "unknown"
            )
            group_text = normalize_unicode(group_id, 128).strip()
            return f"{platform}:group:{group_text}", f"群 {group_text}"
        platform = (
            normalize_unicode(
                event.get_platform_id() or event.get_platform_name() or "unknown",
                128,
            ).strip()
            or "unknown"
        )
        session_id = normalize_unicode(
            event.get_session_id() or event.unified_msg_origin,
            256,
        )
        return f"{platform}:private:{session_id}", "私聊会话"

    async def _target_for_event(self, event: AstrMessageEvent) -> ServerTarget:
        await self._ensure_ready()
        assert self.targets is not None
        scope_id, scope_label = self._scope_from_event(event)
        group_id = event.get_group_id() or ""
        return await self.targets.resolve(
            scope_id,
            scope_label,
            str(group_id),
            is_private=not bool(group_id),
        )

    async def _collector_targets(self) -> list[ServerTarget]:
        await self._ensure_ready()
        async with self._runtime_settings_lock():
            if self._closing or self._superseded or not self._owns_runtime_slot():
                return []
            assert self.targets is not None
            return await self.targets.collector_targets()

    async def _current_status(
        self,
        target: ServerTarget,
        allow_reuse: bool = True,
    ) -> MinecraftStatus:
        await self._ensure_ready()
        assert self.status_service is not None
        return await self.status_service.current(target, allow_reuse)

    async def _template_data(
        self,
        target: ServerTarget,
        current: MinecraftStatus,
        rows: list[Any],
    ) -> dict[str, Any]:
        return await self.presenter.template_data(target, current, rows)

    def _render_cache_key(self, target: ServerTarget) -> str:
        return self.presenter.render_cache_key(target)

    def _get_cached_render(self, cache_key: str):
        return self.render_cache.get(cache_key, self.settings.render_cache_seconds)

    def _set_cached_render(
        self,
        cache_key: str,
        image_url: str,
        warning: str = "",
    ) -> None:
        self.render_cache.set(
            cache_key,
            image_url,
            self.settings.render_cache_seconds,
            warning,
        )

    def _plain_status(self, target: ServerTarget, current: MinecraftStatus) -> str:
        return self.presenter.plain_status(target, current)

    async def _render_status(
        self,
        target: ServerTarget,
    ) -> tuple[str | None, str, str | None]:
        cache_key = self._render_cache_key(target)
        cached_render = self._get_cached_render(cache_key)
        if cached_render is not None:
            return cached_render.image_url, cached_render.warning, None

        async with self._render_locks.hold(cache_key):
            cached_render = self._get_cached_render(cache_key)
            if cached_render is not None:
                return cached_render.image_url, cached_render.warning, None

            current: MinecraftStatus | None = None
            try:
                current = await self._current_status(target, allow_reuse=True)
                assert self.store is not None
                rows = await self.store.load_history(
                    target.scope_id,
                    target.host,
                    target.port,
                    self.settings.chart_hours,
                )
                data = await self._template_data(target, current, rows)
                warning = str(data.pop("background_warning", "") or "")
                async with self._render_semaphore:
                    image_url = await asyncio.wait_for(
                        self.html_render(
                            self.template,
                            data,
                            return_url=True,
                            options={
                                "type": "png",
                                "full_page": False,
                                "clip": {
                                    "x": 0,
                                    "y": 0,
                                    "width": 790,
                                    "height": 500,
                                },
                                "omit_background": False,
                                "scale": "device",
                            },
                        ),
                        timeout=30.0,
                    )
                if not isinstance(image_url, str) or not image_url.strip():
                    raise RuntimeError("AstrBot renderer returned an empty image URL")
                self._set_cached_render(cache_key, image_url, warning)
                return image_url, warning, None
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self._exception(f"render status image failed: {exc}")
                if current is None:
                    fallback = (
                        f"{target.server_name} 查询或渲染失败\n地址：{target.host}:{target.port}"
                    )
                else:
                    fallback = self._plain_status(target, current)
                return (
                    None,
                    "",
                    fallback + f"\n\n图片渲染失败（{type(exc).__name__}），已返回文字状态。",
                )

    @filter.regex(r"^/?motd$")
    @_lifecycle_command
    async def motd(self, event: AstrMessageEvent):
        """检测 ``/motd`` 或 ``motd`` 并立即生成服务器状态图片。"""

        event.stop_event()
        if not await self._ensure_collector():
            return
        try:
            target = await self._target_for_event(event)
        except PermissionError as exc:
            yield event.plain_result(str(exc))
            return
        except LookupError as exc:
            yield event.plain_result(
                f"{exc}\n请管理员前往 AstrBot 控制台的插件页面 settings 配置 MOTD 查询地址。"
            )
            return
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._exception(f"resolve MOTD target failed: {exc}")
            yield event.plain_result("读取 Minecraft 查询配置失败，请稍后重试或联系管理员。")
            return

        image_url, warning, error_text = await self._render_status(target)
        if image_url is not None:
            yield event.image_result(image_url)
            if warning:
                yield event.plain_result(warning)
        elif error_text is not None:
            yield event.plain_result(error_text)

    async def _terminate_services(self) -> None:
        try:
            ready_task = self._ready_task
            if ready_task is not None:
                with suppress(asyncio.CancelledError, Exception):
                    await asyncio.shield(ready_task)
            migration_task = self._legacy_migration_task
            if migration_task is not None:
                with suppress(asyncio.CancelledError, Exception):
                    await asyncio.shield(migration_task)
            await self._operations_drained.wait()
            if self.collector is not None:
                await self.collector.close()
        finally:
            self.render_cache.clear()
            self.backgrounds.clear()
            self._release_runtime_ownership()
            async with self._lifecycle_lock:
                self._terminated = True

    async def terminate(self) -> None:
        async with self._lifecycle_lock:
            termination_task = self._termination_task
            if termination_task is None:
                self._closing = True
                candidate_token = self._runtime_candidate_token
                if candidate_token is not None:
                    self._withdraw_runtime_candidate(candidate_token)
                termination_task = asyncio.create_task(
                    self._terminate_services(),
                    name="SimpMC-Motd termination",
                )
                self._termination_task = termination_task
        try:
            await asyncio.shield(termination_task)
        except asyncio.CancelledError:
            while not termination_task.done():
                try:
                    await asyncio.shield(termination_task)
                except asyncio.CancelledError:
                    continue
                except Exception:
                    break
            raise


__all__ = [
    "HistoryStore",
    "MinecraftMotdPlugin",
    "MinecraftStatus",
    "ServerTarget",
    "build_chart",
    "fallback_background_data_uri",
    "fetch_image_data_uri",
    "group_id_from_scope",
    "motd_to_html",
    "normalize_group_scope_key",
    "parse_server_address",
    "query_minecraft_status",
    "row_to_status",
    "row_to_target",
]
