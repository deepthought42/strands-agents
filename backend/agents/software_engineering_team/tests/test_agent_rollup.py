"""Unit tests for the agent/phase rollup (metrics.agent_rollup).

Covers both the dataclass shape (construction defaults, (de)serialization mechanics)
and the pure grouping/percentile computation in ``compute_from_traces``.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest

from software_engineering_team.metrics.agent_rollup import (
    AgentRollupMetrics,
    CallRollup,
    compute_agent_rollup,
    compute_from_traces,
)
from software_engineering_team.shared import trace_store


def test_call_rollup_defaults() -> None:
    """A default CallRollup represents an empty group: zeros and None derived stats."""
    r = CallRollup()
    assert r.call_count == 0
    assert r.total_cost_usd == 0.0
    assert r.total_input_tokens == 0
    assert r.total_output_tokens == 0
    assert r.total_cache_read_tokens == 0
    assert r.total_cache_creation_tokens == 0
    assert r.cache_read_ratio is None
    assert r.latency_ms_median is None
    assert r.latency_ms_p95 is None
    assert r.latency_ms_sample_count == 0


def test_agent_rollup_metrics_defaults() -> None:
    """A default AgentRollupMetrics carries no groups in any of the three views."""
    m = AgentRollupMetrics(window_days=7.0, computed_at="2026-09-02T00:00:00+00:00")
    assert m.window_days == 7.0
    assert m.computed_at == "2026-09-02T00:00:00+00:00"
    assert m.by_agent == {}
    assert m.by_phase == {}
    assert m.by_agent_phase == {}


def test_agent_rollup_metrics_mutable_defaults_are_isolated() -> None:
    """Two independent instances don't share the same default dict (the field(default_factory) footgun)."""
    a = AgentRollupMetrics(window_days=1.0, computed_at="t")
    b = AgentRollupMetrics(window_days=1.0, computed_at="t")

    a.by_agent["backend"] = CallRollup(call_count=1)
    a.by_phase["execution"] = CallRollup(call_count=1)
    a.by_agent_phase["backend"] = {"execution": CallRollup(call_count=1)}

    assert b.by_agent == {}
    assert b.by_phase == {}
    assert b.by_agent_phase == {}


def test_to_dict_round_trip_nests_plain_dicts() -> None:
    """to_dict() recurses through every level, including the by_agent_phase nesting."""
    m = AgentRollupMetrics(window_days=30.0, computed_at="2026-09-02T00:00:00+00:00")
    m.by_agent["backend"] = CallRollup(
        call_count=3,
        total_cost_usd=1.23,
        total_input_tokens=100,
        total_output_tokens=50,
        total_cache_read_tokens=40,
        total_cache_creation_tokens=10,
        cache_read_ratio=0.4,
        latency_ms_median=250.0,
        latency_ms_p95=900.0,
        latency_ms_sample_count=3,
    )
    m.by_phase["execution"] = CallRollup(call_count=3, total_cost_usd=1.23)
    m.by_agent_phase["backend"] = {"execution": CallRollup(call_count=3, total_cost_usd=1.23)}

    d = m.to_dict()

    # The documented invariant: the whole shape serializes to JSON end to end.
    assert json.loads(json.dumps(d)) == d

    assert d["window_days"] == 30.0
    assert d["computed_at"] == "2026-09-02T00:00:00+00:00"

    # Every nested value is a plain dict, not a CallRollup instance.
    assert isinstance(d["by_agent"]["backend"], dict)
    assert isinstance(d["by_phase"]["execution"], dict)
    assert isinstance(d["by_agent_phase"]["backend"], dict)
    assert isinstance(d["by_agent_phase"]["backend"]["execution"], dict)

    assert d["by_agent"]["backend"]["call_count"] == 3
    assert d["by_agent"]["backend"]["cache_read_ratio"] == 0.4
    assert d["by_agent"]["backend"]["latency_ms_median"] == 250.0
    assert d["by_agent"]["backend"]["latency_ms_p95"] == 900.0
    assert d["by_phase"]["execution"]["total_cost_usd"] == 1.23
    assert d["by_agent_phase"]["backend"]["execution"]["call_count"] == 3

    # A group with no samples keeps its None sentinels through serialization.
    assert d["by_phase"]["execution"]["cache_read_ratio"] is None
    assert d["by_phase"]["execution"]["latency_ms_median"] is None


