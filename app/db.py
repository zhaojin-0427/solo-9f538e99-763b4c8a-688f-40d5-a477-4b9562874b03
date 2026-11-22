"""SQLite persistence with idempotent, atomic event ingestion."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Iterator, List, Optional, Tuple

from .matching import Conversion, Touchpoint, run_attribution
from .models import ApiError, AttributeRequest, TouchpointIn


SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    user_id          TEXT PRIMARY KEY,
    window_seconds   INTEGER NOT NULL,
    default_capacity INTEGER NOT NULL,
    created_at       TEXT NOT NULL,
    updated_at       TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS events (
    user_id       TEXT NOT NULL,
    event_id      TEXT NOT NULL,
    event_type    TEXT NOT NULL CHECK (event_type IN ('touchpoint', 'conversion')),
    occurred_at   TEXT NOT NULL,
    channel       TEXT,
    priority      INTEGER,
    capacity      INTEGER,
    value         REAL,
    payload_hash  TEXT NOT NULL,
    PRIMARY KEY (user_id, event_id),
    FOREIGN KEY (user_id) REFERENCES users(user_id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_events_user_type_time
    ON events (user_id, event_type, occurred_at);
"""


def to_db_time(value: datetime) -> str:
    """Canonical fixed-width UTC timestamp so text ordering is chronological."""

    utc = value.astimezone(timezone.utc)
    # Fixed width, zero padded year keeps lexicographic = chronological order.
    return (
        f"{utc.year:04d}-{utc.month:02d}-{utc.day:02d}T"
        f"{utc.hour:02d}:{utc.minute:02d}:{utc.second:02d}.{utc.microsecond:06d}Z"
    )


