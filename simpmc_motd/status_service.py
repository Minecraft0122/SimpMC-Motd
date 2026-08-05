from __future__ import annotations

import asyncio
import time
from collections import OrderedDict
from collections.abc import Awaitable, Callable
from dataclasses import replace
from typing import Protocol

from .concurrency import KeyedLocks
from .minecraft.client import query_minecraft_status
from .models import MinecraftStatus, ServerTarget
from .storage import HistoryStore, row_to_status


class StatusSettings(Protocol):
    @property
    def timeout_seconds(self) -> float: ...

    @property
    def protocol_version(self) -> int: ...

    @property
    def send_latency_ping(self) -> bool: ...

    @property
    def sample_reuse_seconds(self) -> int: ...

    @property
    def max_parallel_queries(self) -> int: ...


StatusQuery = Callable[..., Awaitable[MinecraftStatus]]


class StatusService:
    def __init__(
        self,
        store: HistoryStore,
        settings: StatusSettings,
        query: StatusQuery = query_minecraft_status,
    ) -> None:
        self._store = store
        self._settings = settings
        self._query = query
        self._query_locks = KeyedLocks()
        self._query_semaphore = asyncio.Semaphore(
            max(1, int(getattr(settings, "max_parallel_queries", 4)))
        )
        self._endpoint_cache: OrderedDict[str, MinecraftStatus] = OrderedDict()
        self._max_endpoint_cache_entries = 256

    def query_key(self, target: ServerTarget) -> str:
        return f"{target.host.casefold()}|{target.port}|{self._settings.protocol_version}"

    def _cached_endpoint_status(
        self,
        key: str,
        max_age_seconds: int,
    ) -> MinecraftStatus | None:
        status = self._endpoint_cache.get(key)
        if status is None:
            return None
        if time.time() - status.sampled_at > max_age_seconds:
            self._endpoint_cache.pop(key, None)
            return None
        self._endpoint_cache.move_to_end(key)
        return status

    def _cache_endpoint_status(self, key: str, status: MinecraftStatus) -> None:
        self._endpoint_cache[key] = status
        self._endpoint_cache.move_to_end(key)
        while len(self._endpoint_cache) > self._max_endpoint_cache_entries:
            self._endpoint_cache.popitem(last=False)

    async def latest_stored(
        self,
        target: ServerTarget,
        max_age_seconds: int,
    ) -> MinecraftStatus | None:
        if max_age_seconds <= 0:
            return None
        row = await self._store.latest_status(
            target.scope_id,
            target.host,
            target.port,
            max_age_seconds,
        )
        return row_to_status(row, target) if row is not None else None

    async def sample(self, target: ServerTarget) -> MinecraftStatus:
        async with self._query_semaphore:
            status = await self._query(
                host=target.host,
                port=target.port,
                timeout=self._settings.timeout_seconds,
                protocol_version=self._settings.protocol_version,
                send_latency_ping=self._settings.send_latency_ping,
            )
        self._cache_endpoint_status(self.query_key(target), status)
        await self._store.add_sample(target.scope_id, status)
        return status

    async def current(
        self,
        target: ServerTarget,
        allow_reuse: bool = True,
    ) -> MinecraftStatus:
        max_age = self._settings.sample_reuse_seconds if allow_reuse else 0
        latest = await self.latest_stored(target, max_age)
        if latest is not None:
            return latest

        async with self._query_locks.hold(self.query_key(target)):
            latest = await self.latest_stored(target, max_age)
            if latest is not None:
                return latest
            if max_age > 0:
                endpoint_status = self._cached_endpoint_status(
                    self.query_key(target),
                    max_age,
                )
                if endpoint_status is not None:
                    scoped_status = replace(
                        endpoint_status,
                        host=target.host,
                        port=target.port,
                    )
                    await self._store.add_sample(target.scope_id, scoped_status)
                    return scoped_status
            return await self.sample(target)

    def refresh_settings(self) -> None:
        """Apply concurrency changes and discard endpoint results after a console save."""

        self._query_semaphore = asyncio.Semaphore(
            max(1, int(getattr(self._settings, "max_parallel_queries", 4)))
        )
        self._endpoint_cache.clear()

    @property
    def active_query_keys(self) -> int:
        return len(self._query_locks)
