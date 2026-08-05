from __future__ import annotations

import asyncio
import errno
import json
import os
import sqlite3
import threading
import time
import uuid
from collections.abc import Callable
from contextlib import contextmanager, suppress
from pathlib import Path
from typing import Any, TypeVar

from .minecraft.components import safe_favicon, sanitize_component
from .models import MinecraftStatus, ServerTarget
from .text import normalize_unicode

T = TypeVar("T")
LEGACY_MIGRATION_KEY = "astrbot_plugin_mc_motd:v1"
_STARTUP_LOCKS_GUARD = threading.Lock()
_STARTUP_LOCKS: dict[str, Any] = {}


@contextmanager
def _database_file_lock(destination: Path):
    """Serialize database startup across module reloads and processes."""

    # Keep the original suffix so a hot upgrade still coordinates with an old
    # instance that only used this lock for legacy migration.
    lock_path = destination.with_name(f"{destination.name}.migration.lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+b") as lock_file:
        lock_file.seek(0, os.SEEK_END)
        if lock_file.tell() == 0:
            lock_file.write(b"\0")
            lock_file.flush()
        lock_file.seek(0)

        if os.name == "nt":
            import msvcrt

            while True:
                try:
                    msvcrt.locking(lock_file.fileno(), msvcrt.LK_NBLCK, 1)
                    break
                except OSError as exc:
                    if exc.errno not in {errno.EACCES, errno.EDEADLK}:
                        raise
                    time.sleep(0.05)
            try:
                yield
            finally:
                lock_file.seek(0)
                with suppress(OSError):
                    msvcrt.locking(lock_file.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


def _database_lock_key(destination: Path) -> str:
    try:
        return os.path.normcase(str(destination.resolve()))
    except OSError:
        return os.path.normcase(str(destination.absolute()))


@contextmanager
def _database_startup_lock(destination: Path):
    """Guard migration and schema setup for one destination database."""

    lock_key = _database_lock_key(destination)
    with _STARTUP_LOCKS_GUARD:
        startup_lock = _STARTUP_LOCKS.setdefault(lock_key, threading.Lock())
    with startup_lock, _database_file_lock(destination):
        yield


def _initialize_schema(connection: sqlite3.Connection) -> None:
    connection.execute("PRAGMA journal_mode = WAL")
    connection.execute("PRAGMA synchronous = NORMAL")
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS servers (
            scope_id TEXT PRIMARY KEY,
            scope_label TEXT NOT NULL,
            server_name TEXT NOT NULL,
            host TEXT NOT NULL,
            port INTEGER NOT NULL,
            configured INTEGER NOT NULL DEFAULT 1,
            created_at REAL NOT NULL,
            updated_at REAL NOT NULL
        )
        """
    )
    server_columns = {
        row["name"] for row in connection.execute("PRAGMA table_info(servers)").fetchall()
    }
    if "configured" not in server_columns:
        connection.execute("ALTER TABLE servers ADD COLUMN configured INTEGER NOT NULL DEFAULT 1")
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS samples (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            scope_id TEXT NOT NULL DEFAULT '__default__',
            server_host TEXT NOT NULL DEFAULT '',
            server_port INTEGER NOT NULL DEFAULT 0,
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
        CREATE TABLE IF NOT EXISTS server_payloads (
            server_host TEXT NOT NULL,
            server_port INTEGER NOT NULL,
            favicon TEXT,
            updated_at REAL NOT NULL,
            PRIMARY KEY (server_host, server_port)
        )
        """
    )
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS plugin_migrations (
            name TEXT PRIMARY KEY,
            completed_at REAL NOT NULL
        )
        """
    )
    sample_columns = {
        row["name"] for row in connection.execute("PRAGMA table_info(samples)").fetchall()
    }
    if "scope_id" not in sample_columns:
        connection.execute(
            "ALTER TABLE samples ADD COLUMN scope_id TEXT NOT NULL DEFAULT '__default__'"
        )
    if "server_host" not in sample_columns:
        connection.execute("ALTER TABLE samples ADD COLUMN server_host TEXT NOT NULL DEFAULT ''")
    if "server_port" not in sample_columns:
        connection.execute("ALTER TABLE samples ADD COLUMN server_port INTEGER NOT NULL DEFAULT 0")
    connection.execute("CREATE INDEX IF NOT EXISTS idx_samples_sampled_at ON samples(sampled_at)")
    connection.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_samples_server
        ON samples(server_host, server_port)
        """
    )
    connection.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_samples_scope_server_time
        ON samples(scope_id, server_host, server_port, sampled_at)
        """
    )
    connection.execute("PRAGMA user_version = 2")


def migrate_legacy_database(source: Path, destination: Path) -> bool:
    """Copy or transactionally merge the legacy database exactly once.

    The source is always read-only. If no destination exists, SQLite's backup
    API captures the source and its WAL before an atomic rename. If both paths
    exist (common after v1.3.4), missing bindings and de-duplicated samples are
    merged into the newer destination without overwriting its configuration.
    """

    if not source.is_file():
        return False
    with _database_startup_lock(destination):
        return _migrate_legacy_database_locked(source, destination)


def _migrate_legacy_database_locked(source: Path, destination: Path) -> bool:
    if not source.is_file():
        return False
    try:
        if source.resolve() == destination.resolve():
            return False
    except OSError:
        pass
    destination.parent.mkdir(parents=True, exist_ok=True)

    if not destination.exists():
        _backup_database(source, destination)
        connection = sqlite3.connect(
            destination.resolve().as_uri(),
            timeout=5.0,
            uri=True,
        )
        connection.row_factory = sqlite3.Row
        try:
            with connection:
                _initialize_schema(connection)
                connection.execute(
                    "INSERT OR REPLACE INTO plugin_migrations (name, completed_at) VALUES (?, ?)",
                    (LEGACY_MIGRATION_KEY, time.time()),
                )
        finally:
            connection.close()
        return True

    connection = sqlite3.connect(
        destination.resolve().as_uri(),
        timeout=5.0,
        uri=True,
    )
    connection.row_factory = sqlite3.Row
    attached = False
    try:
        with connection:
            _initialize_schema(connection)
        completed = connection.execute(
            "SELECT 1 FROM plugin_migrations WHERE name = ?",
            (LEGACY_MIGRATION_KEY,),
        ).fetchone()
        if completed is not None:
            return False

        source_uri = f"{source.resolve().as_uri()}?mode=ro"
        connection.execute("ATTACH DATABASE ? AS legacy_db", (source_uri,))
        attached = True
        with connection:
            _merge_attached_legacy(connection)
            connection.execute(
                "INSERT INTO plugin_migrations (name, completed_at) VALUES (?, ?)",
                (LEGACY_MIGRATION_KEY, time.time()),
            )
        return True
    finally:
        if attached:
            with suppress(sqlite3.Error):
                connection.execute("DETACH DATABASE legacy_db")
        connection.close()


def _backup_database(source: Path, destination: Path) -> None:
    temporary = destination.with_name(
        f"{destination.name}.{os.getpid()}.{threading.get_ident()}.{uuid.uuid4().hex}.migrating"
    )
    source_connection: sqlite3.Connection | None = None
    destination_connection: sqlite3.Connection | None = None
    try:
        try:
            source_connection = sqlite3.connect(
                f"{source.resolve().as_uri()}?mode=ro",
                timeout=5.0,
                uri=True,
            )
            destination_connection = sqlite3.connect(temporary, timeout=5.0)
            source_connection.backup(destination_connection)
            destination_connection.commit()
        finally:
            if destination_connection is not None:
                destination_connection.close()
            if source_connection is not None:
                source_connection.close()
        os.replace(temporary, destination)
    except BaseException:
        if temporary.exists():
            temporary.unlink()
        raise


def _attached_columns(connection: sqlite3.Connection, table: str) -> set[str]:
    if table not in {"servers", "samples", "server_payloads"}:
        raise ValueError("unsupported legacy table")
    return {str(row["name"]) for row in connection.execute(f"PRAGMA legacy_db.table_info({table})")}


def _merge_attached_legacy(connection: sqlite3.Connection) -> None:
    tables = {
        str(row["name"])
        for row in connection.execute(
            "SELECT name FROM legacy_db.sqlite_master WHERE type = 'table'"
        )
    }

    if "servers" in tables:
        columns = _attached_columns(connection, "servers")
        required = {
            "scope_id",
            "scope_label",
            "server_name",
            "host",
            "port",
            "created_at",
            "updated_at",
        }
        if not required <= columns:
            raise ValueError("旧数据库 servers 表缺少必要字段")
        configured = "COALESCE(configured, 1)" if "configured" in columns else "1"
        connection.execute(
            f"""
            INSERT OR IGNORE INTO servers (
                scope_id, scope_label, server_name, host, port,
                configured, created_at, updated_at
            )
            SELECT scope_id, scope_label, server_name, host, port,
                   {configured}, created_at, updated_at
            FROM legacy_db.servers
            """
        )

    if "samples" in tables:
        columns = _attached_columns(connection, "samples")
        if not {"sampled_at", "success"} <= columns:
            raise ValueError("旧数据库 samples 表缺少必要字段")
        expressions = {
            "scope_id": (
                "COALESCE(legacy.scope_id, '__default__')"
                if "scope_id" in columns
                else "'__default__'"
            ),
            "server_host": (
                "COALESCE(legacy.server_host, '')" if "server_host" in columns else "''"
            ),
            "server_port": ("COALESCE(legacy.server_port, 0)" if "server_port" in columns else "0"),
        }
        for name in (
            "online",
            "max_players",
            "motd",
            "version_name",
            "latency_ms",
            "error",
            "raw_json",
        ):
            expressions[name] = f"legacy.{name}" if name in columns else "NULL"
        scope = expressions["scope_id"]
        host = expressions["server_host"]
        port = expressions["server_port"]
        connection.execute(
            f"""
                INSERT INTO samples (
                    scope_id, server_host, server_port, sampled_at,
                    success, online, max_players, motd, version_name,
                    latency_ms, error, raw_json
                )
                SELECT {scope}, {host}, {port}, legacy.sampled_at,
                       legacy.success, {expressions["online"]},
                       {expressions["max_players"]}, {expressions["motd"]},
                       {expressions["version_name"]}, {expressions["latency_ms"]},
                       {expressions["error"]}, {expressions["raw_json"]}
                FROM legacy_db.samples AS legacy
                WHERE NOT EXISTS (
                    SELECT 1 FROM samples AS existing
                    WHERE existing.scope_id = {scope}
                      AND existing.server_host = {host}
                      AND existing.server_port = {port}
                      AND existing.sampled_at = legacy.sampled_at
                )
            """
        )

    if "server_payloads" in tables:
        columns = _attached_columns(connection, "server_payloads")
        required = {"server_host", "server_port", "favicon", "updated_at"}
        if required <= columns:
            connection.execute(
                """
                INSERT OR IGNORE INTO server_payloads (
                    server_host, server_port, favicon, updated_at
                )
                SELECT server_host, server_port, favicon, updated_at
                FROM legacy_db.server_payloads
                WHERE favicon IS NOT NULL
                """
            )


class HistoryStore:
    """SQLite persistence isolated from the AstrBot event loop.

    Connections are deliberately short-lived.  All operations are serialized to
    keep schema migrations and writes deterministic, then executed in a worker
    thread so a slow filesystem cannot stall message handling.
    """

    def __init__(self, db_path: Path):
        self.db_path = db_path
        self._lock = asyncio.Lock()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.db_path, timeout=5.0)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout = 5000")
        connection.execute("PRAGMA synchronous = NORMAL")
        return connection

    def _with_connection(self, operation: Callable[[sqlite3.Connection], T]) -> T:
        connection = self._connect()
        try:
            with connection:
                return operation(connection)
        finally:
            connection.close()

    async def _run(self, operation: Callable[[sqlite3.Connection], T]) -> T:
        async with self._lock:
            worker = asyncio.create_task(asyncio.to_thread(self._with_connection, operation))
            try:
                return await asyncio.shield(worker)
            except asyncio.CancelledError as cancelled:
                # Cancelling to_thread does not stop its worker. Keep the lock
                # until SQLite is actually done so the next operation cannot
                # overlap the abandoned thread.
                while not worker.done():
                    with suppress(asyncio.CancelledError, Exception):
                        await asyncio.shield(worker)
                with suppress(Exception):
                    worker.result()
                raise cancelled

    def _init_db(self) -> None:
        with _database_startup_lock(self.db_path):
            self._with_connection(_initialize_schema)

    async def get_server(self, scope_id: str) -> sqlite3.Row | None:
        def operation(connection: sqlite3.Connection) -> sqlite3.Row | None:
            return connection.execute(
                """
                SELECT scope_id, scope_label, server_name, host, port, configured
                FROM servers
                WHERE scope_id = ?
                """,
                (scope_id,),
            ).fetchone()

        return await self._run(operation)

    async def list_servers(self) -> list[sqlite3.Row]:
        def operation(connection: sqlite3.Connection) -> list[sqlite3.Row]:
            return list(
                connection.execute(
                    """
                    SELECT scope_id, scope_label, server_name, host, port, configured
                    FROM servers
                    ORDER BY updated_at DESC
                    """
                )
            )

        return await self._run(operation)

    async def upsert_server(self, target: ServerTarget) -> None:
        now = time.time()

        def operation(connection: sqlite3.Connection) -> None:
            connection.execute(
                """
                INSERT INTO servers (
                    scope_id, scope_label, server_name, host, port,
                    configured, created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(scope_id) DO UPDATE SET
                    scope_label = excluded.scope_label,
                    server_name = excluded.server_name,
                    host = excluded.host,
                    port = excluded.port,
                    configured = excluded.configured,
                    updated_at = excluded.updated_at
                """,
                (
                    target.scope_id,
                    target.scope_label,
                    target.server_name,
                    target.host,
                    target.port,
                    1 if target.configured else 0,
                    now,
                    now,
                ),
            )

        await self._run(operation)

    async def delete_server(self, scope_id: str) -> None:
        def operation(connection: sqlite3.Connection) -> None:
            connection.execute(
                "DELETE FROM servers WHERE scope_id = ?",
                (scope_id,),
            )

        await self._run(operation)

    async def copy_scope_history(self, source_scope: str, destination_scope: str) -> None:
        if source_scope == destination_scope:
            return

        def operation(connection: sqlite3.Connection) -> None:
            connection.execute(
                """
                INSERT INTO samples (
                    scope_id, server_host, server_port, sampled_at,
                    success, online, max_players, motd, version_name,
                    latency_ms, error, raw_json
                )
                SELECT ?, legacy.server_host, legacy.server_port,
                       legacy.sampled_at, legacy.success, legacy.online,
                       legacy.max_players, legacy.motd, legacy.version_name,
                       legacy.latency_ms, legacy.error, legacy.raw_json
                FROM samples AS legacy
                WHERE legacy.scope_id = ?
                  AND NOT EXISTS (
                      SELECT 1 FROM samples AS existing
                      WHERE existing.scope_id = ?
                        AND existing.server_host = legacy.server_host
                        AND existing.server_port = legacy.server_port
                        AND existing.sampled_at = legacy.sampled_at
                  )
                """,
                (destination_scope, source_scope, destination_scope),
            )

        await self._run(operation)

    async def add_sample(self, scope_id: str, status: MinecraftStatus) -> None:
        source_json = status.raw_json if isinstance(status.raw_json, dict) else {}
        persisted_json: dict[str, object] = {}
        if "description" in source_json:
            persisted_json["description"] = sanitize_component(source_json["description"])
        favicon = safe_favicon(status.favicon) or safe_favicon(source_json.get("favicon"))
        raw_json_text = json.dumps(persisted_json, ensure_ascii=False) if persisted_json else None

        def operation(connection: sqlite3.Connection) -> None:
            connection.execute(
                """
                INSERT INTO samples (
                    scope_id, server_host, server_port, sampled_at,
                    success, online, max_players, motd, version_name,
                    latency_ms, error, raw_json
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    scope_id,
                    status.host,
                    status.port,
                    status.sampled_at,
                    1 if status.ok else 0,
                    status.online,
                    status.max_players,
                    normalize_unicode(status.motd_plain, 8192),
                    normalize_unicode(status.version_name, 256),
                    status.latency_ms,
                    normalize_unicode(status.error, 2048),
                    raw_json_text,
                ),
            )
            if favicon is not None:
                connection.execute(
                    """
                    INSERT INTO server_payloads (
                        server_host, server_port, favicon, updated_at
                    ) VALUES (?, ?, ?, ?)
                    ON CONFLICT(server_host, server_port) DO UPDATE SET
                        favicon = excluded.favicon,
                        updated_at = excluded.updated_at
                    """,
                    (status.host, status.port, favicon, status.sampled_at),
                )
            elif status.ok:
                # A successful response without a favicon means the endpoint
                # intentionally removed it. Failed samples must not erase the
                # last known good icon.
                connection.execute(
                    """
                    DELETE FROM server_payloads
                    WHERE server_host = ? AND server_port = ?
                    """,
                    (status.host, status.port),
                )

        await self._run(operation)

    async def purge_older_than(self, cutoff_ts: float) -> None:
        def operation(connection: sqlite3.Connection) -> None:
            connection.execute(
                "DELETE FROM samples WHERE sampled_at < ?",
                (cutoff_ts,),
            )
            connection.execute(
                """
                DELETE FROM server_payloads
                WHERE NOT EXISTS (
                    SELECT 1 FROM samples
                    WHERE samples.server_host = server_payloads.server_host
                      AND samples.server_port = server_payloads.server_port
                )
                """
            )

        await self._run(operation)

    async def load_history(
        self,
        scope_id: str,
        host: str,
        port: int,
        hours: int,
    ) -> list[sqlite3.Row]:
        cutoff = time.time() - max(1, hours) * 3600

        def operation(connection: sqlite3.Connection) -> list[sqlite3.Row]:
            return list(
                connection.execute(
                    """
                    SELECT sampled_at, success, online, max_players, latency_ms
                    FROM samples
                    WHERE scope_id = ?
                      AND server_host = ?
                      AND server_port = ?
                      AND sampled_at >= ?
                    ORDER BY sampled_at ASC
                    """,
                    (scope_id, host, port, cutoff),
                )
            )

        return await self._run(operation)

    async def latest_status(
        self,
        scope_id: str,
        host: str,
        port: int,
        max_age_seconds: int,
    ) -> sqlite3.Row | None:
        cutoff = time.time() - max(0, max_age_seconds)

        def operation(connection: sqlite3.Connection) -> sqlite3.Row | None:
            return connection.execute(
                """
                SELECT samples.sampled_at, samples.success, samples.online,
                       samples.max_players, samples.motd, samples.version_name,
                       samples.latency_ms, samples.error, samples.raw_json,
                       server_payloads.favicon AS endpoint_favicon
                FROM samples
                LEFT JOIN server_payloads
                  ON server_payloads.server_host = samples.server_host
                 AND server_payloads.server_port = samples.server_port
                WHERE samples.scope_id = ?
                  AND samples.server_host = ?
                  AND samples.server_port = ?
                  AND samples.sampled_at >= ?
                ORDER BY samples.sampled_at DESC
                LIMIT 1
                """,
                (scope_id, host, port, cutoff),
            ).fetchone()

        return await self._run(operation)

    async def clear(self, scope_id: str) -> None:
        def operation(connection: sqlite3.Connection) -> None:
            connection.execute(
                "DELETE FROM samples WHERE scope_id = ?",
                (scope_id,),
            )
            connection.execute(
                """
                DELETE FROM server_payloads
                WHERE NOT EXISTS (
                    SELECT 1 FROM samples
                    WHERE samples.server_host = server_payloads.server_host
                      AND samples.server_port = server_payloads.server_port
                )
                """
            )

        await self._run(operation)


