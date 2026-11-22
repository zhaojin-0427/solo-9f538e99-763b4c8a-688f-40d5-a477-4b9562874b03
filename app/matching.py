"""Deterministic, capacity-aware attribution matching.

The matching problem is a capacitated bipartite assignment:

* each conversion needs at most one touchpoint;
* a touchpoint can serve at most ``capacity`` conversions;
* a touchpoint is eligible only when it happened strictly before the
  conversion and no further back than the attribution window.

We first maximise the number of attributed conversions.  Among all
maximum-cardinality assignments we minimise a deterministic preference
cost.  Touchpoints are ranked by priority (descending), timestamp
(descending, i.e. last touch first) and finally event id (ascending), and
each assignment costs that rank.  Minimising the total rank therefore
prefers high-priority and recent touchpoints and is stable for ties.

This is solved exactly as a min-cost max-flow problem.  The implementation
uses successive shortest augmenting paths with Johnson potentials.
"""

from __future__ import annotations

import heapq
from bisect import bisect_left
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional, Sequence, Tuple

from .models import ApiError

# A defensive guard: a very dense window can otherwise create millions of
# edges and exhaust memory.  ~120k edges resolve in a few seconds; typical
# batches are far below this.
MAX_CANDIDATE_LINKS = 120_000


@dataclass(frozen=True)
class Touchpoint:
    id: str
    occurred_at: datetime
    channel: str
    priority: int
    capacity: Optional[int]

    def effective_capacity(self, default_capacity: int) -> int:
        return default_capacity if self.capacity is None else self.capacity


@dataclass(frozen=True)
class Conversion:
    id: str
    occurred_at: datetime
    value: float


@dataclass
class MatchOutcome:
    matched_touchpoint: Optional[str]
    reason: Optional[str]
    eligible_touchpoints: List[Touchpoint]


class _Edge:
    __slots__ = ("to", "rev", "cap", "cost")

    def __init__(self, to: int, rev: int, cap: int, cost: int) -> None:
        self.to = to
        self.rev = rev
        self.cap = cap
        self.cost = cost


class _MinCostMaxFlow:
    """Small, dependency-free min-cost max-flow solver."""

    def __init__(self, node_count: int) -> None:
        self.graph: List[List[_Edge]] = [[] for _ in range(node_count)]

    def add_edge(self, src: int, dst: int, cap: int, cost: int) -> None:
        forward = _Edge(dst, len(self.graph[dst]), cap, cost)
        backward = _Edge(src, len(self.graph[src]), 0, -cost)
        self.graph[src].append(forward)
        self.graph[dst].append(backward)

    def run(self, source: int, sink: int) -> Tuple[int, int]:
        """Return ``(max_flow, min_cost)``.

        All original edge costs must be non-negative, which holds here
        because they are ranks.  Potentials keep reduced costs non-negative
        after each augmentation, so Dijkstra remains valid.
        """

        node_count = len(self.graph)
        potential = [0] * node_count
        total_flow = 0
        total_cost = 0
        infinity = 1 << 120

        while True:
            dist = [infinity] * node_count
            prev_node = [-1] * node_count
            prev_edge = [-1] * node_count
            dist[source] = 0
            queue: List[Tuple[int, int]] = [(0, source)]

            while queue:
                current_dist, node = heapq.heappop(queue)
                if current_dist != dist[node]:
                    continue
                for edge_index, edge in enumerate(self.graph[node]):
                    if edge.cap <= 0:
                        continue
                    reduced = edge.cost + potential[node] - potential[edge.to]
                    candidate = current_dist + reduced
                    if candidate < dist[edge.to]:
                        dist[edge.to] = candidate
                        prev_node[edge.to] = node
                        prev_edge[edge.to] = edge_index
                        heapq.heappush(queue, (candidate, edge.to))

            if dist[sink] == infinity:
                break

            for node in range(node_count):
                if dist[node] < infinity:
                    potential[node] += dist[node]

            # All source -> conversion edges have capacity one, so every
            # augmentation carries exactly one unit.  Keep it general anyway.
            augment = infinity
            node = sink
            while node != source:
                parent = prev_node[node]
                if parent < 0:
                    raise RuntimeError("broken augmenting path")
                edge = self.graph[parent][prev_edge[node]]
                augment = min(augment, edge.cap)
                node = parent

            node = sink
            path_cost = 0
            while node != source:
                parent = prev_node[node]
                edge = self.graph[parent][prev_edge[node]]
                path_cost += edge.cost
                edge.cap -= augment
                self.graph[node][edge.rev].cap += augment
                node = parent

            total_flow += augment
            total_cost += augment * path_cost

        return total_flow, total_cost


def _iso(value: datetime) -> str:
    return value.isoformat().replace("+00:00", "Z")


def _gap_seconds(later: datetime, earlier: datetime) -> float:
    return round((later - earlier).total_seconds(), 6)


