from __future__ import annotations

import asyncio
import threading
import unittest
from unittest.mock import patch

from tests import test_main_adapter as _adapter


class CandidateMessageRaceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.harness = _adapter.MainAdapterSmokeTests(
            methodName="test_direct_import_registers_regex_and_settings_page_apis",
        )
        await self.harness.asyncSetUp()

    async def asyncTearDown(self) -> None:
        await self.harness.asyncTearDown()

    async def test_motd_does_not_retire_a_candidate_waiting_to_take_over(self) -> None:
        older = self.harness.main.MinecraftMotdPlugin(
            _adapter._ContextStub(),
            _adapter._ConfigStub({"background_image_url": " "}),
        )
        newer = self.harness.main.MinecraftMotdPlugin(
            _adapter._ContextStub(),
            _adapter._ConfigStub({"background_image_url": " "}),
        )
        self.harness._plugins.extend((older, newer))

        older_prepare_entered = threading.Event()
        newer_prepare_entered = threading.Event()
        release_older = threading.Event()
        release_newer = threading.Event()
        original_older_prepare = older._prepare_store

        def blocked_older_prepare():
            older_prepare_entered.set()
            if not release_older.wait(timeout=2.0):
                raise TimeoutError("test did not release older candidate")
            return original_older_prepare()

        def failed_newer_prepare():
            newer_prepare_entered.set()
            if not release_newer.wait(timeout=2.0):
                raise TimeoutError("test did not release newer candidate")
            raise OSError("newer preparation failed")

        older_initialization: asyncio.Task[None] | None = None
        newer_initialization: asyncio.Task[None] | None = None
        try:
            with (
                patch.object(older, "_prepare_store", side_effect=blocked_older_prepare),
                patch.object(newer, "_prepare_store", side_effect=failed_newer_prepare),
            ):
                older_initialization = asyncio.create_task(older.initialize())
                self.assertTrue(
                    await asyncio.to_thread(older_prepare_entered.wait, 1.0),
                )
                newer_initialization = asyncio.create_task(newer.initialize())
                self.assertTrue(
                    await asyncio.to_thread(newer_prepare_entered.wait, 1.0),
                )

                release_older.set()
                for _attempt in range(100):
                    if older.collector is not None:
                        break
                    await asyncio.sleep(0.01)

                self.assertIsNotNone(older.collector)
                self.assertFalse(older_initialization.done())
                self.assertFalse(older._owns_runtime_slot())

                results = await _adapter._collect(older.motd(_adapter._EventStub()))
                collector_closed_after_message = older.collector.closed

                release_newer.set()
                newer_result, older_result = await asyncio.wait_for(
                    asyncio.gather(
                        newer_initialization,
                        older_initialization,
                        return_exceptions=True,
                    ),
                    timeout=2.0,
                )
        finally:
            release_older.set()
            release_newer.set()
            tasks = tuple(
                task for task in (older_initialization, newer_initialization) if task is not None
            )
            for task in tasks:
                if not task.done():
                    task.cancel()
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)

        self.assertEqual([], results)
        self.assertFalse(collector_closed_after_message)
        self.assertIsInstance(newer_result, OSError)
        self.assertIsNone(older_result)
        self.assertTrue(older._owns_runtime_slot())
        self.assertTrue(older.collector.running)
        self.assertFalse(older.collector.closed)


if __name__ == "__main__":
    unittest.main()
