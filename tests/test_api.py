"""API-level tests: validation, idempotency, atomicity and reads."""

from __future__ import annotations

import copy

import pytest
from fastapi.testclient import TestClient

from app.main import create_app


@pytest.fixture()
def client(tmp_path):
    app = create_app(str(tmp_path / "test.db"))
    with TestClient(app) as test_client:
        yield test_client


def base_payload():
    return {
        "user_id": "u1",
        "attribution_window_seconds": 86400,
        "default_touch_capacity": 1,
        "touchpoints": [
            {
                "id": "t1",
                "occurred_at": "2026-11-01T00:00:00Z",
                "channel": "SEO",
                "priority": 1,
            },
            {
                "id": "t2",
                "occurred_at": "2026-11-01T06:00:00Z",
                "channel": "Ads",
                "priority": 3,
                "capacity": 2,
            },
        ],
        "conversions": [
            {"id": "c1", "occurred_at": "2026-11-01T12:00:00Z", "value": 10},
            {"id": "c2", "occurred_at": "2026-11-01T13:00:00Z", "value": 20},
            {"id": "c3", "occurred_at": "2026-11-01T14:00:00Z", "value": 30},
        ],
    }


def test_attribute_endpoint_returns_full_result(client):
    response = client.post("/attribute", json=base_payload())
    assert response.status_code == 200
    data = response.json()
    assert data["user_id"] == "u1"
    assert data["received"] == {
        "touchpoints": 2,
        "conversions": 3,
        "inserted": 5,
        "duplicates_ignored": 0,
    }
    assert data["totals"]["attributed_conversions"] == 3
    assert data["totals"]["attributed_value"] == 60.0
    assert {row["channel"] for row in data["channels"]} == {"SEO", "Ads"}
    for row in data["conversions"]:
        assert row["attributed"] is True
        assert row["eligible_touchpoints"]


def test_duplicate_batch_is_idempotent(client):
    first = client.post("/attribute", json=base_payload()).json()
    second = client.post("/attribute", json=base_payload()).json()
    assert second["received"]["inserted"] == 0
    assert second["received"]["duplicates_ignored"] == 5
    assert first["conversions"] == second["conversions"]
    assert len(client.get("/api/users/u1/attribution").json()["conversions"]) == 3


def test_reused_id_with_different_content_rejects_whole_batch(client):
    client.post("/attribute", json=base_payload())
    changed = base_payload()
    changed["touchpoints"][0]["channel"] = "Changed"
    changed["conversions"].append(
        {"id": "new", "occurred_at": "2026-11-01T15:00:00Z", "value": 1}
    )
    response = client.post("/attribute", json=changed)
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "event_id_conflict"
    stored = client.get("/api/users/u1/attribution").json()
    # Neither the changed payload nor the new conversion leaked in.
    assert len(stored["conversions"]) == 3
    assert [row["conversion_id"] for row in stored["conversions"]] == [
        "c1",
        "c2",
        "c3",
    ]


def test_in_batch_duplicate_id_with_conflicting_content_is_rejected(client):
    payload = base_payload()
    payload["conversions"].append(
        {"id": "c1", "occurred_at": "2026-11-01T20:00:00Z", "value": 99}
    )
    response = client.post("/attribute", json=payload)
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "duplicate_event_id"
    assert client.get("/api/users/u1/attribution").status_code == 404


@pytest.mark.parametrize(
    "mutation",
    [
        lambda p: p.update(attribution_window_seconds=0),
        lambda p: p.update(attribution_window_seconds=-5),
        lambda p: p.update(attribution_window_seconds="abc"),
        lambda p: p.update(user_id=""),
        lambda p: p["touchpoints"][0].update(occurred_at="not-a-time"),
        lambda p: p["touchpoints"][0].update(channel=""),
        lambda p: p["touchpoints"][0].update(priority=1.5),
        lambda p: p["touchpoints"][0].update(capacity=-1),
        lambda p: p["touchpoints"][0].update(surprise="field"),
        lambda p: p["conversions"][0].update(value=-1),
        lambda p: p["conversions"][0].update(value=True),
        lambda p: p["conversions"][0].update(value="ten"),
        lambda p: p["conversions"][0].update(id=""),
        lambda p: p["conversions"][0].update(occurred_at="2026-13-45T99:00:00Z"),
    ],
)
def test_invalid_fields_reject_whole_batch(client, mutation):
    payload = base_payload()
    mutation(payload)
    payload["conversions"].append(
        {"id": "valid_but_should_not_persist", "occurred_at": "2026-11-02T00:00:00Z"}
    )
    response = client.post("/attribute", json=payload)
    assert response.status_code == 422
    body = response.json()["error"]
    assert body["code"] == "invalid_payload"
    assert body["details"]
    users = client.get("/api/users").json()
    assert users["count"] == 0


