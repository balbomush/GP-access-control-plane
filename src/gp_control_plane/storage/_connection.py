"""gp_control_plane.storage._connection — moved from storage.py (split)."""
from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any
import sqlite3
from gp_control_plane.storage._compact import _cleanup_runtime_state, _run_deferred_vacuum
from gp_control_plane.storage._constants import AUTH_BUSY_TIMEOUT_MS, _MIGRATED_DB_PATHS, _MIGRATION_LOCK
from gp_control_plane.storage._errors import _raise_storage_unavailable
from gp_control_plane.storage._paths import _secure_sqlite_files, db_path
from gp_control_plane.storage._schema import _migrate_schema
from gp_control_plane.storage._writes import _ensure_system_domain_presets_conn


class ClosingConnection(sqlite3.Connection):
    def __init__(self, database: str | Path, *args: Any, **kwargs: Any) -> None:
        super().__init__(database, *args, **kwargs)
        self._database_path = Path(database)

    def __exit__(self, exc_type: Any, exc_value: Any, traceback: Any) -> bool:
        try:
            try:
                return bool(super().__exit__(exc_type, exc_value, traceback))
            except sqlite3.OperationalError as error:
                _raise_storage_unavailable(error)
        finally:
            self.close()

    def execute(self, *args: Any, **kwargs: Any) -> sqlite3.Cursor:
        try:
            return super().execute(*args, **kwargs)
        except sqlite3.OperationalError as error:
            _raise_storage_unavailable(error)

    def executemany(self, *args: Any, **kwargs: Any) -> sqlite3.Cursor:
        try:
            return super().executemany(*args, **kwargs)
        except sqlite3.OperationalError as error:
            _raise_storage_unavailable(error)

    def executescript(self, *args: Any, **kwargs: Any) -> sqlite3.Cursor:
        try:
            return super().executescript(*args, **kwargs)
        except sqlite3.OperationalError as error:
            _raise_storage_unavailable(error)

    def commit(self) -> None:
        try:
            super().commit()
        except sqlite3.OperationalError as error:
            _raise_storage_unavailable(error)

    def close(self) -> None:
        try:
            super().close()
        finally:
            _secure_sqlite_files(self._database_path)


def connect(
    state_dir: Path,
    *,
    check_same_thread: bool = True,
    busy_timeout_ms: int | None = None,
) -> sqlite3.Connection:
    """Open storage with the default 30s timeout or a caller-specific budget."""
    try:
        path = db_path(state_dir)
        timeout_seconds = 30 if busy_timeout_ms is None else max(0, busy_timeout_ms) / 1000
        conn = sqlite3.connect(
            path,
            timeout=timeout_seconds,
            factory=ClosingConnection,
            check_same_thread=check_same_thread,
        )
        conn.row_factory = sqlite3.Row
        migration_key = path.resolve()
        with _MIGRATION_LOCK:
            if migration_key not in _MIGRATED_DB_PATHS:
                conn.execute("PRAGMA journal_mode=WAL")
                conn.execute("PRAGMA synchronous=NORMAL")
                conn.execute("PRAGMA foreign_keys=ON")
                _migrate_schema(conn)
                _cleanup_runtime_state(conn, path.parent)
                _ensure_system_domain_presets_conn(conn)
                _run_deferred_vacuum(conn, state_dir)
                _MIGRATED_DB_PATHS.add(migration_key)
                _secure_sqlite_files(path)
                return conn
            conn.execute("PRAGMA synchronous=NORMAL")
            conn.execute("PRAGMA foreign_keys=ON")
        _secure_sqlite_files(path)
        return conn
    except sqlite3.OperationalError as error:
        _raise_storage_unavailable(error)


@contextmanager
def auth_transaction(
    state_dir: Path, *, busy_timeout_ms: int = AUTH_BUSY_TIMEOUT_MS
) -> Iterator[sqlite3.Connection]:
    """Run a short auth operation under a cross-process SQLite write lock."""
    try:
        timeout_ms = max(0, int(busy_timeout_ms))
    except (TypeError, ValueError):
        timeout_ms = AUTH_BUSY_TIMEOUT_MS
    # sqlite3.connect() installs this busy timeout before the initialization
    # PRAGMAs and migrations below can issue a blocking SQLite operation.
    conn = connect(state_dir, busy_timeout_ms=timeout_ms)
    try:
        # A first connection may have just applied schema migrations. Finish that
        # setup transaction before taking the dedicated auth write lock.
        if conn.in_transaction:
            conn.commit()
        conn.execute("BEGIN IMMEDIATE")
        try:
            yield conn
        except BaseException:
            try:
                conn.rollback()
            except sqlite3.OperationalError as error:
                _raise_storage_unavailable(error)
            raise
        else:
            conn.commit()
    except sqlite3.OperationalError as error:
        _raise_storage_unavailable(error)
    finally:
        conn.close()


@contextmanager
def auth_read_snapshot(
    state_dir: Path, *, busy_timeout_ms: int = AUTH_BUSY_TIMEOUT_MS
) -> Iterator[sqlite3.Connection | None]:
    """Read an existing auth record without migrations or a writer transaction.

    ``None`` means that the database does not exist yet.  Callers must then use
    :func:`auth_transaction` to perform the initial schema/auth bootstrap.
    """
    try:
        timeout_ms = max(0, int(busy_timeout_ms))
    except (TypeError, ValueError):
        timeout_ms = AUTH_BUSY_TIMEOUT_MS

    path = state_dir / "strategy-finder" / "state.sqlite3"
    if not path.is_file():
        yield None
        return

    conn: sqlite3.Connection | None = None
    try:
        # ``mode=ro`` and ``query_only`` guarantee this path cannot create,
        # migrate, or modify the database.  BEGIN is deferred: the SELECT made
        # by the caller obtains a WAL reader snapshot without competing for the
        # live writer's RESERVED lock.
        conn = sqlite3.connect(
            f"{path.resolve().as_uri()}?mode=ro",
            uri=True,
            timeout=timeout_ms / 1000,
        )
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA query_only=ON")
        conn.execute("BEGIN")
        yield conn
    except sqlite3.OperationalError as error:
        _raise_storage_unavailable(error)
    finally:
        if conn is not None:
            try:
                conn.rollback()
            except sqlite3.OperationalError as error:
                _raise_storage_unavailable(error)
            finally:
                conn.close()