def _row(
    *,
    agent_key: str = "agent",
    phase: str = "phase",
    cost_usd: float = 0.0,
    input_tokens: int = 0,
    output_tokens: int = 0,
    cache_read_tokens: int = 0,
    cache_creation_tokens: int = 0,
    latency_ms: float = 0.0,
) -> dict[str, Any]:
    """Build one se_agent_traces-shaped row; keyword-only defaults let each test override only the fields under test."""
    return {
        "agent_key": agent_key,
        "phase": phase,
        "cost_usd": cost_usd,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "cache_read_tokens": cache_read_tokens,
        "cache_creation_tokens": cache_creation_tokens,
        "latency_ms": latency_ms,
    }


def test_window_days_must_be_positive() -> None:
    """compute_from_traces rejects a non-positive window_days."""
    with pytest.raises(ValueError, match="window_days"):
        compute_from_traces([], window_days=0)
    with pytest.raises(ValueError, match="window_days"):
        compute_from_traces([], window_days=-1.0)


def test_empty_rows_yields_empty_groups() -> None:
    """An empty row list is not an error: every grouping view is empty."""
    m = compute_from_traces([], window_days=7.0)
    assert m.window_days == 7.0
    assert m.by_agent == {}
    assert m.by_phase == {}
    assert m.by_agent_phase == {}


def test_single_group_aggregates_sums_and_percentiles() -> None:
    """Every row in one agent/phase group contributes to its sums, ratio, and percentiles."""
    rows = [
        _row(
            agent_key="backend",
            phase="execution",
            cost_usd=1.0,
            input_tokens=100,
            output_tokens=50,
            cache_read_tokens=40,
            cache_creation_tokens=10,
            latency_ms=200,
        ),
        _row(
            agent_key="backend",
            phase="execution",
            cost_usd=2.0,
            input_tokens=200,
            output_tokens=60,
            cache_read_tokens=80,
            cache_creation_tokens=20,
            latency_ms=300,
        ),
        _row(
            agent_key="backend",
            phase="execution",
            cost_usd=0.5,
            input_tokens=50,
            output_tokens=10,
            cache_read_tokens=0,
            cache_creation_tokens=0,
            latency_ms=100,
        ),
    ]

    m = compute_from_traces(rows, window_days=7.0)

    r = m.by_agent_phase["backend"]["execution"]
    assert r.call_count == 3
    assert r.total_cost_usd == pytest.approx(3.5)
    assert r.total_input_tokens == 350
    assert r.total_output_tokens == 120
    assert r.total_cache_read_tokens == 120
    assert r.total_cache_creation_tokens == 30
    assert r.cache_read_ratio == pytest.approx(0.24)  # 120 / (120 + 30 + 350)
    assert r.latency_ms_median == pytest.approx(200.0)
    assert r.latency_ms_p95 == pytest.approx(300.0)  # n=3, rank=ceil(2.85)=3
    assert r.latency_ms_sample_count == 3

    # by_agent and by_phase collapse to the same single group here.
    assert m.by_agent["backend"].call_count == 3
    assert m.by_phase["execution"].call_count == 3


def test_multi_group_partitions_by_agent_phase_and_pair() -> None:
    """Rows spanning agents and phases partition correctly into all three views, sorted."""
    rows = [
        _row(agent_key="backend", phase="execution", cost_usd=1.0, latency_ms=50),
        _row(agent_key="backend", phase="design", cost_usd=2.0, latency_ms=150),
        _row(agent_key="frontend", phase="execution", cost_usd=3.0, latency_ms=250),
    ]

    m = compute_from_traces(rows, window_days=1.0)

    assert list(m.by_agent.keys()) == ["backend", "frontend"]
    assert list(m.by_phase.keys()) == ["design", "execution"]
    assert list(m.by_agent_phase.keys()) == ["backend", "frontend"]
    assert list(m.by_agent_phase["backend"].keys()) == ["design", "execution"]

    assert m.by_agent["backend"].call_count == 2
    assert m.by_agent["frontend"].call_count == 1
    assert m.by_phase["execution"].call_count == 2
    assert m.by_phase["design"].call_count == 1
    assert m.by_agent_phase["backend"]["execution"].call_count == 1
    assert m.by_agent_phase["backend"]["design"].call_count == 1
    assert m.by_agent_phase["frontend"]["execution"].call_count == 1