def test_missing_required_fields_rejected(client):
    for payload in (
        {},
        {"user_id": "u1"},
        {"user_id": "u1", "attribution_window_seconds": 10},
        {
            "user_id": "u1",
            "attribution_window_seconds": 10,
            "touchpoints": [],
            "conversions": [],
        },
    ):
        assert client.post("/attribute", json=payload).status_code == 422


def test_naive_timestamps_are_treated_as_utc(client):
    payload = base_payload()
    for touchpoint in payload["touchpoints"]:
        touchpoint["occurred_at"] = touchpoint["occurred_at"].replace("Z", "")
    response = client.post("/attribute", json=payload)
    assert response.status_code == 200
    assert response.json()["totals"]["attributed_conversions"] == 3


def test_users_and_events_endpoints(client):
    client.post("/attribute", json=base_payload())
    users = client.get("/api/users").json()
    assert users["count"] == 1
    assert users["users"][0]["user_id"] == "u1"

    events = client.get("/api/users/u1/events").json()
    assert [event["event_id"] for event in events["events"]] == [
        "t1",
        "t2",
        "c1",
        "c2",
        "c3",
    ]
    assert events["settings"]["attribution_window_seconds"] == 86400


def test_unknown_user_returns_404(client):
    response = client.get("/api/users/nobody/attribution")
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "user_not_found"


def test_window_setting_updates_on_new_batch(client):
    client.post("/attribute", json=base_payload())
    changed = copy.deepcopy(base_payload())
    changed["attribution_window_seconds"] = 60
    response = client.post("/attribute", json=changed)
    assert response.json()["settings"]["attribution_window_seconds"] == 60


def test_health_and_index(client):
    assert client.get("/health").json() == {"status": "ok"}
    page = client.get("/")
    assert page.status_code == 200
    assert "运营归因分析台" in page.text


def test_invalid_json_is_rejected(client):
    response = client.post(
        "/attribute",
        content="{not json",
        headers={"Content-Type": "application/json"},
    )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "invalid_payload"


def test_empty_event_batch_is_rejected(client):
    payload = {
        "user_id": "u1",
        "attribution_window_seconds": 60,
        "touchpoints": [],
        "conversions": [],
    }
    response = client.post("/attribute", json=payload)
    assert response.status_code == 422
    assert client.get("/api/users").json()["count"] == 0


def test_candidate_space_guard_rejects_dense_batch(client):
    touchpoints = [
        {
            "id": f"t{i}",
            "occurred_at": f"2026-11-01T{i // 3600:02d}:{(i // 60) % 60:02d}:{i % 60:02d}Z",
            "channel": "x",
        }
        for i in range(500)
    ]
    conversions = [
        {
            "id": f"c{i}",
            "occurred_at": "2026-11-01T23:59:00Z",
            "value": 1,
        }
        for i in range(400)
    ]
    payload = {
        "user_id": "dense",
        "attribution_window_seconds": 86400,
        "default_touch_capacity": 1,
        "touchpoints": touchpoints,
        "conversions": conversions,
    }
    response = client.post("/attribute", json=payload)
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "candidate_space_too_large"
    assert client.get("/api/users").json()["count"] == 0


def test_oversized_body_is_rejected(client, monkeypatch):
    from app import main as main_module

    monkeypatch.setattr(main_module, "MAX_BODY_BYTES", 100)
    response = client.post("/attribute", json=base_payload())
    assert response.status_code == 413
    assert response.json()["error"]["code"] == "payload_too_large"


def test_oversized_chunked_body_is_rejected(tmp_path, monkeypatch):
    """A body without Content-Length must still be counted while streaming."""

    from fastapi.testclient import TestClient

    from app import main as main_module

    monkeypatch.setattr(main_module, "MAX_BODY_BYTES", 1_000)
    client = TestClient(main_module.create_app(str(tmp_path / "chunked.db")))
    large = "x" * 5_000

    def stream():
        for offset in range(0, len(large), 500):
            yield large[offset : offset + 500].encode()

    response = client.post(
        "/attribute", content=stream(), headers={"Content-Type": "application/json"}
    )
    assert response.status_code == 413
    assert response.json()["error"]["code"] == "payload_too_large"


def test_concurrent_idempotent_ingestion(tmp_path):
    from concurrent.futures import ThreadPoolExecutor

    from app.db import Database
    from app.models import AttributeRequest

    database = Database(str(tmp_path / "concurrent.db"))
    database.initialize()
    request = AttributeRequest(**base_payload())

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda _: database.ingest(request), range(16)))

    assert sum(result["received"]["inserted"] for result in results) == 5
    stored = database.get_user_events("u1")["events"]
    assert len(stored) == 5
    database.close()