def _rank_touchpoints(touchpoints: Sequence[Touchpoint]) -> Dict[str, int]:
    # Stable multi-pass sort produces priority DESC, time DESC, id ASC
    # without relying on floating point timestamps.
    ordered = sorted(touchpoints, key=lambda tp: tp.id)
    ordered.sort(key=lambda tp: tp.occurred_at, reverse=True)
    ordered.sort(key=lambda tp: tp.priority, reverse=True)
    return {tp.id: rank for rank, tp in enumerate(ordered)}


def run_attribution(
    touchpoints: Sequence[Touchpoint],
    conversions: Sequence[Conversion],
    window_seconds: int,
    default_capacity: int,
) -> dict:
    """Compute attribution for one user's complete event set."""

    window = timedelta(seconds=window_seconds)
    by_time = sorted(touchpoints, key=lambda tp: (tp.occurred_at, tp.id))
    touch_times = [tp.occurred_at for tp in by_time]
    capacity_by_id = {
        tp.id: tp.effective_capacity(default_capacity) for tp in touchpoints
    }
    rank_by_id = _rank_touchpoints(touchpoints)

    # Pre-compute eligible touchpoints (inside the time window, strictly
    # before the conversion).  The shortlist is used both for graph
    # construction and for explaining unattributed conversions.
    eligible: List[List[Touchpoint]] = []
    in_window: List[List[Touchpoint]] = []
    candidate_links = 0
    for conversion in conversions:
        try:
            lower = conversion.occurred_at - window
        except OverflowError:
            # Very old timestamps plus a long window underflow datetime's
            # supported range; everything from the beginning counts.
            lower = datetime.min.replace(tzinfo=timezone.utc)
        left = bisect_left(touch_times, lower)
        # Strictly before the conversion: a simultaneous touch is not "prior".
        right = bisect_left(touch_times, conversion.occurred_at)
        shortlist = by_time[left:right]
        in_window.append(shortlist)
        usable = [tp for tp in shortlist if capacity_by_id[tp.id] > 0]
        # Stable preference order also makes tie handling deterministic.
        usable.sort(key=lambda tp: rank_by_id[tp.id])
        eligible.append(usable)
        candidate_links += len(usable)
        if candidate_links > MAX_CANDIDATE_LINKS:
            raise ApiError(
                422,
                "candidate_space_too_large",
                (
                    "候选触点组合过多，请缩小归因窗口、减少事件或降低触点容量。"
                    f" 当前候选连接数已超过上限 {MAX_CANDIDATE_LINKS}。"
                ),
            )

    # Node layout: source, conversions, touchpoints, sink.
    conversion_count = len(conversions)
    touchpoint_count = len(touchpoints)
    source = 0
    conversion_offset = 1
    touchpoint_offset = conversion_offset + conversion_count
    sink = touchpoint_offset + touchpoint_count
    flow = _MinCostMaxFlow(sink + 1)

    # Deterministic iteration order for reproducible augmenting paths.
    conversion_order = sorted(
        range(conversion_count),
        key=lambda index: (
            conversions[index].occurred_at,
            conversions[index].id,
        ),
    )
    touchpoint_index = {
        tp.id: touchpoint_offset + index
        for index, tp in enumerate(touchpoints)
    }

    for index in conversion_order:
        node = conversion_offset + index
        flow.add_edge(source, node, 1, 0)
        for tp in eligible[index]:
            # rank is the combined priority/time/id preference.
            flow.add_edge(node, touchpoint_index[tp.id], 1, rank_by_id[tp.id])

    for tp in touchpoints:
        capacity = capacity_by_id[tp.id]
        if capacity > 0:
            flow.add_edge(
                touchpoint_index[tp.id], sink, min(capacity, conversion_count), 0
            )

    attributed_count, _ = flow.run(source, sink)

    # Read the assignment from the forward conversion -> touchpoint edges.
    matched: List[Optional[str]] = [None] * conversion_count
    used_by_touchpoint: Dict[str, int] = defaultdict(int)

    for index in range(conversion_count):
        node = conversion_offset + index
        for edge in flow.graph[node]:
            target = edge.to
            if not (touchpoint_offset <= target < sink):
                continue
            if edge.cap == 0:  # used by the final flow
                tp = touchpoints[target - touchpoint_offset]
                matched[index] = tp.id
                used_by_touchpoint[tp.id] += 1
                break

    outcomes: List[MatchOutcome] = []
    for index in range(conversion_count):
        tp_id = matched[index]
        # Explain with every touchpoint that was in the window, including
        # zero-capacity ones, while the graph only used usable touchpoints.
        display = sorted(in_window[index], key=lambda tp: rank_by_id[tp.id])
        if tp_id is not None:
            outcomes.append(MatchOutcome(tp_id, None, display))
        elif not in_window[index]:
            outcomes.append(MatchOutcome(None, "no_eligible_touchpoint", display))
        else:
            # There was at least one touchpoint inside the window and before
            # the conversion, so the only remaining explanation is that all
            # eligible capacity went to other conversions (or capacity is 0).
            outcomes.append(MatchOutcome(None, "capacity_exhausted", display))

    # A maximum flow leaves no unmatched conversion with spare eligible
    # capacity.  This assertion protects that invariant during development.
    assert sum(1 for item in matched if item is not None) == attributed_count

    return _build_result(
        touchpoints=touchpoints,
        conversions=conversions,
        outcomes=outcomes,
        matched=matched,
        capacity_by_id=capacity_by_id,
        used_by_touchpoint=used_by_touchpoint,
        window_seconds=window_seconds,
        default_capacity=default_capacity,
    )