def row_to_target(row: sqlite3.Row, configured: bool = True) -> ServerTarget:
    columns = set(row.keys())
    return ServerTarget(
        scope_id=str(row["scope_id"]),
        scope_label=str(row["scope_label"]),
        server_name=str(row["server_name"]),
        host=str(row["host"]),
        port=int(row["port"]),
        configured=bool(row["configured"]) if "configured" in columns else configured,
    )


def row_to_status(row: sqlite3.Row, target: ServerTarget) -> MinecraftStatus:
    columns = set(row.keys())
    raw_json = None
    raw_text = row["raw_json"] if "raw_json" in columns else None
    if raw_text:
        try:
            parsed = json.loads(str(raw_text))
            raw_json = parsed if isinstance(parsed, dict) else None
        except (TypeError, ValueError, json.JSONDecodeError):
            raw_json = None
    endpoint_favicon = row["endpoint_favicon"] if "endpoint_favicon" in columns else None
    if endpoint_favicon:
        if raw_json is None:
            raw_json = {}
        raw_json["favicon"] = endpoint_favicon
    return MinecraftStatus(
        ok=bool(row["success"]),
        sampled_at=float(row["sampled_at"]),
        host=target.host,
        port=target.port,
        online=int(row["online"]) if row["online"] is not None else None,
        max_players=int(row["max_players"]) if row["max_players"] is not None else None,
        motd_plain=str(row["motd"] or ""),
        version_name=str(row["version_name"] or ""),
        favicon=safe_favicon(raw_json.get("favicon") if raw_json else None),
        latency_ms=int(row["latency_ms"]) if row["latency_ms"] is not None else None,
        error=str(row["error"] or ""),
        raw_json=raw_json,
    )
