from __future__ import annotations

import asyncio
import os
import time
import weakref
from collections.abc import Awaitable, Callable
from contextlib import suppress
from typing import Protocol

from .models import ServerTarget
from .status_service import StatusService
from .storage import HistoryStore


class CollectorSettings(Protocol):
    @property
    def max_parallel_queries(self) -> int: ...

    @property
    def retention_days(self) -> int: ...

    @property
    def query_interval_seconds(self) -> int: ...


TargetProvider = Callable[[], Awaitable[list[ServerTarget]]]
LogCallback = Callable[[str], None]
_LOOP_REGISTRY_ATTRIBUTE = "_simpmc_motd_active_collectors_v1"


class StatusCollector:
    def __init__(
        self,
        targets: TargetProvider,
        status_service: StatusService,
        store: HistoryStore,
        settings: CollectorSettings,
        info: LogCallback,
        warning: LogCallback,
        exception: LogCallback,
        initial_delay_seconds: float = 1.0,
    ) -> None:
        self._targets = targets
        self._status_service = status_service
        self._store = store
        self._settings = settings
        self._info = info
        self._warning = warning
        self._exception = exception
        self._initial_delay_seconds = max(0.0, initial_delay_seconds)
        self._task: asyncio.Task[None] | None = None
        self._closed = False
        self._registry_loop: asyncio.AbstractEventLoop | None = None
        self._registry_key: str | None = None

    def _runtime_key(self) -> str:
        try:
            database_path = self._store.db_path.resolve()
        except (OSError, RuntimeError):
            database_path = self._store.db_path.absolute()
        return os.path.normcase(str(database_path))

    def _claim_runtime_slot(self, loop: asyncio.AbstractEventLoop) -> None:
        """Retire a duplicate collector left by a concurrent plugin load."""

        registry = getattr(loop, _LOOP_REGISTRY_ATTRIBUTE, None)
        if not isinstance(registry, weakref.WeakValueDictionary):
            registry = weakref.WeakValueDictionary()
            setattr(loop, _LOOP_REGISTRY_ATTRIBUTE, registry)

        key = self._runtime_key()
        previous = registry.get(key)
        registry[key] = self
        self._registry_loop = loop
        self._registry_key = key
        if previous is not None and previous is not self:
            previous._retire_for_replacement()

    def _retire_for_replacement(self) -> None:
        self._closed = True
        if self._task is not None:
            self._task.cancel()

    def _release_runtime_slot(self) -> None:
        loop = self._registry_loop
        key = self._registry_key
        self._registry_loop = None
        self._registry_key = None
        if loop is None or key is None:
            return
        registry = getattr(loop, _LOOP_REGISTRY_ATTRIBUTE, None)
        if isinstance(registry, weakref.WeakValueDictionary) and registry.get(key) is self:
            registry.pop(key, None)

    def start(self) -> bool:
        if self._closed:
            return False
        if self._task is not None and not self._task.done():
            return False
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return False
        task = loop.create_task(
            self._run(),
            name="SimpMC-Motd status collector",
        )
        try:
            self._claim_runtime_slot(loop)
        except Exception:
            task.cancel()
            raise
        self._task = task
        return True

    async def ensure_started(self) -> None:
        if self._closed:
            return
        if not self.start() and not self._closed and (self._task is None or self._task.done()):
            self._task = asyncio.create_task(
                self._run(),
                name="SimpMC-Motd status collector",
            )

    async def _run(self) -> None:
        await asyncio.sleep(self._initial_delay_seconds)
        while True:
            try:
                targets = await self._targets()
                if targets:
                    await self._sample_targets(targets)
                cutoff = time.time() - self._settings.retention_days * 86400
                await self._store.purge_older_than(cutoff)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self._exception(f"collector loop error: {exc}")
            await asyncio.sleep(self._settings.query_interval_seconds)

    async def _sample_targets(self, targets: list[ServerTarget]) -> None:
        iterator = iter(targets)

        async def worker() -> None:
            while True:
                try:
                    target = next(iterator)
                except StopIteration:
                    return
                await self._sample_safely(target)

        worker_count = min(len(targets), self._settings.max_parallel_queries)
        await asyncio.gather(*(worker() for _ in range(worker_count)))

    async def _sample_safely(self, target: ServerTarget) -> None:
        try:
            status = await self._status_service.current(target, allow_reuse=True)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._exception(f"{target.scope_label} status sampling crashed: {exc}")
            return
        if status.ok:
            self._info(
                f"{target.scope_label} {status.host}:{status.port} "
                f"{status.online}/{status.max_players} online"
            )
        else:
            self._warning(f"{target.scope_label} status query failed: {status.error}")

    async def stop(self) -> None:
        task = self._task
        self._task = None
        if task is None:
            return
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task

    async def close(self) -> None:
        """Permanently stop this instance so an unloading plugin cannot revive it."""

        self._closed = True
        try:
            await self.stop()
        finally:
            self._release_runtime_slot()

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    @property
    def closed(self) -> bool:
        return self._closed
