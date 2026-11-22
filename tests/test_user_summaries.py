"""Tests for the denormalised per-user summary behind ``GET /api/users``.

The summary must always equal the event detail: only first-time event
inserts move the counters, while settings updates, duplicate/conflict
requests and rollbacks never do.  Startup backfill and interleaved writes
are covered as well.
"""

from __future__ import annotations

import sqlite3
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from app.db import Database, to_db_time
from app.main import create_app
from app.models import AttributeRequest


def payload(user_id="u1", touchpoints=("t1",), conversions=("c1",), **overrides):
    body = {
        "user_id": user_id,
        "attribution_window_seconds": overrides.pop("window", 86400),
        "default_touch_capacity": overrides.pop("capacity", 1),
        "touchpoints": [
            {
                "id": event_id,
                "occurred_at": "2026-11-01T00:00:00Z",
                "channel": "SEO",
            }
            for event_id in touchpoints
        ],
        "conversions": [
            {"id": event_id, "occurred_at": "2026-11-01T12:00:00Z", "value": 1}
            for event_id in conversions
        ],
    }
    body.update(overrides)
    return body


def detail_counts(db_path: str) -> dict:
    """Authoritative counts recomputed straight from the events table."""

    connection = sqlite3.connect(db_path)
    try:
        rows = connection.execute(
            "SELECT user_id, event_type, COUNT(*) FROM events GROUP BY user_id, event_type"
        ).fetchall()
    finally:
        connection.close()
    counts: dict = {}
    for user_id, event_type, count in rows:
        bucket = counts.setdefault(user_id, {"touchpoints": 0, "conversions": 0})
        bucket[
            "touchpoints" if event_type == "touchpoint" else "conversions"
        ] = count
    return counts


def summary_rows(db_path: str) -> dict:
    connection = sqlite3.connect(db_path)
    try:
        rows = connection.execute(
            "SELECT user_id, touchpoint_count, conversion_count, updated_at "
            "FROM user_summaries"
        ).fetchall()
    finally:
        connection.close()
    return {
        user_id: {
            "touchpoints": touchpoint_count,
            "conversions": conversion_count,
            "updated_at": updated_at,
        }
        for user_id, touchpoint_count, conversion_count, updated_at in rows
    }


def assert_summary_matches_detail(db_path: str) -> None:
    details = detail_counts(db_path)
    summaries = summary_rows(db_path)
    assert set(summaries) == set(details)
    for user_id, bucket in details.items():
        assert summaries[user_id]["touchpoints"] == bucket["touchpoints"]
        assert summaries[user_id]["conversions"] == bucket["conversions"]


@pytest.fixture()
def client(tmp_path):
    db_path = str(tmp_path / "test.db")
    app = create_app(db_path)
    with TestClient(app) as test_client:
        yield test_client, db_path


def test_summary_counts_first_write_and_response_shape(client):
    test_client, db_path = client
    response = test_client.post(
        "/attribute",
        json=payload(touchpoints=("t1", "t2"), conversions=("c1", "c2", "c3")),
    )
    assert response.status_code == 200

    users = test_client.get("/api/users").json()["users"]
    assert len(users) == 1
    assert set(users[0]) == {
        "user_id",
        "attribution_window_seconds",
        "default_touch_capacity",
        "updated_at",
        "touchpoints",
        "conversions",
    }
    assert users[0]["touchpoints"] == 2
    assert users[0]["conversions"] == 3
    assert_summary_matches_detail(db_path)


def test_duplicate_batch_does_not_move_counters(client):
    test_client, db_path = client
    body = payload(touchpoints=("t1",), conversions=("c1",))
    test_client.post("/attribute", json=body)
    before = summary_rows(db_path)["u1"]

    # Identical resubmission: every event is a duplicate, counters must stay.
    response = test_client.post("/attribute", json=body)
    assert response.json()["received"]["inserted"] == 0
    after = summary_rows(db_path)["u1"]
    assert after["touchpoints"] == before["touchpoints"] == 1
    assert after["conversions"] == before["conversions"] == 1
    assert_summary_matches_detail(db_path)


def test_settings_only_update_keeps_counters(client):
    test_client, db_path = client
    body = payload(touchpoints=("t1", "t2"), conversions=("c1",))
    test_client.post("/attribute", json=body)
    before = summary_rows(db_path)["u1"]

    body["attribution_window_seconds"] = 60
    response = test_client.post("/attribute", json=body)
    assert response.status_code == 200
    after = summary_rows(db_path)["u1"]
    assert after["touchpoints"] == before["touchpoints"] == 2
    assert after["conversions"] == before["conversions"] == 1
    # updated_at mirrors the users table so list ordering is unchanged.
    assert after["updated_at"] >= before["updated_at"]
    assert_summary_matches_detail(db_path)