def from_db_time(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


class Database:
    """Thin repository around a single SQLite file.

    A connection is opened per operation.  Writes use ``BEGIN IMMEDIATE`` so
    concurrent requests cannot interleave the idempotency check with inserts.
    """

    def __init__(self, path: str) -> None:
        self.path = path
        self._keeper: Optional[sqlite3.Connection] = None
        if path == ":memory:":
            # Every sqlite3 connection to ":memory:" gets its own database.
            # A shared-cache URI plus a keeper connection keeps all operations
            # on one in-memory database (handy for tests and demos).
            self._uri = "file:attribution_memory?mode=memory&cache=shared"
            self._keeper = sqlite3.connect(
                self._uri, uri=True, timeout=30.0, isolation_level=None
            )
        else:
            self._uri = path

    def connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(
            self._uri,
            uri=self.path == ":memory:",
            timeout=30.0,
            isolation_level=None,
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 30000")
        return connection

    def close(self) -> None:
        if self._keeper is not None:
            self._keeper.close()
            self._keeper = None

    def initialize(self) -> None:
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        connection = self.connect()
        try:
            connection.execute("PRAGMA journal_mode = WAL")
            connection.executescript(SCHEMA)
        finally:
            connection.close()

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        connection = self.connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            yield connection
            connection.execute("COMMIT")
        except BaseException:
            connection.execute("ROLLBACK")
            raise
        finally:
            connection.close()

    @contextmanager
    def read(self) -> Iterator[sqlite3.Connection]:
        connection = self.connect()
        try:
            # A read transaction gives one consistent snapshot when the
            # computation issues multiple SELECTs.
            connection.execute("BEGIN")
            yield connection
            connection.execute("COMMIT")
        finally:
            connection.close()

    # -- ingestion ---------------------------------------------------------

    def ingest(self, request: AttributeRequest) -> dict:
        """Insert a batch atomically, then recompute attribution.

        Duplicate event ids with an identical payload are ignored.  A reused
        id with different content rejects the whole batch with 409 and leaves
        the database untouched.
        """

        now = to_db_time(datetime.now(timezone.utc))
        prepared = self._prepare_events(request)

        with self.transaction() as connection:
            existing = self._load_hashes(connection, request.user_id)
            conflicts = sorted(
                event_id
                for event_id, payload_hash, _ in prepared
                if event_id in existing and existing[event_id] != payload_hash
            )
            if conflicts:
                raise ApiError(
                    409,
                    "event_id_conflict",
                    "事件 ID 已存在但内容不同，整批请求未写入。",
                    [{"event_id": event_id} for event_id in conflicts],
                )

            duplicates = sum(
                1 for event_id, _, _ in prepared if event_id in existing
            )
            inserted = len(prepared) - duplicates

            self._upsert_user(connection, request, now)
            if inserted:
                for event_id, payload_hash, event in prepared:
                    if event_id in existing:
                        continue
                    self._insert_event(
                        connection, request.user_id, event, payload_hash
                    )

            result = self._compute_locked(connection, request.user_id)
            result["received"] = {
                "touchpoints": len(request.touchpoints),
                "conversions": len(request.conversions),
                "inserted": inserted,
                "duplicates_ignored": duplicates,
            }
            return result

    def _prepare_events(
        self, request: AttributeRequest
    ) -> List[Tuple[str, str, object]]:
        """Validate in-batch duplicate ids and return (id, payload hash)."""

        seen: Dict[str, str] = {}
        prepared: List[Tuple[str, str, object]] = []
        conflicts: List[str] = []
        for event in list(request.touchpoints) + list(request.conversions):
            payload_hash = event_payload_hash(event)
            previous = seen.get(event.id)
            if previous is None:
                seen[event.id] = payload_hash
                prepared.append((event.id, payload_hash, event))
            elif previous != payload_hash:
                conflicts.append(event.id)
        if conflicts:
            raise ApiError(
                422,
                "duplicate_event_id",
                "同一批请求中事件 ID 被不同内容重复使用。",
                [{"event_id": event_id} for event_id in sorted(set(conflicts))],
            )
        return prepared

    def _load_hashes(
        self, connection: sqlite3.Connection, user_id: str
    ) -> Dict[str, str]:
        rows = connection.execute(
            "SELECT event_id, payload_hash FROM events WHERE user_id = ?",
            (user_id,),
        ).fetchall()
        return {row["event_id"]: row["payload_hash"] for row in rows}

    def _upsert_user(
        self,
        connection: sqlite3.Connection,
        request: AttributeRequest,
        now: str,
    ) -> None:
        connection.execute(
            """
            INSERT INTO users (user_id, window_seconds, default_capacity,
                               created_at, updated_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                window_seconds = excluded.window_seconds,
                default_capacity = excluded.default_capacity,
                updated_at = excluded.updated_at
            """,
            (
                request.user_id,
                request.attribution_window_seconds,
                request.default_touch_capacity,
                now,
                now,
            ),
        )

    def _insert_event(
        self,
        connection: sqlite3.Connection,
        user_id: str,
        event,
        payload_hash: str,
    ) -> None:
        if isinstance(event, TouchpointIn):
            connection.execute(
                """
                INSERT INTO events (user_id, event_id, event_type, occurred_at,
                                    channel, priority, capacity, value, payload_hash)
                VALUES (?, ?, 'touchpoint', ?, ?, ?, ?, NULL, ?)
                """,
                (
                    user_id,
                    event.id,
                    to_db_time(event.occurred_at),
                    event.channel,
                    event.priority,
                    event.capacity,
                    payload_hash,
                ),
            )
        else:
            connection.execute(
                """
                INSERT INTO events (user_id, event_id, event_type, occurred_at,
                                    channel, priority, capacity, value, payload_hash)
                VALUES (?, ?, 'conversion', ?, NULL, NULL, NULL, ?, ?)
                """,
                (
                    user_id,
                    event.id,
                    to_db_time(event.occurred_at),
                    event.value,
                    payload_hash,
                ),
            )

    # -- reads -------------------------------------------------------------

    def list_users(self) -> List[dict]:
        with self.read() as connection:
            rows = connection.execute(
                """
                SELECT u.user_id, u.window_seconds, u.default_capacity,
                       u.updated_at,
                       COALESCE(SUM(e.event_type = 'touchpoint'), 0) AS touchpoints,
                       COALESCE(SUM(e.event_type = 'conversion'), 0) AS conversions
                FROM users u
                LEFT JOIN events e ON e.user_id = u.user_id
                GROUP BY u.user_id
                ORDER BY u.updated_at DESC, u.user_id ASC
                """
            ).fetchall()
        return [
            {
                "user_id": row["user_id"],
                "attribution_window_seconds": row["window_seconds"],
                "default_touch_capacity": row["default_capacity"],
                "updated_at": row["updated_at"],
                "touchpoints": row["touchpoints"],
                "conversions": row["conversions"],
            }
            for row in rows
        ]

    def get_attribution(self, user_id: str) -> dict:
        with self.read() as connection:
            user = connection.execute(
                "SELECT * FROM users WHERE user_id = ?", (user_id,)
            ).fetchone()
            if user is None:
                raise ApiError(404, "user_not_found", f"用户 {user_id!r} 不存在。")
            return self._compute_locked(connection, user_id)

    def get_user_events(self, user_id: str) -> dict:
        with self.read() as connection:
            user = connection.execute(
                "SELECT * FROM users WHERE user_id = ?", (user_id,)
            ).fetchone()
            if user is None:
                raise ApiError(404, "user_not_found", f"用户 {user_id!r} 不存在。")
            rows = connection.execute(
                "SELECT * FROM events WHERE user_id = ? "
                "ORDER BY occurred_at ASC, event_id ASC",
                (user_id,),
            ).fetchall()
        return {
            "user_id": user_id,
            "settings": {
                "attribution_window_seconds": user["window_seconds"],
                "default_touch_capacity": user["default_capacity"],
            },
            "events": [
                {
                    "event_id": row["event_id"],
                    "type": row["event_type"],
                    "occurred_at": row["occurred_at"],
                    "channel": row["channel"],
                    "priority": row["priority"],
                    "capacity": row["capacity"],
                    "value": row["value"],
                }
                for row in rows
            ],
        }

    def health(self) -> bool:
        try:
            with self.read() as connection:
                connection.execute("SELECT 1").fetchone()
            return True
        except sqlite3.Error:
            return False

    # -- computation -------------------------------------------------------

    def _compute_locked(
        self, connection: sqlite3.Connection, user_id: str
    ) -> dict:
        user = connection.execute(
            "SELECT * FROM users WHERE user_id = ?", (user_id,)
        ).fetchone()
        if user is None:
            raise ApiError(404, "user_not_found", f"用户 {user_id!r} 不存在。")
        rows = connection.execute(
            "SELECT * FROM events WHERE user_id = ? "
            "ORDER BY occurred_at ASC, event_id ASC",
            (user_id,),
        ).fetchall()

        touchpoints: List[Touchpoint] = []
        conversions: List[Conversion] = []
        for row in rows:
            occurred_at = from_db_time(row["occurred_at"])
            if row["event_type"] == "touchpoint":
                touchpoints.append(
                    Touchpoint(
                        id=row["event_id"],
                        occurred_at=occurred_at,
                        channel=row["channel"],
                        priority=row["priority"],
                        capacity=row["capacity"],
                    )
                )
            else:
                conversions.append(
                    Conversion(
                        id=row["event_id"],
                        occurred_at=occurred_at,
                        value=row["value"],
                    )
                )

        result = run_attribution(
            touchpoints=touchpoints,
            conversions=conversions,
            window_seconds=user["window_seconds"],
            default_capacity=user["default_capacity"],
        )
        result["user_id"] = user_id
        result["updated_at"] = user["updated_at"]
        return result


def event_payload_hash(event) -> str:
    """Stable hash of every field that affects attribution."""

    if isinstance(event, TouchpointIn):
        payload = {
            "type": "touchpoint",
            "id": event.id,
            "occurred_at": to_db_time(event.occurred_at),
            "channel": event.channel,
            "priority": event.priority,
            "capacity": event.capacity,
        }
    else:
        payload = {
            "type": "conversion",
            "id": event.id,
            "occurred_at": to_db_time(event.occurred_at),
            "value": float(event.value),
        }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


__all__ = ["Database", "event_payload_hash", "from_db_time", "to_db_time"]
