from __future__ import annotations

import asyncio
import json
import sqlite3
import tempfile
import threading
import time
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from simpmc_motd import storage as storage_module
from simpmc_motd.models import MinecraftStatus, ServerTarget
from simpmc_motd.rendering.background import fallback_background_data_uri
from simpmc_motd.storage import (
    HistoryStore,
    migrate_legacy_database,
    row_to_status,
    row_to_target,
)


class HistoryStoreTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self._temporary_directory = tempfile.TemporaryDirectory()
        self.db_path = Path(self._temporary_directory.name) / "nested" / "history.sqlite3"

    async def asyncTearDown(self) -> None:
        self._temporary_directory.cleanup()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.db_path)
        connection.row_factory = sqlite3.Row
        return connection

    async def test_same_database_schema_initialization_is_serialized(self) -> None:
        original_initialize = storage_module._initialize_schema
        start_barrier = threading.Barrier(2)
        counter_lock = threading.Lock()
        active = 0
        max_active = 0

        def tracked_initialize(connection: sqlite3.Connection) -> None:
            nonlocal active, max_active
            with counter_lock:
                active += 1
                max_active = max(max_active, active)
            try:
                time.sleep(0.05)
                original_initialize(connection)
            finally:
                with counter_lock:
                    active -= 1

        def construct_store() -> HistoryStore:
            start_barrier.wait(timeout=1.0)
            return HistoryStore(self.db_path)

        with patch.object(storage_module, "_initialize_schema", side_effect=tracked_initialize):
            stores = await asyncio.gather(
                asyncio.to_thread(construct_store),
                asyncio.to_thread(construct_store),
            )

        self.assertEqual(2, len(stores))
        self.assertEqual(1, max_active)

    async def test_server_upsert_preserves_creation_time_and_delete_keeps_samples(self) -> None:
        store = HistoryStore(self.db_path)
        initial = ServerTarget(
            scope_id="group:1",
            scope_label="群 1",
            server_name="Old",
            host="old.example",
            port=25565,
            configured=True,
        )
        updated = ServerTarget(
            scope_id="group:1",
            scope_label="群一",
            server_name="New",
            host="new.example",
            port=25566,
            configured=False,
        )
        with patch("simpmc_motd.storage.time.time", return_value=100.0):
            await store.upsert_server(initial)
        with patch("simpmc_motd.storage.time.time", return_value=200.0):
            await store.upsert_server(updated)

        row = await store.get_server("group:1")
        self.assertIsNotNone(row)
        assert row is not None
        self.assertEqual(updated, row_to_target(row))
        with closing(self._connect()) as connection:
            timestamps = connection.execute(
                "SELECT created_at, updated_at FROM servers WHERE scope_id = ?",
                ("group:1",),
            ).fetchone()
        self.assertEqual(100.0, timestamps["created_at"])
        self.assertEqual(200.0, timestamps["updated_at"])

        status = MinecraftStatus(
            ok=True,
            sampled_at=time.time(),
            host=updated.host,
            port=updated.port,
            online=3,
            max_players=10,
            motd_plain="online",
        )
        await store.add_sample(updated.scope_id, status)
        await store.delete_server(updated.scope_id)
        self.assertIsNone(await store.get_server(updated.scope_id))
        self.assertIsNotNone(
            await store.latest_status(
                updated.scope_id, updated.host, updated.port, max_age_seconds=60
            )
        )

    async def test_history_isolated_by_scope_and_server_and_round_trips_status(self) -> None:
        store = HistoryStore(self.db_path)
        now = time.time()
        favicon = fallback_background_data_uri(64, 64)
        statuses = (
            (
                "group:1",
                MinecraftStatus(
                    ok=True,
                    sampled_at=now - 30,
                    host="one.example",
                    port=25565,
                    online=5,
                    max_players=20,
                    motd_plain="first",
                    version_name="1.21",
                    latency_ms=8,
                    raw_json={"favicon": favicon, "description": {"text": "first"}},
                ),
            ),
            (
                "group:1",
                MinecraftStatus(
                    ok=False,
                    sampled_at=now - 10,
                    host="one.example",
                    port=25565,
                    error="offline",
                ),
            ),
            (
                "group:1",
                MinecraftStatus(
                    ok=True,
                    sampled_at=now - 5,
                    host="two.example",
                    port=25565,
                    online=99,
                    max_players=100,
                ),
            ),
            (
                "group:2",
                MinecraftStatus(
                    ok=True,
                    sampled_at=now - 2,
                    host="one.example",
                    port=25565,
                    online=8,
                    max_players=20,
                    raw_json={"favicon": favicon, "description": "shared endpoint"},
                ),
            ),
        )
        for scope_id, status in statuses:
            await store.add_sample(scope_id, status)

        with patch("simpmc_motd.storage.time.time", return_value=now):
            rows = await store.load_history("group:1", "one.example", 25565, hours=1)
            latest = await store.latest_status("group:1", "one.example", 25565, max_age_seconds=60)
        self.assertEqual([5, None], [row["online"] for row in rows])
        self.assertEqual([1, 0], [row["success"] for row in rows])
        self.assertIsNotNone(latest)
        assert latest is not None
        target = ServerTarget("group:1", "群 1", "One", "one.example", 25565)
        latest_status = row_to_status(latest, target)
        self.assertFalse(latest_status.ok)
        self.assertEqual("offline", latest_status.error)
        self.assertEqual(favicon, latest_status.favicon)

        with closing(self._connect()) as connection:
            successful_row = connection.execute(
                """
                SELECT sampled_at, success, online, max_players, motd,
                       version_name, latency_ms, error, raw_json
                FROM samples
                WHERE scope_id = ? AND server_host = ? AND server_port = ?
                  AND success = 1
                ORDER BY sampled_at DESC
                LIMIT 1
                """,
                ("group:1", "one.example", 25565),
            ).fetchone()
            endpoint_payloads = connection.execute(
                """
                SELECT favicon
                FROM server_payloads
                WHERE server_host = ? AND server_port = ?
                """,
                ("one.example", 25565),
            ).fetchall()
        self.assertIsNotNone(successful_row)
        assert successful_row is not None
        persisted_json = json.loads(successful_row["raw_json"])
        self.assertNotIn("favicon", persisted_json)
        self.assertEqual({"text": "first"}, persisted_json["description"])
        self.assertEqual(1, len(endpoint_payloads))
        self.assertEqual(favicon, endpoint_payloads[0]["favicon"])

        await store.purge_older_than(now - 20)
        with patch("simpmc_motd.storage.time.time", return_value=now):
            remaining = await store.load_history("group:1", "one.example", 25565, hours=1)
        self.assertEqual([0], [row["success"] for row in remaining])

        await store.clear("group:1")
        with patch("simpmc_motd.storage.time.time", return_value=now):
            self.assertEqual([], await store.load_history("group:1", "two.example", 25565, hours=1))
            other_scope = await store.load_history("group:2", "one.example", 25565, hours=1)
        self.assertEqual([8], [row["online"] for row in other_scope])

    async def test_concurrent_sample_writes_are_serialized(self) -> None:
        store = HistoryStore(self.db_path)
        now = time.time()
        statuses = [
            MinecraftStatus(
                ok=True,
                sampled_at=now + index / 100,
                host="parallel.example",
                port=25565,
                online=index,
                max_players=100,
            )
            for index in range(16)
        ]
        await asyncio.gather(*(store.add_sample("group:parallel", status) for status in statuses))
        with patch("simpmc_motd.storage.time.time", return_value=now):
            rows = await store.load_history("group:parallel", "parallel.example", 25565, hours=1)
        self.assertEqual(list(range(16)), [row["online"] for row in rows])

    async def test_cancellation_keeps_sqlite_worker_serialized(self) -> None:
        store = HistoryStore(self.db_path)
        started = threading.Event()
        release = threading.Event()
        order: list[str] = []

        def slow_operation(_connection: sqlite3.Connection) -> None:
            started.set()
            if not release.wait(timeout=2.0):
                raise TimeoutError("test worker was not released")
            order.append("slow")

        first = asyncio.create_task(store._run(slow_operation))
        self.assertTrue(await asyncio.to_thread(started.wait, 1.0))
        first.cancel()
        second = asyncio.create_task(store._run(lambda _connection: order.append("second")))
        await asyncio.sleep(0.05)
        self.assertFalse(second.done())
        release.set()
        with self.assertRaises(asyncio.CancelledError):
            await first
        await second
        self.assertEqual(["slow", "second"], order)

    async def test_scope_history_copy_is_idempotent(self) -> None:
        store = HistoryStore(self.db_path)
        now = time.time()
        for index in range(2):
            await store.add_sample(
                "group:legacy",
                MinecraftStatus(
                    ok=True,
                    sampled_at=now + index,
                    host="copy.example",
                    port=25565,
                    online=index,
                    max_players=20,
                ),
            )

        await store.copy_scope_history("group:legacy", "qq:group:legacy")
        await store.copy_scope_history("group:legacy", "qq:group:legacy")
        with patch("simpmc_motd.storage.time.time", return_value=now + 2):
            copied = await store.load_history(
                "qq:group:legacy",
                "copy.example",
                25565,
                hours=1,
            )
        self.assertEqual([0, 1], [row["online"] for row in copied])

    async def test_favicon_is_stored_once_and_not_repeated_in_sample_json(self) -> None:
        store = HistoryStore(self.db_path)
        now = time.time()
        favicon = fallback_background_data_uri(64, 64)
        for index in range(2):
            await store.add_sample(
                "group:payload",
                MinecraftStatus(
                    ok=True,
                    sampled_at=now + index,
                    host="payload.example",
                    port=25565,
                    online=index,
                    max_players=20,
                    motd_plain=f"sample {index}",
                    raw_json={
                        "favicon": favicon,
                        "description": {"text": f"sample {index}"},
                        "players": {"sample": [{"name": "must not persist"}]},
                    },
                ),
            )

        with closing(self._connect()) as connection:
            sample_rows = connection.execute(
                """
                SELECT raw_json
                FROM samples
                WHERE scope_id = ?
                ORDER BY sampled_at ASC
                """,
                ("group:payload",),
            ).fetchall()
            payload_rows = connection.execute(
                """
                SELECT favicon
                FROM server_payloads
                WHERE server_host = ? AND server_port = ?
                """,
                ("payload.example", 25565),
            ).fetchall()

        self.assertEqual(2, len(sample_rows))
        for row in sample_rows:
            persisted_json = json.loads(row["raw_json"])
            self.assertNotIn("favicon", persisted_json)
            self.assertNotIn("players", persisted_json)
        self.assertEqual(1, len(payload_rows))
        self.assertEqual(favicon, payload_rows[0]["favicon"])

    async def test_failed_sample_does_not_clear_existing_endpoint_favicon(self) -> None:
        store = HistoryStore(self.db_path)
        now = time.time()
        favicon = fallback_background_data_uri(64, 64)
        target = ServerTarget(
            "group:offline",
            "群 offline",
            "Offline",
            "offline.example",
            25565,
        )
        await store.add_sample(
            target.scope_id,
            MinecraftStatus(
                ok=True,
                sampled_at=now - 1,
                host=target.host,
                port=target.port,
                online=5,
                max_players=20,
                raw_json={"favicon": favicon, "description": "online"},
            ),
        )
        await store.add_sample(
            target.scope_id,
            MinecraftStatus(
                ok=False,
                sampled_at=now,
                host=target.host,
                port=target.port,
                error="offline",
            ),
        )

        latest = await store.latest_status(
            target.scope_id,
            target.host,
            target.port,
            max_age_seconds=60,
        )
        self.assertIsNotNone(latest)
        assert latest is not None
        restored = row_to_status(latest, target)
        self.assertFalse(restored.ok)
        self.assertEqual("offline", restored.error)
        self.assertEqual(favicon, restored.favicon)
        self.assertEqual(favicon, restored.raw_json["favicon"])

        with closing(self._connect()) as connection:
            payload = connection.execute(
                """
                SELECT favicon
                FROM server_payloads
                WHERE server_host = ? AND server_port = ?
                """,
                (target.host, target.port),
            ).fetchone()
        self.assertIsNotNone(payload)
        self.assertEqual(favicon, payload["favicon"])


class HistoryStoreMigrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_legacy_raw_json_favicon_remains_readable(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            db_path = Path(directory) / "history.sqlite3"
            sampled_at = time.time()
            favicon = fallback_background_data_uri(64, 64)
            with (
                closing(sqlite3.connect(db_path)) as connection,
                connection,
            ):
                connection.execute(
                    """
                    CREATE TABLE samples (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        scope_id TEXT NOT NULL,
                        server_host TEXT NOT NULL,
                        server_port INTEGER NOT NULL,
                        sampled_at REAL NOT NULL,
                        success INTEGER NOT NULL,
                        online INTEGER,
                        max_players INTEGER,
                        motd TEXT,
                        version_name TEXT,
                        latency_ms INTEGER,
                        error TEXT,
                        raw_json TEXT
                    )
                    """
                )
                connection.execute(
                    """
                    INSERT INTO samples (
                        scope_id, server_host, server_port, sampled_at,
                        success, online, max_players, motd, version_name,
                        latency_ms, error, raw_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        "group:legacy-favicon",
                        "legacy-favicon.example",
                        25565,
                        sampled_at,
                        1,
                        3,
                        20,
                        "legacy",
                        "old",
                        8,
                        "",
                        json.dumps(
                            {
                                "favicon": favicon,
                                "description": {"text": "legacy"},
                            }
                        ),
                    ),
                )

            store = HistoryStore(db_path)
            row = await store.latest_status(
                "group:legacy-favicon",
                "legacy-favicon.example",
                25565,
                max_age_seconds=60,
            )
            self.assertIsNotNone(row)
            assert row is not None
            target = ServerTarget(
                "group:legacy-favicon",
                "群 legacy-favicon",
                "Legacy",
                "legacy-favicon.example",
                25565,
            )
            restored = row_to_status(row, target)

            self.assertTrue(restored.ok)
            self.assertEqual(favicon, restored.favicon)
            self.assertEqual(favicon, restored.raw_json["favicon"])
            self.assertEqual({"text": "legacy"}, restored.raw_json["description"])

    async def test_legacy_database_migration_uses_backup_and_preserves_source(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "old" / "history.sqlite3"
            destination = root / "new" / "history.sqlite3"
            source.parent.mkdir(parents=True)

            source_connection = sqlite3.connect(source)
            try:
                source_connection.execute("PRAGMA journal_mode = WAL")
                source_connection.execute("PRAGMA wal_autocheckpoint = 0")
                source_connection.execute("CREATE TABLE legacy_marker (value TEXT NOT NULL)")
                source_connection.execute(
                    "INSERT INTO legacy_marker (value) VALUES (?)",
                    ("before migration",),
                )
                source_connection.commit()

                self.assertTrue(migrate_legacy_database(source, destination))
                self.assertTrue(source.is_file())
                self.assertTrue(destination.is_file())
                self.assertEqual(
                    [],
                    list(destination.parent.glob(f"{destination.name}.*.migrating")),
                )

                with closing(sqlite3.connect(destination)) as migrated:
                    migrated_value = migrated.execute("SELECT value FROM legacy_marker").fetchone()[
                        0
                    ]
                source_value = source_connection.execute(
                    "SELECT value FROM legacy_marker"
                ).fetchone()[0]
                self.assertEqual("before migration", migrated_value)
                self.assertEqual("before migration", source_value)

                source_connection.execute(
                    "INSERT INTO legacy_marker (value) VALUES (?)",
                    ("after migration",),
                )
                source_connection.commit()
                self.assertFalse(migrate_legacy_database(source, destination))
                with closing(sqlite3.connect(destination)) as migrated:
                    migrated_values = migrated.execute(
                        "SELECT value FROM legacy_marker ORDER BY rowid"
                    ).fetchall()
                self.assertEqual([("before migration",)], migrated_values)
            finally:
                source_connection.close()

    async def test_concurrent_first_migrations_share_one_destination_lock(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "old" / "history.sqlite3"
            destination = root / "new" / "history.sqlite3"
            source.parent.mkdir(parents=True)
            with closing(sqlite3.connect(source)) as connection, connection:
                connection.execute("CREATE TABLE marker (value TEXT NOT NULL)")
                connection.execute("INSERT INTO marker (value) VALUES ('legacy')")

            backup_entered = threading.Event()
            release_backup = threading.Event()
            backup_calls = 0
            original_backup = storage_module._backup_database

            def blocked_backup(source_path: Path, destination_path: Path) -> None:
                nonlocal backup_calls
                backup_calls += 1
                backup_entered.set()
                if not release_backup.wait(timeout=2.0):
                    raise TimeoutError("test did not release the migration backup")
                original_backup(source_path, destination_path)

            try:
                with patch.object(storage_module, "_backup_database", side_effect=blocked_backup):
                    first = asyncio.create_task(
                        asyncio.to_thread(migrate_legacy_database, source, destination)
                    )
                    self.assertTrue(await asyncio.to_thread(backup_entered.wait, 1.0))
                    second = asyncio.create_task(
                        asyncio.to_thread(migrate_legacy_database, source, destination)
                    )
                    await asyncio.sleep(0.05)
                    release_backup.set()
                    results = await asyncio.gather(first, second)
            finally:
                release_backup.set()

            self.assertCountEqual([True, False], results)
            self.assertEqual(1, backup_calls)
            self.assertTrue(destination.is_file())
            self.assertEqual(
                [],
                list(destination.parent.glob(f"{destination.name}.*.migrating")),
            )

    async def test_existing_destination_merges_once_without_overwriting_newer_bindings(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "old" / "history.sqlite3"
            destination = root / "new" / "history.sqlite3"
            source_store = HistoryStore(source)
            destination_store = HistoryStore(destination)
            now = time.time()
            legacy_target = ServerTarget(
                "group:shared",
                "群 shared",
                "Legacy name",
                "same.example",
                25565,
            )
            current_target = ServerTarget(
                "group:shared",
                "群 shared",
                "Current name",
                "same.example",
                25565,
            )
            legacy_only_target = ServerTarget(
                "group:legacy-only",
                "群 legacy-only",
                "Legacy only",
                "legacy.example",
                25566,
            )
            await source_store.upsert_server(legacy_target)
            await source_store.upsert_server(legacy_only_target)
            await destination_store.upsert_server(current_target)

            duplicate = MinecraftStatus(
                ok=True,
                sampled_at=now,
                host="same.example",
                port=25565,
                online=4,
                max_players=20,
            )
            await source_store.add_sample("group:shared", duplicate)
            await destination_store.add_sample("group:shared", duplicate)
            await source_store.add_sample(
                "group:shared",
                MinecraftStatus(
                    ok=True,
                    sampled_at=now + 1,
                    host="same.example",
                    port=25565,
                    online=5,
                    max_players=20,
                ),
            )
            await source_store.add_sample(
                "group:legacy-only",
                MinecraftStatus(
                    ok=True,
                    sampled_at=now + 2,
                    host="legacy.example",
                    port=25566,
                    online=6,
                    max_players=20,
                ),
            )

            self.assertTrue(migrate_legacy_database(source, destination))
            self.assertFalse(migrate_legacy_database(source, destination))

            merged_store = HistoryStore(destination)
            shared = await merged_store.get_server("group:shared")
            legacy_only = await merged_store.get_server("group:legacy-only")
            self.assertEqual("Current name", shared["server_name"])
            self.assertEqual("Legacy only", legacy_only["server_name"])
            with (
                closing(sqlite3.connect(source)) as source_connection,
                closing(sqlite3.connect(destination)) as destination_connection,
            ):
                source_count = source_connection.execute("SELECT COUNT(*) FROM samples").fetchone()[
                    0
                ]
                destination_count = destination_connection.execute(
                    "SELECT COUNT(*) FROM samples"
                ).fetchone()[0]
                marker_count = destination_connection.execute(
                    "SELECT COUNT(*) FROM plugin_migrations"
                ).fetchone()[0]
            self.assertEqual(3, source_count)
            self.assertEqual(3, destination_count)
            self.assertEqual(1, marker_count)

    async def test_legacy_schema_is_extended_without_losing_rows(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            db_path = Path(directory) / "history.sqlite3"
            sampled_at = time.time()
            with (
                closing(sqlite3.connect(db_path)) as connection,
                connection,
            ):
                connection.execute(
                    """
                    CREATE TABLE servers (
                        scope_id TEXT PRIMARY KEY,
                        scope_label TEXT NOT NULL,
                        server_name TEXT NOT NULL,
                        host TEXT NOT NULL,
                        port INTEGER NOT NULL,
                        created_at REAL NOT NULL,
                        updated_at REAL NOT NULL
                    )
                    """
                )
                connection.execute(
                    """
                    INSERT INTO servers (
                        scope_id, scope_label, server_name, host, port,
                        created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        "group:legacy",
                        "旧群",
                        "Legacy",
                        "legacy.example",
                        25565,
                        1,
                        2,
                    ),
                )
                connection.execute(
                    """
                    CREATE TABLE samples (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        sampled_at REAL NOT NULL,
                        success INTEGER NOT NULL,
                        online INTEGER,
                        max_players INTEGER,
                        motd TEXT,
                        version_name TEXT,
                        latency_ms INTEGER,
                        error TEXT,
                        raw_json TEXT
                    )
                    """
                )
                connection.execute(
                    """
                    INSERT INTO samples (
                        sampled_at, success, online, max_players, motd,
                        version_name, latency_ms, error, raw_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        sampled_at,
                        1,
                        4,
                        20,
                        "legacy motd",
                        "old",
                        7,
                        "",
                        "{}",
                    ),
                )

            store = HistoryStore(db_path)
            with closing(sqlite3.connect(db_path)) as connection:
                connection.row_factory = sqlite3.Row
                server_columns = {
                    row["name"] for row in connection.execute("PRAGMA table_info(servers)")
                }
                sample_columns = {
                    row["name"] for row in connection.execute("PRAGMA table_info(samples)")
                }
                indexes = {row["name"] for row in connection.execute("PRAGMA index_list(samples)")}
                schema_version = connection.execute("PRAGMA user_version").fetchone()[0]
                legacy_server = connection.execute(
                    "SELECT * FROM servers WHERE scope_id = 'group:legacy'"
                ).fetchone()
                legacy_sample = connection.execute("SELECT * FROM samples").fetchone()
                endpoint_plan = " ".join(
                    row["detail"]
                    for row in connection.execute(
                        """
                        EXPLAIN QUERY PLAN
                        SELECT 1 FROM samples
                        WHERE server_host = ? AND server_port = ?
                        """,
                        ("legacy.example", 25565),
                    )
                )

            self.assertIn("configured", server_columns)
            self.assertEqual(2, schema_version)
            self.assertTrue({"scope_id", "server_host", "server_port"} <= sample_columns)
            self.assertIn("idx_samples_sampled_at", indexes)
            self.assertIn("idx_samples_server", indexes)
            self.assertIn("idx_samples_server", endpoint_plan)
            self.assertIn("idx_samples_scope_server_time", indexes)
            self.assertEqual(1, legacy_server["configured"])
            self.assertEqual("__default__", legacy_sample["scope_id"])
            self.assertEqual("", legacy_sample["server_host"])
            self.assertEqual(0, legacy_sample["server_port"])
            self.assertEqual("legacy motd", legacy_sample["motd"])

            migrated = await store.latest_status("__default__", "", 0, max_age_seconds=60)
            self.assertIsNotNone(migrated)
            self.assertEqual(4, migrated["online"])


if __name__ == "__main__":
    unittest.main()