def test_conflict_request_leaves_summary_untouched(client):
    test_client, db_path = client
    test_client.post("/attribute", json=payload(touchpoints=("t1",)))
    before = summary_rows(db_path)["u1"]

    conflicting = payload(
        touchpoints=("t1",), conversions=("c1",)
    )
    # Reuse t1 with different content -> 409, whole batch rolled back.
    conflicting["touchpoints"][0]["channel"] = "Changed"
    response = test_client.post("/attribute", json=conflicting)
    assert response.status_code == 409
    assert summary_rows(db_path)["u1"] == before

    # New events after the rejected batch still count exactly once.
    ok = payload(touchpoints=("t2",), conversions=("c1",))
    response = test_client.post("/attribute", json=ok)
    assert response.status_code == 200
    after = summary_rows(db_path)["u1"]
    assert after["touchpoints"] == 2
    assert after["conversions"] == 1
    assert_summary_matches_detail(db_path)


def test_in_batch_duplicate_422_creates_no_summary(client):
    test_client, db_path = client
    bad = payload(touchpoints=("t1",), conversions=("c1",))
    bad["conversions"].append(
        {"id": "c1", "occurred_at": "2026-11-01T20:00:00Z", "value": 99}
    )
    assert test_client.post("/attribute", json=bad).status_code == 422
    assert test_client.get("/api/users").json()["count"] == 0
    assert summary_rows(db_path) == {}


def test_partial_new_and_duplicate_batch_counts_only_new(client):
    test_client, db_path = client
    test_client.post(
        "/attribute", json=payload(touchpoints=("t1",), conversions=("c1",))
    )
    test_client.post(
        "/attribute",
        json=payload(touchpoints=("t1", "t2"), conversions=("c1", "c2")),
    )
    row = summary_rows(db_path)["u1"]
    assert row["touchpoints"] == 2
    assert row["conversions"] == 2
    assert_summary_matches_detail(db_path)


def test_legacy_database_is_backfilled_from_events_on_startup(tmp_path):
    db_path = str(tmp_path / "legacy.db")
    # Simulate a database written by the previous version: no summary table.
    connection = sqlite3.connect(db_path)
    connection.executescript(
        """
        CREATE TABLE users (
            user_id TEXT PRIMARY KEY, window_seconds INTEGER NOT NULL,
            default_capacity INTEGER NOT NULL,
            created_at TEXT NOT NULL, updated_at TEXT NOT NULL
        );
        CREATE TABLE events (
            user_id TEXT NOT NULL, event_id TEXT NOT NULL,
            event_type TEXT NOT NULL CHECK (event_type IN ('touchpoint','conversion')),
            occurred_at TEXT NOT NULL, channel TEXT, priority INTEGER,
            capacity INTEGER, value REAL, payload_hash TEXT NOT NULL,
            PRIMARY KEY (user_id, event_id)
        );
        """
    )
    stamp = to_db_time(datetime(2026, 11, 1, tzinfo=timezone.utc))
    connection.execute(
        "INSERT INTO users VALUES ('legacy', 60, 1, ?, ?)", (stamp, stamp)
    )
    connection.executemany(
        "INSERT INTO events (user_id, event_id, event_type, occurred_at, payload_hash) "
        "VALUES (?, ?, ?, ?, 'h')",
        [
            ("legacy", "t1", "touchpoint", stamp),
            ("legacy", "t2", "touchpoint", stamp),
            ("legacy", "c1", "conversion", stamp),
        ],
    )
    connection.commit()
    connection.close()

    database = Database(db_path)
    database.initialize()
    try:
        row = summary_rows(db_path)["legacy"]
        assert row["touchpoints"] == 2
        assert row["conversions"] == 1
        assert row["updated_at"] == stamp
        users = database.list_users()
        assert users == [
            {
                "user_id": "legacy",
                "attribution_window_seconds": 60,
                "default_touch_capacity": 1,
                "updated_at": stamp,
                "touchpoints": 2,
                "conversions": 1,
            }
        ]
        # Backfill is idempotent across restarts.
        database.initialize()
        assert summary_rows(db_path)["legacy"]["touchpoints"] == 2
    finally:
        database.close()


