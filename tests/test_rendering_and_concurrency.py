from __future__ import annotations

import asyncio
import json
import time
import unittest
from dataclasses import dataclass
from typing import Any

from simpmc_motd.concurrency import KeyedLocks
from simpmc_motd.models import MinecraftStatus, ServerTarget
from simpmc_motd.rendering.cache import RenderCache
from simpmc_motd.rendering.chart import (
    build_chart,
    build_x_ticks,
    build_y_ticks,
    downsample_rows,
)
from simpmc_motd.status_service import StatusService


class ChartTests(unittest.TestCase):
    @staticmethod
    def status(ok: bool = True) -> MinecraftStatus:
        return MinecraftStatus(
            ok=ok,
            sampled_at=400.0,
            host="chart.example",
            port=25565,
            online=10 if ok else None,
            max_players=20 if ok else None,
            error="offline" if not ok else "",
        )

    def test_downsample_is_bounded_and_keeps_both_ends(self) -> None:
        rows = [{"value": value} for value in range(10)]
        self.assertEqual(rows, downsample_rows(rows, 0))
        self.assertEqual([0, 3, 9], [row["value"] for row in downsample_rows(rows, 3)])
        self.assertEqual(3, len(downsample_rows(rows, 3)))

    def test_chart_uses_only_successful_rows_and_real_time_positions(self) -> None:
        rows = [
            {"sampled_at": 100.0, "success": 1, "online": 5},
            {"sampled_at": 200.0, "success": 0, "online": 99},
            {"sampled_at": 300.0, "success": 1, "online": None},
            {"sampled_at": 400.0, "success": 1, "online": 10},
        ]
        chart = build_chart(rows, self.status(), 180, 100.0, 400.0)
        self.assertEqual("ok", chart["chart_status"])
        self.assertEqual(2, chart["sample_count"])
        self.assertEqual(10, chart["peak_online"])
        self.assertEqual(12, chart["y_max"])
        self.assertTrue(chart["line_points"].startswith("38.0,"))
        self.assertIn("742.0,", chart["line_points"])
        self.assertTrue(chart["area_points"].startswith("38.0,296 "))
        self.assertTrue(chart["area_points"].endswith(" 742.0,296"))
        self.assertEqual([], chart["point_markers"])

    def test_single_point_empty_and_offline_states(self) -> None:
        row = {"sampled_at": 250.0, "success": 1, "online": -4}
        empty = build_chart([row], self.status(), 180, 100.0, 400.0)
        self.assertEqual("empty", empty["chart_status"])
        self.assertEqual("暂无历史在线人数", empty["empty_text"])
        self.assertEqual(0, empty["peak_online"])
        self.assertEqual("", empty["area_points"])
        self.assertEqual(1, len(empty["point_markers"]))

        offline = build_chart([row], self.status(False), 180, 100.0, 400.0)
        self.assertEqual("error", offline["chart_status"])
        self.assertEqual("#ff5f6d", offline["chart_color"])
        self.assertEqual("服务器连接失败", offline["empty_text"])

    def test_ticks_keep_timezone_and_y_axis_contract(self) -> None:
        ticks = build_x_ticks(0, 4 * 3600)
        self.assertEqual(["38", "214", "390", "566", "742"], [t["x"] for t in ticks])
        self.assertEqual(
            ["08:00", "09:00", "10:00", "11:00", "12:00"],
            [tick["time"] for tick in ticks],
        )
        y_ticks = build_y_ticks(37, 18, 296)
        self.assertEqual("37", y_ticks[0]["label"])
        self.assertEqual("0", y_ticks[-1]["label"])
        self.assertTrue(all(int(tick["label"]) % 5 == 0 for tick in y_ticks[1:-1]))

    def test_untrusted_player_counts_are_bounded_before_float_math(self) -> None:
        rows = [
            {"sampled_at": 100.0, "success": 1, "online": 10**400},
            {"sampled_at": 400.0, "success": 1, "online": "invalid"},
        ]
        chart = build_chart(rows, self.status(), 180, 100.0, 400.0)
        self.assertEqual(2_147_483_647, chart["peak_online"])
        self.assertEqual(2_576_980_377, chart["y_max"])
        self.assertNotIn("inf", chart["line_points"].lower())
        self.assertNotIn("nan", chart["line_points"].lower())


