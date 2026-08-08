from __future__ import annotations

import html
from collections.abc import Mapping, Sequence
from typing import Any, Protocol

from ..constants import RENDER_CACHE_VERSION
from ..minecraft.components import motd_to_html
from ..models import MinecraftStatus, ServerTarget
from ..text import normalize_unicode
from .background import BackgroundImageService
from .chart import build_chart, build_x_ticks, format_ts


class PresentationSettings(Protocol):
    @property
    def chart_hours(self) -> int: ...

    @property
    def retention_days(self) -> int: ...

    @property
    def max_chart_points(self) -> int: ...

    @property
    def latency_warning_ms(self) -> int: ...

    @property
    def background_image_url(self) -> str: ...

    @property
    def background_opacity(self) -> float: ...

    @property
    def background_overlay_opacity(self) -> float: ...


def safe_text(value: Any) -> str:
    return html.escape(normalize_unicode(value), quote=True)


class StatusPresenter:
    def __init__(
        self,
        settings: PresentationSettings,
        backgrounds: BackgroundImageService,
    ) -> None:
        self._settings = settings
        self._backgrounds = backgrounds

    async def template_data(
        self,
        target: ServerTarget,
        current: MinecraftStatus,
        rows: Sequence[Mapping[str, Any]],
    ) -> dict[str, Any]:
        end_ts = current.sampled_at
        start_ts = end_ts - self._settings.chart_hours * 3600
        chart = build_chart(
            rows,
            current,
            self._settings.max_chart_points,
            start_ts,
            end_ts,
            self._settings.latency_warning_ms,
        )
        background = await self._backgrounds.get()
        background_opacity = self._settings.background_opacity
        overlay_opacity = self._settings.background_overlay_opacity
        if background.is_fallback:
            background_opacity = max(background_opacity, 0.92)
            overlay_opacity = min(overlay_opacity, 0.36)
        current_view = {
            "ok": current.ok,
            "online": current.online,
            "max_players": current.max_players,
            "motd_html": motd_to_html(
                (current.raw_json or {}).get("description")
                if current.raw_json
                else current.motd_plain
            ),
            "favicon": current.favicon,
            "error": safe_text(current.error or "服务器未响应"),
            "sampled_at_text": format_ts(current.sampled_at),
        }
        return {
            "server_name": safe_text(target.server_name),
            "scope_label": safe_text(target.scope_label),
            "current": current_view,
            "x_ticks": build_x_ticks(start_ts, end_ts),
            "background_image_url": background.image_url,
            "background_opacity": f"{background_opacity:.2f}",
            "background_overlay_opacity": f"{overlay_opacity:.2f}",
            "background_warning": background.warning,
            **{
                key: chart[key]
                for key in (
                    "line_points",
                    "area_points",
                    "line_segments",
                    "point_markers",
                    "y_ticks",
                    "chart_color",
                    "chart_fill_opacity",
                    "chart_axis_color",
                    "chart_tick_color",
                    "line_color",
                    "empty_text",
                    "offline_bars",
                    "latency_markers",
                )
            },
        }

    def render_cache_key(self, target: ServerTarget) -> str:
        return "|".join(
            [
                RENDER_CACHE_VERSION,
                target.scope_id,
                target.host,
                str(target.port),
                target.server_name,
                str(self._settings.chart_hours),
                str(self._settings.max_chart_points),
                str(self._settings.latency_warning_ms),
                self._settings.background_image_url,
                f"{self._settings.background_opacity:.2f}",
                f"{self._settings.background_overlay_opacity:.2f}",
            ]
        )

    @staticmethod
    def plain_status(target: ServerTarget, current: MinecraftStatus) -> str:
        if current.ok:
            return (
                f"{target.server_name} 当前在线：{current.online}/{current.max_players}\n"
                f"地址：{current.host}:{current.port}\n"
                f"MOTD：{current.motd_plain}"
            )
        return (
            f"{target.server_name} 查询失败\n"
            f"地址：{current.host}:{current.port}\n"
            f"错误：{current.error or '服务器未响应'}"
        )
