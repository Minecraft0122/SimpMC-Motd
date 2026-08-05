from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import Any, TypeVar

from ..constants import DISPLAY_TZ, MAX_PLAYER_COUNT
from ..models import MinecraftStatus

RowT = TypeVar("RowT", bound=Mapping[str, Any])


def _player_count(value: Any) -> int:
    try:
        return min(MAX_PLAYER_COUNT, max(0, int(value)))
    except (TypeError, ValueError, OverflowError):
        return 0


def downsample_rows(rows: Sequence[RowT], limit: int) -> list[RowT]:
    if limit <= 0 or len(rows) <= limit:
        return list(rows)
    step = len(rows) / limit
    picked = [rows[math.floor(index * step)] for index in range(limit)]
    if rows[-1] not in picked:
        picked[-1] = rows[-1]
    return picked


def build_y_ticks(
    y_max: int,
    plot_top: int,
    plot_bottom: int,
) -> list[dict[str, str]]:
    height = plot_bottom - plot_top
    ticks: list[dict[str, str]] = [{"label": str(y_max), "y": str(plot_top + 8)}]
    if y_max > 5:
        step = max(5, int(math.ceil((y_max / 4) / 5) * 5))
        values = []
        for value in range(step, y_max, step):
            y = plot_bottom - (value / y_max) * height
            if plot_top + 46 <= y <= plot_bottom - 28:
                values.append(value)
        for value in reversed(values):
            y = plot_bottom - (value / y_max) * height
            ticks.append({"label": str(value), "y": f"{y + 8:.1f}"})
    ticks.append({"label": "0", "y": str(plot_bottom + 3)})
    return ticks


def build_chart(
    rows: Sequence[Mapping[str, Any]],
    current: MinecraftStatus,
    max_points: int,
    start_ts: float,
    end_ts: float,
) -> dict[str, Any]:
    successful = [row for row in rows if row["success"] and row["online"] is not None]
    sampled = downsample_rows(successful, max(20, max_points))
    online_values = [_player_count(row["online"]) for row in successful]
    peak_online = max(online_values) if online_values else 0
    y_max = max(1, math.ceil(max(peak_online, 1) * 1.2))

    plot_left = 38
    plot_right = 742
    plot_top = 18
    plot_bottom = 296
    width = plot_right - plot_left
    height = plot_bottom - plot_top
    span = max(end_ts - start_ts, 1)

    line_points = ""
    area_points = ""
    point_markers: list[dict[str, str]] = []
    if sampled:
        points: list[str] = []
        marker_values: list[tuple[float, float]] = []
        for row in sampled:
            x = plot_left + (float(row["sampled_at"]) - start_ts) / span * width
            x = min(plot_right, max(plot_left, x))
            online = _player_count(row["online"])
            y = plot_bottom - (online / y_max) * height
            points.append(f"{x:.1f},{y:.1f}")
            marker_values.append((x, y))
        line_points = " ".join(points)
        if len(points) > 1:
            first_x = points[0].split(",")[0]
            last_x = points[-1].split(",")[0]
            area_points = f"{first_x},{plot_bottom} {line_points} {last_x},{plot_bottom}"
        else:
            x, y = marker_values[0]
            point_markers.append({"x": f"{x:.1f}", "y": f"{y:.1f}"})

    if not current.ok:
        chart_status = "error"
        chart_color = "#ff5f6d"
        chart_fill_opacity = "0.18"
        chart_axis_color = chart_color
        chart_tick_color = chart_color
        empty_text = "服务器连接失败"
    elif len(successful) < 2:
        chart_status = "empty"
        chart_color = "#aeb7c3"
        chart_fill_opacity = "0.10"
        chart_axis_color = chart_color
        chart_tick_color = chart_color
        empty_text = "暂无历史在线人数"
    else:
        chart_status = "ok"
        chart_color = "#58f15f"
        chart_fill_opacity = "0.70"
        chart_axis_color = "#ffffff"
        chart_tick_color = "#d2d8e2"
        empty_text = ""

    return {
        "line_points": line_points,
        "area_points": area_points,
        "point_markers": point_markers,
        "y_max": y_max,
        "y_ticks": build_y_ticks(y_max, plot_top, plot_bottom),
        "peak_online": peak_online,
        "sample_count": len(successful),
        "chart_status": chart_status,
        "chart_color": chart_color,
        "chart_fill_opacity": chart_fill_opacity,
        "chart_axis_color": chart_axis_color,
        "chart_tick_color": chart_tick_color,
        "empty_text": empty_text,
    }


def format_ts(ts: float, pattern: str = "%Y-%m-%d %H:%M:%S") -> str:
    return datetime.fromtimestamp(ts, DISPLAY_TZ).strftime(pattern)


def build_x_ticks(start_ts: float, end_ts: float) -> list[dict[str, str]]:
    positions = [
        (0.0, 38, "start"),
        (0.25, 214, "middle"),
        (0.5, 390, "middle"),
        (0.75, 566, "middle"),
        (1.0, 742, "end"),
    ]
    span = max(end_ts - start_ts, 1)
    return [
        {
            "x": str(x),
            "anchor": anchor,
            "time": format_ts(start_ts + span * ratio, "%H:%M"),
        }
        for ratio, x, anchor in positions
    ]