class RenderCacheTests(unittest.TestCase):
    def test_ttl_warning_and_lru_eviction(self) -> None:
        now = [0.0]
        cache = RenderCache(max_entries=2, clock=lambda: now[0])
        cache.set("a", "image:a", ttl_seconds=100, warning="warning:a")
        cache.set("b", "image:b", ttl_seconds=100)
        entry = cache.get("a", ttl_seconds=100)
        self.assertIsNotNone(entry)
        self.assertEqual("warning:a", entry.warning)

        cache.set("c", "image:c", ttl_seconds=100)
        self.assertIsNone(cache.get("b", ttl_seconds=100))
        self.assertEqual("image:a", cache.get("a", ttl_seconds=100).image_url)
        self.assertEqual("image:c", cache.get("c", ttl_seconds=100).image_url)
        self.assertEqual(2, len(cache))

        now[0] = 100.0
        self.assertIsNotNone(cache.get("a", ttl_seconds=100))
        now[0] = 100.01
        self.assertIsNone(cache.get("a", ttl_seconds=100))
        self.assertEqual(1, len(cache))

        cache.clear()
        self.assertEqual(0, len(cache))

    def test_non_positive_ttl_disables_reads_and_writes(self) -> None:
        cache = RenderCache(clock=lambda: 1.0)
        cache.set("disabled", "image", ttl_seconds=0)
        self.assertEqual(0, len(cache))
        cache.set("enabled", "image", ttl_seconds=10)
        self.assertIsNone(cache.get("enabled", ttl_seconds=0))
        self.assertEqual(1, len(cache))


class KeyedLocksTests(unittest.IsolatedAsyncioTestCase):
    async def test_same_key_is_serialized_and_entry_is_removed(self) -> None:
        locks = KeyedLocks()
        first_entered = asyncio.Event()
        release_first = asyncio.Event()
        second_entered = asyncio.Event()
        order: list[str] = []

        async def first() -> None:
            async with locks.hold("same"):
                order.append("first-enter")
                first_entered.set()
                await release_first.wait()
                order.append("first-exit")

        async def second() -> None:
            async with locks.hold("same"):
                order.append("second-enter")
                second_entered.set()

        first_task = asyncio.create_task(first())
        await first_entered.wait()
        second_task = asyncio.create_task(second())
        await asyncio.sleep(0)
        self.assertFalse(second_entered.is_set())
        self.assertEqual(1, len(locks))
        release_first.set()
        await asyncio.gather(first_task, second_task)
        self.assertEqual(
            ["first-enter", "first-exit", "second-enter"],
            order,
        )
        self.assertEqual(0, len(locks))

    async def test_different_keys_run_in_parallel(self) -> None:
        locks = KeyedLocks()
        entered_a = asyncio.Event()
        entered_b = asyncio.Event()
        release = asyncio.Event()

        async def worker(key: str, entered: asyncio.Event) -> None:
            async with locks.hold(key):
                entered.set()
                await release.wait()

        task_a = asyncio.create_task(worker("a", entered_a))
        task_b = asyncio.create_task(worker("b", entered_b))
        await asyncio.wait_for(asyncio.gather(entered_a.wait(), entered_b.wait()), timeout=1.0)
        self.assertEqual(2, len(locks))
        release.set()
        await asyncio.gather(task_a, task_b)
        self.assertEqual(0, len(locks))

    async def test_cancelled_waiter_does_not_leak_lock_entry(self) -> None:
        locks = KeyedLocks()
        owner_entered = asyncio.Event()
        release_owner = asyncio.Event()

        async def owner() -> None:
            async with locks.hold("key"):
                owner_entered.set()
                await release_owner.wait()

        async def waiter() -> None:
            async with locks.hold("key"):
                raise AssertionError("cancelled waiter must not acquire the lock")

        owner_task = asyncio.create_task(owner())
        await owner_entered.wait()
        waiter_task = asyncio.create_task(waiter())
        await asyncio.sleep(0)
        waiter_task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await waiter_task
        self.assertEqual(1, len(locks))
        release_owner.set()
        await owner_task
        self.assertEqual(0, len(locks))