def test_deterministic_regardless_of_input_row_order() -> None:
    """Reordering the same logical rows produces an identical result, key order included."""
    rows = [
        _row(agent_key="backend", phase="execution", cost_usd=1.0, latency_ms=50),
        _row(agent_key="backend", phase="design", cost_usd=2.0, latency_ms=150),
        _row(agent_key="frontend", phase="execution", cost_usd=3.0, latency_ms=250),
    ]

    forward = compute_from_traces(rows, window_days=1.0)
    backward = compute_from_traces(list(reversed(rows)), window_days=1.0)

    assert list(forward.by_agent.keys()) == list(backward.by_agent.keys())
    assert list(forward.by_phase.keys()) == list(backward.by_phase.keys())
    assert list(forward.by_agent_phase.keys()) == list(backward.by_agent_phase.keys())
    forward_dict = forward.to_dict()
    backward_dict = backward.to_dict()
    del forward_dict["computed_at"]
    del backward_dict["computed_at"]
    assert forward_dict == backward_dict


def test_single_row_group_median_equals_p95() -> None:
    """A group with exactly one call has its sole latency as both median and p95."""
    rows = [_row(latency_ms=123.0)]

    m = compute_from_traces(rows, window_days=1.0)

    r = m.by_agent["agent"]
    assert r.call_count == 1
    assert r.latency_ms_sample_count == 1
    assert r.latency_ms_median == pytest.approx(123.0)
    assert r.latency_ms_p95 == pytest.approx(123.0)


def test_p95_of_two_samples_is_the_larger() -> None:
    """Nearest-rank p95 of two samples returns the worse (larger) one."""
    rows = [_row(latency_ms=100.0), _row(latency_ms=200.0)]

    m = compute_from_traces(rows, window_days=1.0)

    r = m.by_agent["agent"]
    assert r.latency_ms_median == pytest.approx(150.0)
    assert r.latency_ms_p95 == pytest.approx(200.0)


def test_p95_nearest_rank_for_larger_sample() -> None:
    """p95 by nearest-rank (no interpolation) matches hand computation for n=4."""
    rows = [_row(latency_ms=v) for v in (10.0, 20.0, 30.0, 40.0)]

    m = compute_from_traces(rows, window_days=1.0)

    # rank = max(1, min(4, ceil(0.95 * 4))) = ceil(3.8) = 4 -> the largest sample.
    assert m.by_agent["agent"].latency_ms_p95 == pytest.approx(40.0)


def test_cache_read_ratio_none_when_no_prompt_tokens() -> None:
    """cache_read_ratio is None (not 0.0) when the group processed zero prompt-side tokens."""
    rows = [_row(cost_usd=1.0, input_tokens=0, cache_read_tokens=0, cache_creation_tokens=0)]

    m = compute_from_traces(rows, window_days=1.0)

    r = m.by_agent["agent"]
    assert r.call_count == 1
    assert r.cache_read_ratio is None


def test_cache_read_ratio_zero_when_tokens_exist_but_none_cached() -> None:
    """cache_read_ratio is 0.0, not None, when prompt tokens exist but none were cache reads."""
    rows = [_row(input_tokens=100, cache_read_tokens=0, cache_creation_tokens=0)]

    m = compute_from_traces(rows, window_days=1.0)

    assert m.by_agent["agent"].cache_read_ratio == pytest.approx(0.0)


def test_empty_string_agent_and_phase_form_a_real_group() -> None:
    """Rows with an empty agent_key/phase are grouped under '""', not dropped."""
    rows = [_row(agent_key="", phase="")]

    m = compute_from_traces(rows, window_days=1.0)

    assert m.by_agent[""].call_count == 1
    assert m.by_phase[""].call_count == 1
    assert m.by_agent_phase[""][""].call_count == 1