def _build_result(
    *,
    touchpoints: Sequence[Touchpoint],
    conversions: Sequence[Conversion],
    outcomes: Sequence[MatchOutcome],
    matched: Sequence[Optional[str]],
    capacity_by_id: Dict[str, int],
    used_by_touchpoint: Dict[str, int],
    window_seconds: int,
    default_capacity: int,
) -> dict:
    tp_by_id = {tp.id: tp for tp in touchpoints}
    channel_stats: Dict[str, dict] = {}

    for tp in touchpoints:
        stats = channel_stats.setdefault(
            tp.channel,
            {
                "channel": tp.channel,
                "touchpoints": 0,
                "capacity": 0,
                "attributed_conversions": 0,
                "attributed_value": 0.0,
            },
        )
        stats["touchpoints"] += 1
        stats["capacity"] += capacity_by_id[tp.id]

    conversion_rows = []
    attributed_value = 0.0
    reason_counts: Dict[str, int] = {
        "no_eligible_touchpoint": 0,
        "capacity_exhausted": 0,
    }

    for index, conversion in enumerate(conversions):
        outcome = outcomes[index]
        tp_id = matched[index]
        row = {
            "conversion_id": conversion.id,
            "occurred_at": _iso(conversion.occurred_at),
            "value": conversion.value,
            "attributed": tp_id is not None,
            "touchpoint_id": tp_id,
            "channel": None,
            "priority": None,
            "touchpoint_occurred_at": None,
            "time_gap_seconds": None,
            "reason": outcome.reason,
            "eligible_count": len(outcome.eligible_touchpoints),
            "eligible_touchpoints": [],
        }
        if outcome.reason is not None:
            reason_counts[outcome.reason] = reason_counts.get(outcome.reason, 0) + 1

        if tp_id is not None:
            tp = tp_by_id[tp_id]
            row.update(
                {
                    "channel": tp.channel,
                    "priority": tp.priority,
                    "touchpoint_occurred_at": _iso(tp.occurred_at),
                    "time_gap_seconds": _gap_seconds(
                        conversion.occurred_at, tp.occurred_at
                    ),
                }
            )
            stats = channel_stats[tp.channel]
            stats["attributed_conversions"] += 1
            stats["attributed_value"] += conversion.value
            attributed_value += conversion.value

        for candidate in outcome.eligible_touchpoints:
            row["eligible_touchpoints"].append(
                {
                    "touchpoint_id": candidate.id,
                    "channel": candidate.channel,
                    "priority": candidate.priority,
                    "occurred_at": _iso(candidate.occurred_at),
                    "capacity": capacity_by_id[candidate.id],
                    "selected": candidate.id == tp_id,
                }
            )
        conversion_rows.append(row)

    touchpoint_rows = []
    for tp in sorted(touchpoints, key=lambda item: (item.occurred_at, item.id)):
        capacity = capacity_by_id[tp.id]
        used = used_by_touchpoint.get(tp.id, 0)
        touchpoint_rows.append(
            {
                "touchpoint_id": tp.id,
                "channel": tp.channel,
                "occurred_at": _iso(tp.occurred_at),
                "priority": tp.priority,
                "capacity": capacity,
                "used": used,
                "remaining": max(0, capacity - used),
            }
        )

    channels = sorted(channel_stats.values(), key=lambda item: item["channel"])
    for channel in channels:
        channel["attributed_value"] = round(channel["attributed_value"], 6)

    conversion_rows.sort(
        key=lambda row: (row["occurred_at"], row["conversion_id"])
    )

    total_capacity = sum(capacity_by_id.values())
    return {
        "settings": {
            "attribution_window_seconds": window_seconds,
            "default_touch_capacity": default_capacity,
        },
        "totals": {
            "touchpoints": len(touchpoints),
            "conversions": len(conversions),
            "attributed_conversions": len(
                [item for item in matched if item is not None]
            ),
            "unattributed_conversions": len(
                [item for item in matched if item is None]
            ),
            "attributed_value": round(attributed_value, 6),
            "touchpoint_capacity": total_capacity,
            "capacity_used": sum(used_by_touchpoint.values()),
        },
        "unattributed_reasons": reason_counts,
        "conversions": conversion_rows,
        "touchpoints": touchpoint_rows,
        "channels": channels,
    }


__all__ = [
    "Conversion",
    "MatchOutcome",
    "Touchpoint",
    "run_attribution",
]
