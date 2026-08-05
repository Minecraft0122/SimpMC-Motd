from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass
class MinecraftStatus:
    ok: bool
    sampled_at: float
    host: str
    port: int
    online: int | None = None
    max_players: int | None = None
    motd_plain: str = ""
    version_name: str = ""
    protocol: int | None = None
    favicon: str | None = None
    latency_ms: int | None = None
    error: str = ""
    raw_json: dict[str, Any] | None = None


@dataclass
class ServerTarget:
    scope_id: str
    scope_label: str
    server_name: str
    host: str
    port: int
    configured: bool = True


@dataclass
class RenderCacheEntry:
    created_at: float
    image_url: str
    warning: str = ""


@dataclass
class BackgroundCacheEntry:
    created_at: float
    source_url: str
    image_url: str


@dataclass
class BackgroundRenderImage:
    image_url: str
    is_fallback: bool = False
    warning: str = ""