def test_zero_call_group_is_zeros_and_nones() -> None:
    """A declared agent_key/phase pair with no rows reports zeros/None, not an omission or a raise."""
    m = compute_from_traces(
        [],
        window_days=7.0,
        expected_agent_keys=["backend"],
        expected_phases=["execution"],
    )

    r = m.by_agent_phase["backend"]["execution"]
    assert r.call_count == 0
    assert r.total_cost_usd == 0.0
    assert r.total_input_tokens == 0
    assert r.total_output_tokens == 0
    assert r.total_cache_read_tokens == 0
    assert r.total_cache_creation_tokens == 0
    assert r.cache_read_ratio is None
    assert r.latency_ms_median is None
    assert r.latency_ms_p95 is None
    assert r.latency_ms_sample_count == 0
    assert m.by_agent["backend"].call_count == 0
    assert m.by_phase["execution"].call_count == 0


def test_zero_call_group_equals_empty_group_rollup() -> None:
    """A zero-call declared group is byte-for-byte a default CallRollup — one code path, no drift."""
    m = compute_from_traces([], window_days=1.0, expected_agent_keys=["backend"])

    assert m.by_agent["backend"] == CallRollup()
    # expected_agent_keys alone (no expected_phases) gives the agent an empty
    # by_agent_phase entry, not a densified one.
    assert m.by_agent_phase["backend"] == {}


def test_expected_phases_only_densifies_observed_agents() -> None:
    """Passing expected_phases without expected_agent_keys densifies only observed agents."""
    rows = [_row(agent_key="backend", phase="execution")]

    m = compute_from_traces(rows, window_days=1.0, expected_phases=["design"])

    # Observed agent gets the declared phase as a zero-call group.
    assert m.by_agent_phase["backend"]["design"].call_count == 0
    # No new agent keys are synthesized.
    assert list(m.by_agent_phase.keys()) == ["backend"]
    # by_phase includes the declared phase.
    assert m.by_phase["design"].call_count == 0


def test_empty_window_with_expected_keys_yields_full_zero_call_grid() -> None:
    """An empty window plus declared agents/phases still produces the full cross-product, all zero-call."""
    m = compute_from_traces(
        [],
        window_days=7.0,
        expected_agent_keys=["backend", "frontend"],
        expected_phases=["design", "execution"],
    )

    assert list(m.by_agent.keys()) == ["backend", "frontend"]
    assert list(m.by_phase.keys()) == ["design", "execution"]
    assert list(m.by_agent_phase.keys()) == ["backend", "frontend"]
    for agent_key in ("backend", "frontend"):
        assert list(m.by_agent_phase[agent_key].keys()) == ["design", "execution"]
        for phase in ("design", "execution"):
            assert m.by_agent_phase[agent_key][phase].call_count == 0


def test_expected_keys_never_drop_observed_keys() -> None:
    """A declared agent/phase set is a union with what was observed, never a replacement."""
    rows = [_row(agent_key="backend", phase="execution", cost_usd=1.0, latency_ms=50)]

    m = compute_from_traces(
        rows,
        window_days=1.0,
        expected_agent_keys=["frontend"],
        expected_phases=["design"],
    )

    assert m.by_agent["backend"].call_count == 1
    assert m.by_agent["frontend"].call_count == 0
    assert m.by_phase["execution"].call_count == 1
    assert m.by_phase["design"].call_count == 0
    # The observed pair stays populated; the declared phase densifies every agent
    # in by_agent_phase, observed or declared.
    assert m.by_agent_phase["backend"]["execution"].call_count == 1
    assert m.by_agent_phase["backend"]["design"].call_count == 0
    assert m.by_agent_phase["frontend"]["design"].call_count == 0
    assert "execution" not in m.by_agent_phase["frontend"]


def test_expected_keys_default_and_empty_iterable_match_sparse_behavior() -> None:
    """Omitting expected_agent_keys/expected_phases and passing empty iterables are equivalent to today's sparse result."""
    rows = [_row(agent_key="backend", phase="execution")]

    default_result = compute_from_traces(rows, window_days=1.0)
    explicit_empty = compute_from_traces(
        rows, window_days=1.0, expected_agent_keys=[], expected_phases=[]
    )

    assert default_result.by_agent.keys() == explicit_empty.by_agent.keys()
    assert default_result.by_phase.keys() == explicit_empty.by_phase.keys()
    assert default_result.by_agent_phase.keys() == explicit_empty.by_agent_phase.keys()
    assert "frontend" not in default_result.by_agent


