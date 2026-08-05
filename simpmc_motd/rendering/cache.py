from __future__ import annotations

import time
from collections import OrderedDict
from collections.abc import Callable

from ..constants import DEFAULT_RENDER_CACHE_ENTRIES
from ..models import RenderCacheEntry


class RenderCache:
    def __init__(
        self,
        max_entries: int = DEFAULT_RENDER_CACHE_ENTRIES,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._max_entries = max(1, max_entries)
        self._clock = clock
        self._entries: OrderedDict[str, RenderCacheEntry] = OrderedDict()

    def get(self, key: str, ttl_seconds: int) -> RenderCacheEntry | None:
        if ttl_seconds <= 0:
            return None
        entry = self._entries.get(key)
        if entry is None:
            return None
        if self._clock() - entry.created_at > ttl_seconds:
            self._entries.pop(key, None)
            return None
        self._entries.move_to_end(key)
        return entry

    def set(
        self,
        key: str,
        image_url: str,
        ttl_seconds: int,
        warning: str = "",
    ) -> None:
        if ttl_seconds <= 0:
            return
        now = self._clock()
        expired = [
            cache_key
            for cache_key, entry in self._entries.items()
            if now - entry.created_at > ttl_seconds
        ]
        for cache_key in expired:
            self._entries.pop(cache_key, None)
        self._entries[key] = RenderCacheEntry(now, image_url, warning)
        self._entries.move_to_end(key)
        while len(self._entries) > self._max_entries:
            self._entries.popitem(last=False)

    def clear(self) -> None:
        self._entries.clear()

    def __len__(self) -> int:
        return len(self._entries)
