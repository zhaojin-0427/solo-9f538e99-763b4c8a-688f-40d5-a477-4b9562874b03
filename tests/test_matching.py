"""Tests for the attribution engine."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app.matching import Conversion, Touchpoint, run_attribution

T0 = datetime(2026, 11, 1, tzinfo=timezone.utc)


def touchpoint(
    event_id: str,
    minutes: float,
    channel: str = "seo",
    priority: int = 0,
    capacity: int | None = None,
) -> Touchpoint:
    return Touchpoint(
        id=event_id,
        occurred_at=T0 + timedelta(minutes=minutes),
        channel=channel,
        priority=priority,
        capacity=capacity,
    )


def conversion(event_id: str, minutes: float, value: float = 10.0) -> Conversion:
    return Conversion(
        id=event_id, occurred_at=T0 + timedelta(minutes=minutes), value=value
    )


def by_id(result):
    return {row["conversion_id"]: row for row in result["conversions"]}


def test_touchpoint_must_precede_conversion_and_be_in_window():
    result = run_attribution(
        [
            touchpoint("before_window", -100),
            touchpoint("in_window", 5),
            touchpoint("simultaneous", 10),
            touchpoint("after", 20),
        ],
        [conversion("c1", 10)],
        window_seconds=3600,
        default_capacity=1,
    )
    row = by_id(result)["c1"]
    assert row["touchpoint_id"] == "in_window"
    assert [tp["touchpoint_id"] for tp in row["eligible_touchpoints"]] == ["in_window"]


def test_capacity_constrains_and_explains_exhaustion():
    result = run_attribution(
        [touchpoint("t1", 0, capacity=1)],
        [conversion("c1", 10), conversion("c2", 20)],
        window_seconds=3600,
        default_capacity=1,
    )
    rows = by_id(result)
    assert rows["c1"]["attributed"] is True
    assert rows["c2"]["attributed"] is False
    assert rows["c2"]["reason"] == "capacity_exhausted"
    assert result["unattributed_reasons"]["capacity_exhausted"] == 1


def test_no_eligible_touchpoint_reason():
    result = run_attribution(
        [touchpoint("t1", 200)],
        [conversion("c1", 10)],
        window_seconds=3600,
        default_capacity=1,
    )
    row = by_id(result)["c1"]
    assert row["reason"] == "no_eligible_touchpoint"
    assert row["eligible_count"] == 0


def test_maximises_attributed_conversions_with_reassignment():
    # c1 greedily prefers t2, but only t2 can serve c2.  A maximum-cardinality
    # solution must move t1 to c1.
    result = run_attribution(
        [touchpoint("t1", 0), touchpoint("t2", 15)],
        [conversion("c1", 20), conversion("c2", 60)],
        window_seconds=3600,
        default_capacity=1,
    )
    rows = by_id(result)
    assert rows["c1"]["attributed"] and rows["c2"]["attributed"]
    assert {rows["c1"]["touchpoint_id"], rows["c2"]["touchpoint_id"]} == {"t1", "t2"}


def test_priority_then_recency_then_id_is_deterministic():
    result = run_attribution(
        [
            touchpoint("b", 0),
            touchpoint("a", 0),
            touchpoint("newer_low", 4, priority=1),
            touchpoint("older_high", 1, priority=9),
        ],
        [conversion("c1", 5)],
        window_seconds=3600,
        default_capacity=1,
    )
    # Highest priority wins even if older.
    assert by_id(result)["c1"]["touchpoint_id"] == "older_high"

    tie = run_attribution(
        [touchpoint("b", 0), touchpoint("a", 0)],
        [conversion("c1", 5)],
        window_seconds=3600,
        default_capacity=1,
    )
    # Identical priority/time falls back to ascending id.
    assert by_id(tie)["c1"]["touchpoint_id"] == "a"

    recency = run_attribution(
        [touchpoint("old", 0), touchpoint("new", 4)],
        [conversion("c1", 5)],
        window_seconds=3600,
        default_capacity=1,
    )
    assert by_id(recency)["c1"]["touchpoint_id"] == "new"


def test_default_capacity_and_explicit_zero_capacity():
    result = run_attribution(
        [touchpoint("default", 0), touchpoint("zero", 1, capacity=0)],
        [conversion("c1", 5), conversion("c2", 6)],
        window_seconds=3600,
        default_capacity=1,
    )
    rows = by_id(result)
    assert rows["c1"]["touchpoint_id"] == "default"
    assert rows["c2"]["attributed"] is False
    assert rows["c2"]["reason"] == "capacity_exhausted"
    zero = next(
        tp for tp in result["touchpoints"] if tp["touchpoint_id"] == "zero"
    )
    assert zero["capacity"] == 0
    assert zero["used"] == 0


def test_capacity_used_never_exceeds_capacity():
    result = run_attribution(
        [touchpoint("t1", 0, capacity=2)],
        [conversion(f"c{i}", 10 + i) for i in range(5)],
        window_seconds=3600,
        default_capacity=1,
    )
    tp = result["touchpoints"][0]
    assert tp["used"] == 2
    assert tp["remaining"] == 0
    assert result["totals"]["attributed_conversions"] == 2


def test_channel_summary_and_totals():
    result = run_attribution(
        [
            touchpoint("seo1", 0, channel="seo", capacity=2),
            touchpoint("ads1", 100, channel="ads", capacity=1),
        ],
        [
            conversion("c1", 30, 100),
            conversion("c2", 40, 50),
            conversion("c3", 130, 25),
        ],
        window_seconds=3600,
        default_capacity=1,
    )
    channels = {row["channel"]: row for row in result["channels"]}
    assert channels["seo"]["attributed_conversions"] == 2
    assert channels["seo"]["attributed_value"] == 150.0
    assert channels["ads"]["attributed_conversions"] == 1
    assert result["totals"]["attributed_value"] == 175.0
    assert result["totals"]["capacity_used"] == 3


def test_windowing_is_inclusive_of_exact_lower_bound():
    result = run_attribution(
        [touchpoint("edge", 0)],
        [conversion("c1", 10)],
        window_seconds=600,
        default_capacity=1,
    )
    assert by_id(result)["c1"]["attributed"] is True
    below = run_attribution(
        [touchpoint("edge", 0)],
        [conversion("c1", 10)],
        window_seconds=599,
        default_capacity=1,
    )
    assert by_id(below)["c1"]["attributed"] is False


def test_empty_inputs():
    result = run_attribution([], [], window_seconds=60, default_capacity=1)
    assert result["totals"]["conversions"] == 0
    assert result["channels"] == []