# --- compute_agent_rollup (the Postgres-reading wrapper) -----------------------
#
# These monkeypatch trace_store.fetch_traces_since with a spy rather than a fake
# cursor: fetch_traces_since's own SQL is covered by test_observability_stores.py,
# so these tests assert only what the wrapper itself is responsible for —
# deriving the window, forwarding job_id/expected keys, and delegating the math
# to compute_from_traces without adding any of its own.


def test_compute_agent_rollup_rejects_non_positive_window(monkeypatch) -> None:
    """window_days <= 0 raises before the store is ever called."""
    calls: list[Any] = []
    monkeypatch.setattr(
        trace_store, "fetch_traces_since", lambda *a, **kw: calls.append((a, kw)) or []
    )

    with pytest.raises(ValueError):
        compute_agent_rollup(0.0)

    assert calls == []


def test_compute_agent_rollup_derives_cutoff_from_window(monkeypatch) -> None:
    """The wrapper calls the store with cutoff = now - window_days, within tolerance."""
    captured: dict[str, Any] = {}

    def _spy(cutoff, *, job_id=None):
        captured["cutoff"] = cutoff
        captured["job_id"] = job_id
        return []

    monkeypatch.setattr(trace_store, "fetch_traces_since", _spy)

    before = datetime.now(tz=timezone.utc)
    compute_agent_rollup(7.0)
    after = datetime.now(tz=timezone.utc)

    assert before - timedelta(days=7) <= captured["cutoff"] <= after - timedelta(days=7)
    assert captured["job_id"] is None


def test_compute_agent_rollup_forwards_job_id(monkeypatch) -> None:
    """job_id — including "" — reaches the store as the keyword it was given."""
    captured: dict[str, Any] = {}

    def _spy(cutoff, *, job_id=None):
        captured["job_id"] = job_id
        return []

    monkeypatch.setattr(trace_store, "fetch_traces_since", _spy)

    compute_agent_rollup(1.0, job_id="j1")
    assert captured["job_id"] == "j1"

    compute_agent_rollup(1.0, job_id="")
    assert captured["job_id"] == ""


def test_compute_agent_rollup_delegates_math_to_compute_from_traces(monkeypatch) -> None:
    """Rows from the store produce the same result as calling compute_from_traces directly."""
    rows = [
        _row(agent_key="backend", phase="execution", cost_usd=1.0, input_tokens=10, latency_ms=100),
        _row(agent_key="frontend", phase="design", cost_usd=2.0, input_tokens=20, latency_ms=200),
    ]
    monkeypatch.setattr(trace_store, "fetch_traces_since", lambda *a, **kw: rows)

    wrapped = compute_agent_rollup(7.0).to_dict()
    direct = compute_from_traces(rows, window_days=7.0).to_dict()
    del wrapped["computed_at"]
    del direct["computed_at"]

    assert wrapped == direct


def test_compute_agent_rollup_empty_result_set_is_well_formed(monkeypatch) -> None:
    """An empty store read returns a well-formed AgentRollupMetrics, never a raise."""
    monkeypatch.setattr(trace_store, "fetch_traces_since", lambda *a, **kw: [])

    m = compute_agent_rollup(7.0)

    assert m.window_days == 7.0
    assert m.by_agent == {}
    assert m.by_phase == {}
    assert m.by_agent_phase == {}


def test_compute_agent_rollup_forwards_expected_keys(monkeypatch) -> None:
    """expected_agent_keys/expected_phases reach compute_from_traces, producing the zero-call grid."""
    monkeypatch.setattr(trace_store, "fetch_traces_since", lambda *a, **kw: [])

    m = compute_agent_rollup(7.0, expected_agent_keys=["backend"], expected_phases=["execution"])

    assert m.by_agent["backend"] == CallRollup()
    assert m.by_phase["execution"] == CallRollup()
    assert m.by_agent_phase["backend"]["execution"] == CallRollup()