def test_summary_survives_process_restart_and_keeps_matching(tmp_path):
    db_path = str(tmp_path / "restart.db")
    first = Database(db_path)
    first.initialize()
    first.ingest(AttributeRequest(**payload(touchpoints=("t1", "t2"), conversions=("c1",))))
    first.close()

    restarted = Database(db_path)
    restarted.initialize()
    try:
        row = summary_rows(db_path)["u1"]
        assert row["touchpoints"] == 2
        assert row["conversions"] == 1
        assert_summary_matches_detail(db_path)

        restarted.ingest(
            AttributeRequest(**payload(touchpoints=("t3",), conversions=("c2", "c3")))
        )
        row = summary_rows(db_path)["u1"]
        assert row["touchpoints"] == 3
        assert row["conversions"] == 3
        assert_summary_matches_detail(db_path)
    finally:
        restarted.close()


def test_interleaved_concurrent_writes_keep_summary_consistent(tmp_path):
    db_path = str(tmp_path / "concurrent.db")
    database = Database(db_path)
    database.initialize()
    user_ids = [f"user-{i}" for i in range(6)]

    def worker(seq):
        local = Database(db_path)
        user_id = user_ids[seq % len(user_ids)]
        for round_index in range(5):
            suffix = f"{seq}-{round_index}"
            # Mix fresh inserts and duplicate replays.
            body = payload(
                user_id=user_id,
                touchpoints=(f"t-{suffix}",),
                conversions=(f"c-{suffix}",),
            )
            local.ingest(AttributeRequest(**body))
            local.ingest(AttributeRequest(**body))
        local.close()

    try:
        with ThreadPoolExecutor(max_workers=8) as pool:
            list(pool.map(worker, range(16)))
        assert_summary_matches_detail(db_path)

        # Every user must expose the same numbers through /api/users.
        app = create_app(db_path)
        with TestClient(app) as test_client:
            listed = {
                row["user_id"]: row
                for row in test_client.get("/api/users").json()["users"]
            }
        for user_id, bucket in detail_counts(db_path).items():
            assert listed[user_id]["touchpoints"] == bucket["touchpoints"]
            assert listed[user_id]["conversions"] == bucket["conversions"]
    finally:
        database.close()


def test_list_ordering_matches_updated_at_desc_then_user_id(client, tmp_path):
    test_client, db_path = client
    test_client.post("/attribute", json=payload(user_id="alpha"))
    time.sleep(0.01)
    test_client.post("/attribute", json=payload(user_id="beta"))
    time.sleep(0.01)
    test_client.post("/attribute", json=payload(user_id="alpha"))  # alpha now newest

    order = [row["user_id"] for row in test_client.get("/api/users").json()["users"]]
    assert order == ["alpha", "beta"]

    # Ties on updated_at break by ascending user_id, same as the old query.
    connection = sqlite3.connect(db_path)
    try:
        stamp = to_db_time(
            datetime(2026, 11, 2, tzinfo=timezone.utc)
        )
        connection.execute("UPDATE user_summaries SET updated_at = ?", (stamp,))
        connection.commit()
    finally:
        connection.close()
    tied = [row["user_id"] for row in test_client.get("/api/users").json()["users"]]
    assert tied == ["alpha", "beta"]


def test_rolled_back_transaction_never_changes_summary(tmp_path):
    db_path = str(tmp_path / "rollback.db")
    database = Database(db_path)
    database.initialize()
    database.ingest(AttributeRequest(**payload(touchpoints=("t1",), conversions=("c1",))))
    before = summary_rows(db_path)["u1"]

    # A failure inside the write transaction must roll event and summary
    # writes back together.
    with pytest.raises(RuntimeError):
        with database.transaction() as connection:
            connection.execute(
                "INSERT INTO events (user_id, event_id, event_type, occurred_at, payload_hash) "
                "VALUES ('u1', 'ghost', 'touchpoint', ?, 'h')",
                (before["updated_at"],),
            )
            connection.execute(
                "UPDATE user_summaries SET touchpoint_count = touchpoint_count + 1 "
                "WHERE user_id = 'u1'"
            )
            raise RuntimeError("boom")

    assert summary_rows(db_path)["u1"] == before
    assert_summary_matches_detail(db_path)
    database.close()


def test_startup_reconciliation_repairs_drifted_summary(tmp_path):
    db_path = str(tmp_path / "drift.db")
    database = Database(db_path)
    database.initialize()
    database.ingest(
        AttributeRequest(**payload(touchpoints=("t1", "t2"), conversions=("c1", "c2")))
    )
    database.close()

    # Tamper with the summary as an external writer might.
    connection = sqlite3.connect(db_path)
    connection.execute(
        "UPDATE user_summaries SET touchpoint_count = 99, conversion_count = 0"
    )
    connection.commit()
    connection.close()

    repaired = Database(db_path)
    repaired.initialize()
    repaired.close()
    row = summary_rows(db_path)["u1"]
    assert row["touchpoints"] == 2
    assert row["conversions"] == 2
    assert_summary_matches_detail(db_path)
