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


def _row_value(row: Mapping[str, Any], key: str, default: Any = None) -> Any:
    try:
        return row[key]
    except (KeyError, IndexError, TypeError):
        return default


def _row_timestamp(row: Mapping[str, Any]) -> float | None:
    try:
        timestamp = float(_row_value(row, "sampled_at"))
    except (TypeError, ValueError, OverflowError):
        return None
    return timestamp if math.isfinite(timestamp) else None


def downsample_rows(rows: Sequence[RowT], limit: int) -> list[RowT]:
    if limit <= 0 or len(rows) <= limit:
        return list(rows)
    step = len(rows) / limit
    picked = [rows[math.floor(index * step)] for index in range(limit)]
    if rows[-1] not in picked:
        picked[-1] = rows[-1]
    return picked


def _downsample_observations(
    rows: Sequence[RowT],
    limit: int,
    latency_warning_ms: int,
) -> list[RowT]:
    if limit <= 0 or len(rows) <= limit:
        return list(rows)

    required_indices = {0, len(rows) - 1}
    for index, row in enumerate(rows):
        if not bool(_row_value(row, "success")):
            required_indices.add(index)
            continue
        try:
            latency = int(_row_value(row, "latency_ms"))
        except (TypeError, ValueError, OverflowError):
            continue
        if latency > latency_warning_ms:
            required_indices.add(index)

    if len(required_indices) >= limit:
        ordered = sorted(required_indices)
        return [rows[index] for index in downsample_rows(ordered, limit)]

    ordinary_indices = [index for index in range(len(rows)) if index not in required_indices]
    selected_indices = required_indices | set(
        downsample_rows(ordinary_indices, limit - len(required_indices))
    )
    return [rows[index] for index in sorted(selected_indices)]


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
    latency_warning_ms: int = 200,
) -> dict[str, Any]:
    try:
        threshold = max(0, int(latency_warning_ms))
    except (TypeError, ValueError, OverflowError):
        threshold = 200
    observations = [row for row in rows if _row_timestamp(row) is not None]
    current_timestamp = _row_timestamp({"sampled_at": current.sampled_at})
    has_current_failure = current_timestamp is not None and any(
        not bool(_row_value(row, "success")) and _row_timestamp(row) == current_timestamp
        for row in observations
    )
    if not current.ok and current_timestamp is not None and not has_current_failure:
        observations.append(
            {
                "sampled_at": current.sampled_at,
                "success": 0,
                "online": None,
                "latency_ms": current.latency_ms,
            }
        )
    observations.sort(key=lambda row: float(_row_value(row, "sampled_at")))
    successful = [
        row
        for row in observations
        if bool(_row_value(row, "success")) and _row_value(row, "online") is not None
    ]
    sampled = _downsample_observations(observations, max(20, max_points), threshold)
    online_values = [_player_count(_row_value(row, "online")) for row in successful]
    peak_online = max(online_values) if online_values else 0
    y_max = max(1, peak_online)

    plot_left = 38
    plot_right = 742
    plot_top = 18
    plot_bottom = 296
    width = plot_right - plot_left
    height = plot_bottom - plot_top
    span = max(end_ts - start_ts, 1)

    line_points = ""
    area_points = ""
    line_segments: list[dict[str, str]] = []
    point_markers: list[dict[str, str]] = []
    offline_bars: list[dict[str, str]] = []
    latency_markers: list[dict[str, str]] = []
    if sampled:
        successful_runs: list[list[tuple[float, float]]] = []
        current_run: list[tuple[float, float]] = []
        x_values: list[float] = []
        for row in sampled:
            x = plot_left + (float(_row_value(row, "sampled_at")) - start_ts) / span * width
            x = min(plot_right, max(plot_left, x))
            x_values.append(x)
        for index, row in enumerate(sampled):
            x = x_values[index]
            if not bool(_row_value(row, "success")):
                if current_run:
                    successful_runs.append(current_run)
                    current_run = []
                neighbors = [
                    abs(x_values[neighbor] - x)
                    for neighbor in (index - 1, index + 1)
                    if 0 <= neighbor < len(x_values) and x_values[neighbor] != x
                ]
                spacing = min(neighbors) if neighbors else 10.0
                bar_width = max(3.0, min(18.0, spacing * 0.65))
                bar_x = max(plot_left, min(plot_right - bar_width, x - bar_width / 2))
                offline_bars.append(
                    {
                        "x": f"{bar_x:.1f}",
                        "y": str(plot_top),
                        "width": f"{bar_width:.1f}",
                        "height": str(height),
                    }
                )
                continue
            if _row_value(row, "online") is None:
                if current_run:
                    successful_runs.append(current_run)
                    current_run = []
                continue
            online = _player_count(_row_value(row, "online"))
            y = plot_bottom - (online / y_max) * height
            current_run.append((x, y))
            try:
                latency = int(_row_value(row, "latency_ms"))
            except (TypeError, ValueError, OverflowError):
                latency = None
            if latency is not None and latency > threshold:
                latency_markers.append({"x": f"{x:.1f}", "y": f"{y:.1f}"})
        if current_run:
            successful_runs.append(current_run)

        for run in successful_runs:
            if len(run) == 1:
                x, y = run[0]
                point_markers.append({"x": f"{x:.1f}", "y": f"{y:.1f}"})
                continue
            run_points = " ".join(f"{x:.1f},{y:.1f}" for x, y in run)
            first_x = f"{run[0][0]:.1f}"
            last_x = f"{run[-1][0]:.1f}"
            line_segments.append(
                {
                    "line_points": run_points,
                    "area_points": (f"{first_x},{plot_bottom} {run_points} {last_x},{plot_bottom}"),
                }
            )
        if len(line_segments) == 1 and not point_markers:
            line_points = line_segments[0]["line_points"]
            area_points = line_segments[0]["area_points"]

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
        "line_segments": line_segments,
        "point_markers": point_markers,
        "offline_bars": offline_bars,
        "latency_markers": latency_markers,
        "y_max": y_max,
        "y_ticks": build_y_ticks(y_max, plot_top, plot_bottom),
        "peak_online": peak_online,
        "sample_count": len(successful),
        "chart_status": chart_status,
        "chart_color": chart_color,
        "chart_fill_opacity": chart_fill_opacity,
        "chart_axis_color": chart_axis_color,
        "chart_tick_color": chart_tick_color,
        "line_color": "#58f15f" if successful else chart_color,
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
