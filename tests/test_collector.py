from __future__ import annotations

import asyncio
import time
import unittest
from dataclasses import dataclass
from pathlib import Path

from simpmc_motd.collector import StatusCollector
from simpmc_motd.models import MinecraftStatus, ServerTarget


@dataclass
class _Settings:
    max_parallel_queries: int = 3
    retention_days: int = 30
    query_interval_seconds: int = 3600


class _Store:
    def __init__(self, db_path: Path | None = None) -> None:
        self.cutoffs: list[float] = []
        self.purged = asyncio.Event()
        self.db_path = db_path or Path(f"collector-test-{id(self)}.sqlite3")

    async def purge_older_than(self, cutoff: float) -> None:
        self.cutoffs.append(cutoff)
        self.purged.set()


class _StatusService:
    def __init__(self) -> None:
        self.active = 0
        self.max_active = 0
        self.seen: list[str] = []

    async def current(
        self,
        target: ServerTarget,
        allow_reuse: bool = True,
    ) -> MinecraftStatus:
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        await asyncio.sleep(0.01)
        self.active -= 1
        self.seen.append(target.scope_id)
        return MinecraftStatus(
            ok=True,
            sampled_at=time.time(),
            host=target.host,
            port=target.port,
            online=1,
            max_players=20,
        )


class CollectorTests(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    def _targets(count: int) -> list[ServerTarget]:
        return [
            ServerTarget(
                scope_id=f"group:{index}",
                scope_label=f"群 {index}",
                server_name="Server",
                host="server.example",
                port=25565,
            )
            for index in range(count)
        ]

    @staticmethod
    def _collector(
        service: _StatusService,
        store: _Store,
        settings: _Settings,
        *,
        targets=None,
        initial_delay_seconds: float = 0.0,
    ) -> StatusCollector:
        return StatusCollector(
            targets=targets or (lambda: asyncio.sleep(0, result=[])),
            status_service=service,  # type: ignore[arg-type]
            store=store,  # type: ignore[arg-type]
            settings=settings,
            info=lambda _message: None,
            warning=lambda _message: None,
            exception=lambda _message: None,
            initial_delay_seconds=initial_delay_seconds,
        )

    async def test_sampling_uses_a_fixed_bounded_worker_pool(self) -> None:
        service = _StatusService()
        store = _Store()
        collector = self._collector(service, store, _Settings(max_parallel_queries=3))
        targets = self._targets(20)

        await collector._sample_targets(targets)

        self.assertEqual(3, service.max_active)
        self.assertCountEqual([target.scope_id for target in targets], service.seen)

    async def test_start_is_idempotent_and_stop_cancels_sleep(self) -> None:
        service = _StatusService()
        store = _Store()
        target_calls = 0

        async def targets() -> list[ServerTarget]:
            nonlocal target_calls
            target_calls += 1
            return []

        collector = self._collector(service, store, _Settings(), targets=targets)
        self.assertTrue(collector.start())
        self.assertFalse(collector.start())
        await asyncio.wait_for(store.purged.wait(), timeout=1.0)
        self.assertTrue(collector.running)
        await collector.stop()

        self.assertFalse(collector.running)
        self.assertEqual(1, target_calls)
        self.assertEqual(1, len(store.cutoffs))

        await collector.close()
        await collector.ensure_started()
        self.assertTrue(collector.closed)
        self.assertFalse(collector.running)

    async def test_new_instance_retires_duplicate_collector_for_same_database(self) -> None:
        database_path = Path("shared-collector-test.sqlite3")
        first = self._collector(
            _StatusService(),
            _Store(database_path),
            _Settings(),
            initial_delay_seconds=3600,
        )
        second = self._collector(
            _StatusService(),
            _Store(database_path),
            _Settings(),
            initial_delay_seconds=3600,
        )

        self.assertTrue(first.start())
        self.assertTrue(second.start())
        await asyncio.sleep(0)

        self.assertTrue(first.closed)
        self.assertFalse(first.running)
        self.assertTrue(second.running)
        await first.ensure_started()
        self.assertFalse(first.running)

        await first.close()
        self.assertTrue(second.running)
        await second.close()


if __name__ == "__main__":
    unittest.main()
