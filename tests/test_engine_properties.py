"""Detailed attribution engine tests covering capacity maximisation."""

from __future__ import annotations

import itertools
import random
from datetime import datetime, timedelta, timezone

from app.matching import Conversion, Touchpoint, run_attribution


T0 = datetime(2026, 1, 1, tzinfo=timezone.utc)


def touches(specs):
    return [
        Touchpoint(
            id=event_id,
            occurred_at=T0 + timedelta(minutes=minute),
            channel=channel,
            priority=priority,
            capacity=capacity,
        )
        for event_id, minute, channel, priority, capacity in specs
    ]


def conversions(specs):
    return [
        Conversion(id=event_id, occurred_at=T0 + timedelta(minutes=minute), value=value)
        for event_id, minute, value in specs
    ]


def brute_force_max(tps, cvs, window_seconds, default_capacity):
    """Exhaustive reference for tiny instances."""

    capacities = {
        tp.id: default_capacity if tp.capacity is None else tp.capacity for tp in tps
    }
    options = []
    for cv in cvs:
        eligible = [
            tp.id
            for tp in tps
            if tp.occurred_at < cv.occurred_at
            and cv.occurred_at - tp.occurred_at <= timedelta(seconds=window_seconds)
            and capacities[tp.id] > 0
        ]
        options.append([None] + eligible)

    best = 0
    for assignment in itertools.product(*options):
        used = {}
        for tp_id in assignment:
            if tp_id is not None:
                used[tp_id] = used.get(tp_id, 0) + 1
        if all(used[tp_id] <= capacities[tp_id] for tp_id in used):
            best = max(best, len([x for x in assignment if x is not None]))
    return best


def test_matches_brute_force_on_random_small_instances():
    rng = random.Random(20261121)
    for _ in range(80):
        tp_count = rng.randint(1, 4)
        cv_count = rng.randint(1, 4)
        window = rng.choice([30, 60, 120])
        default_capacity = rng.randint(0, 2)
        tps = touches(
            [
                (
                    f"t{i}",
                    rng.randint(0, 120),
                    rng.choice(["a", "b"]),
                    rng.randint(0, 3),
                    rng.choice([None, 0, 1, 2]),
                )
                for i in range(tp_count)
            ]
        )
        cvs = conversions(
            [(f"c{i}", rng.randint(0, 160), 1.0) for i in range(cv_count)]
        )
        result = run_attribution(tps, cvs, window, default_capacity)
        expected = brute_force_max(tps, cvs, window, default_capacity)
        assert result["totals"]["attributed_conversions"] == expected, (
            tps,
            cvs,
            window,
            default_capacity,
        )


def test_assignment_respects_every_constraint():
    rng = random.Random(7)
    tps = touches(
        [
            (f"t{i}", rng.randint(0, 200), "a", 0, rng.choice([None, 1, 2, 3]))
            for i in range(12)
        ]
    )
    cvs = conversions([(f"c{i}", rng.randint(0, 260), 5.0) for i in range(10)])
    result = run_attribution(tps, cvs, 120, 1)
    capacity = {tp.id: 1 if tp.capacity is None else tp.capacity for tp in tps}
    tp_by_id = {tp.id: tp for tp in tps}
    used = {}
    for row in result["conversions"]:
        if not row["attributed"]:
            assert row["reason"] in {"no_eligible_touchpoint", "capacity_exhausted"}
            continue
        tp = tp_by_id[row["touchpoint_id"]]
        cv = next(c for c in cvs if c.id == row["conversion_id"])
        assert tp.occurred_at < cv.occurred_at
        assert cv.occurred_at - tp.occurred_at <= timedelta(seconds=120)
        used[tp.id] = used.get(tp.id, 0) + 1
        assert used[tp.id] <= capacity[tp.id]
    assert result["totals"]["attributed_conversions"] == len(used)


def test_priority_is_maximised_among_maximum_cardinality_solutions():
    # Equal capacities and eligibility; higher-priority touchpoints should be
    # used before lower priority ones while keeping every conversion matched.
    result = run_attribution(
        touches(
            [
                ("low", 0, "a", 1, 1),
                ("mid", 1, "a", 2, 1),
                ("high", 2, "a", 3, 1),
            ]
        ),
        conversions([("c1", 10, 1), ("c2", 11, 1)]),
        600,
        1,
    )
    chosen = {row["touchpoint_id"] for row in result["conversions"]}
    assert chosen == {"high", "mid"}