@dataclass
class _Settings:
    timeout_seconds: float = 1.0
    protocol_version: int = 760
    send_latency_ping: bool = False
    sample_reuse_seconds: int = 30


class _MemoryStore:
    def __init__(self) -> None:
        self.rows: dict[tuple[str, str, int], dict[str, Any]] = {}

    async def latest_status(
        self, scope_id: str, host: str, port: int, max_age_seconds: int
    ) -> dict[str, Any] | None:
        return self.rows.get((scope_id, host, port))

    async def add_sample(self, scope_id: str, status: MinecraftStatus) -> None:
        self.rows[(scope_id, status.host, status.port)] = {
            "sampled_at": status.sampled_at,
            "success": 1 if status.ok else 0,
            "online": status.online,
            "max_players": status.max_players,
            "motd": status.motd_plain,
            "version_name": status.version_name,
            "latency_ms": status.latency_ms,
            "error": status.error,
            "raw_json": json.dumps(status.raw_json) if status.raw_json else None,
        }


class StatusServiceConcurrencyTests(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    def target(scope_id: str = "group:1") -> ServerTarget:
        return ServerTarget(scope_id, "群", "Server", "server.example", 25565)

    async def test_concurrent_reusable_queries_are_coalesced(self) -> None:
        store = _MemoryStore()
        calls = 0

        async def query(**kwargs: Any) -> MinecraftStatus:
            nonlocal calls
            calls += 1
            await asyncio.sleep(0.02)
            return MinecraftStatus(
                ok=True,
                sampled_at=123.0,
                host=kwargs["host"],
                port=kwargs["port"],
                online=9,
                max_players=20,
                motd_plain="cached",
            )

        service = StatusService(store, _Settings(), query=query)  # type: ignore[arg-type]
        statuses = await asyncio.gather(*(service.current(self.target()) for _ in range(20)))
        self.assertEqual(1, calls)
        self.assertTrue(all(status.online == 9 for status in statuses))
        self.assertEqual(0, service.active_query_keys)

    async def test_forced_queries_are_serialized_but_not_reused(self) -> None:
        store = _MemoryStore()
        calls = 0
        active = 0
        max_active = 0

        async def query(**kwargs: Any) -> MinecraftStatus:
            nonlocal calls, active, max_active
            calls += 1
            active += 1
            max_active = max(max_active, active)
            await asyncio.sleep(0.01)
            active -= 1
            return MinecraftStatus(
                ok=True,
                sampled_at=float(calls),
                host=kwargs["host"],
                port=kwargs["port"],
                online=calls,
                max_players=20,
            )

        service = StatusService(store, _Settings(), query=query)  # type: ignore[arg-type]
        await asyncio.gather(*(service.current(self.target(), allow_reuse=False) for _ in range(5)))
        self.assertEqual(5, calls)
        self.assertEqual(1, max_active)
        self.assertEqual(0, service.active_query_keys)

    async def test_query_is_shared_by_endpoint_across_scopes(self) -> None:
        store = _MemoryStore()
        calls = 0
        active = 0
        max_active = 0

        async def query(**kwargs: Any) -> MinecraftStatus:
            nonlocal active, calls, max_active
            calls += 1
            active += 1
            max_active = max(max_active, active)
            await asyncio.sleep(0.02)
            active -= 1
            return MinecraftStatus(
                ok=True,
                sampled_at=time.time(),
                host=kwargs["host"],
                port=kwargs["port"],
            )

        service = StatusService(store, _Settings(), query=query)  # type: ignore[arg-type]
        second_target = self.target("group:2")
        second_target.host = "SERVER.EXAMPLE"
        await asyncio.gather(
            service.current(self.target("group:1")),
            service.current(second_target),
        )
        self.assertEqual(1, calls)
        self.assertEqual(1, max_active)
        self.assertEqual(2, len(store.rows))
        self.assertIn(("group:2", "SERVER.EXAMPLE", 25565), store.rows)
        self.assertEqual(0, service.active_query_keys)


if __name__ == "__main__":
    unittest.main()
