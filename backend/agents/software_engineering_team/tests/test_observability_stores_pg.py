"""Postgres-backed round-trip tests for the SE observability stores.

Skipped unless run with ``-m integration`` against a live Postgres.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from ._observability_test_doubles import TraceCallRecord as _Rec

pytestmark = pytest.mark.integration


@pytest.fixture
def _schema():
    from shared.postgres import get_conn, is_postgres_enabled, register_team_schemas

    if not is_postgres_enabled():
        pytest.skip("Postgres not configured")
    from software_engineering_team.postgres import SCHEMA

    register_team_schemas(SCHEMA)
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute("TRUNCATE se_learnings, se_events, se_agent_traces")
    yield
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute("TRUNCATE se_learnings, se_events, se_agent_traces")


def test_learnings_upsert_dedup_and_retrieve(_schema) -> None:
    """Upserting the same fingerprint bumps occurrences and refreshes the counter-measure."""
    from software_engineering_team.shared import learnings_store as ls

    assert ls.upsert_learning(
        pattern="security rejection", trigger="hardcoded api key", counter_measure="use env"
    )
    # Same fingerprint → occurrences bump, not a new row.
    assert ls.upsert_learning(
        pattern="Security Rejection", trigger="Hardcoded API key", counter_measure="use vault"
    )
    assert ls.count_learnings() == 1

    hits = ls.retrieve_learnings("hardcoded api key handling")
    assert len(hits) == 1
    assert hits[0].occurrences == 2
    assert hits[0].counter_measure == "use vault"  # refreshed on upsert


def test_learnings_category_filter(_schema) -> None:
    """Learnings category filter."""
    from software_engineering_team.shared import learnings_store as ls

    ls.upsert_learning(pattern="qa flake", trigger="timing flaky test", category="qa")
    ls.upsert_learning(pattern="sec issue", trigger="flaky injection", category="security")
    qa_only = ls.retrieve_learnings("flaky", category="qa")
    assert [h.category for h in qa_only] == ["qa"]


def test_events_roundtrip_and_dora(_schema) -> None:
    """Events roundtrip and dora."""
    from software_engineering_team.metrics.dora import compute_dora
    from software_engineering_team.shared import se_events

    assert se_events.record_event(se_events.MERGE_TO_MAIN, job_id="j1")
    assert se_events.record_event(se_events.TASK_CREATED, job_id="j1", task_id="t1")
    assert se_events.record_event(se_events.TASK_MERGED, job_id="j1", task_id="t1")

    rows = se_events.fetch_events_since(datetime.now(tz=timezone.utc) - timedelta(days=1))
    assert len(rows) == 3

    m = compute_dora(30.0)
    assert m.deployment_count == 1
    assert m.merged_count == 1


def test_record_event_persists_bound_trace_id(_schema) -> None:
    """An event recorded inside bind_trace_id persists that trace_id and surfaces it in DORA."""
    from shared.observability import bind_trace_id
    from software_engineering_team.metrics.dora import compute_dora
    from software_engineering_team.shared import se_events

    with bind_trace_id("abc123def456"):
        assert se_events.record_event(se_events.MERGE_TO_MAIN, job_id="jT")

    rows = se_events.fetch_events_since(datetime.now(tz=timezone.utc) - timedelta(days=1))
    assert rows[0]["trace_id"] == "abc123def456"

    m = compute_dora(30.0)
    assert m.trace_ids_by_job == {"jT": "abc123def456"}


def test_record_event_explicit_trace_id_overrides_context(_schema) -> None:
    """An explicit trace_id kwarg wins over any trace_id bound in the current context."""
    from shared.observability import bind_trace_id
    from software_engineering_team.shared import se_events

    with bind_trace_id("context-trace"):
        assert se_events.record_event(
            se_events.MERGE_TO_MAIN, job_id="jT", trace_id="explicit-trace"
        )

    rows = se_events.fetch_events_since(datetime.now(tz=timezone.utc) - timedelta(days=1))
    assert rows[0]["trace_id"] == "explicit-trace"


def test_emit_coding_team_metrics_populates_dora(_schema, monkeypatch) -> None:
    """The live coding_team path's metrics helper turns a task graph into DORA events."""
    from datetime import datetime, timedelta, timezone

    from software_engineering_team import orchestrator
    from software_engineering_team.metrics.dora import compute_dora

    created = datetime.now(tz=timezone.utc) - timedelta(hours=1)
    merged = datetime.now(tz=timezone.utc) - timedelta(minutes=30)
    fake_job = {
        "created_at": created.isoformat(),
        "status": "completed",
        "task_graph_snapshot": [
            {"id": "t1", "status": "merged", "merged_at": merged.isoformat(), "revision_count": 0},
            {"id": "t2", "status": "merged", "merged_at": merged.isoformat(), "revision_count": 2},
            {"id": "t3", "status": "failed", "merged_at": None, "revision_count": 0},
        ],
    }
    monkeypatch.setattr(orchestrator, "get_job", lambda jid: fake_job)
    monkeypatch.setattr(orchestrator.cost_tracker, "flush", lambda jid: None)

    orchestrator._emit_coding_team_metrics("job-ct")
    # Idempotent: a resume/re-run must not double-count the deployment or re-entries.
    orchestrator._emit_coding_team_metrics("job-ct")

    m = compute_dora(30.0)
    assert m.deployment_count == 1  # one MERGE_TO_MAIN despite two emit calls
    assert m.merged_count == 2  # t1 + t2 (t3 failed)
    assert m.gate_reentry_count == 1  # t2 needed revisions
    assert m.change_failure_rate == pytest.approx(0.5)
    assert m.lead_time_sample_count == 2
    # lead time ~30 min (job creation → merge), tolerant of test timing.
    assert m.lead_time_seconds_median == pytest.approx(1800, abs=60)


def test_emit_coding_team_metrics_resumed_job_captures_new_merges(_schema, monkeypatch) -> None:
    """A resumed run records newly-merged tasks without re-counting prior ones.

    Regression: the idempotency guard used to skip the whole batch when *any*
    event existed for the job, so a resume with new merges recorded nothing. The
    guard is now per ``(job, task)``: the first run's events are not duplicated and
    the second run's new merge (plus the now-completed ``merge_to_main``) is added.
    """
    from software_engineering_team import orchestrator
    from software_engineering_team.metrics.dora import compute_dora

    created = datetime.now(tz=timezone.utc) - timedelta(hours=1)
    merged = datetime.now(tz=timezone.utc) - timedelta(minutes=30)

    # First run: only t1 merged, job still running (no merge_to_main yet).
    first = {
        "created_at": created.isoformat(),
        "status": "running",
        "task_graph_snapshot": [
            {"id": "t1", "status": "merged", "merged_at": merged.isoformat(), "revision_count": 0},
        ],
    }
    monkeypatch.setattr(orchestrator, "get_job", lambda jid: first)
    monkeypatch.setattr(orchestrator.cost_tracker, "flush", lambda jid: None)
    orchestrator._emit_coding_team_metrics("job-rt")

    # Resume: t2 now merged (and needed a revision); job completed.
    second = {
        "created_at": created.isoformat(),
        "status": "completed",
        "task_graph_snapshot": [
            {"id": "t1", "status": "merged", "merged_at": merged.isoformat(), "revision_count": 0},
            {"id": "t2", "status": "merged", "merged_at": merged.isoformat(), "revision_count": 1},
        ],
    }
    monkeypatch.setattr(orchestrator, "get_job", lambda jid: second)
    orchestrator._emit_coding_team_metrics("job-rt")

    m = compute_dora(30.0)
    assert m.merged_count == 2  # t1 (run 1) + t2 (run 2), t1 not double-counted
    assert m.deployment_count == 1  # merge_to_main emitted once, on the completing run
    assert m.gate_reentry_count == 1  # only t2 needed a revision


def test_se_events_helpers(_schema) -> None:
    """Se events helpers."""
    from software_engineering_team.shared import se_events

    assert se_events.job_has_events("jX") is False
    se_events.record_event(se_events.CRASH_DETECTED, job_id="jX", task_id="t1")
    assert se_events.job_has_events("jX") is True
    assert se_events.job_has_events("jX", se_events.MERGE_TO_MAIN) is False
    # One unresolved crash for t1.
    assert se_events.unresolved_crashed_task_ids("jX") == {"t1"}
    se_events.record_event(se_events.CRASH_RESOLVED, job_id="jX", task_id="t1")
    assert se_events.unresolved_crashed_task_ids("jX") == set()


def test_record_event_coerces_naive_ts_to_utc(_schema) -> None:
    """Record event coerces naive ts to utc."""
    from datetime import datetime as _dt

    from software_engineering_team.shared import se_events

    naive = _dt(2026, 6, 1, 12, 0, 0)  # no tzinfo
    se_events.record_event(se_events.MERGE_TO_MAIN, job_id="jZ", ts=naive)
    rows = se_events.fetch_events_since(_dt(2026, 5, 1, tzinfo=timezone.utc))
    jz = [r for r in rows if r["job_id"] == "jZ"]
    assert jz and jz[0]["ts"].tzinfo is not None  # stored/returned tz-aware


def test_trace_write_and_cost(_schema, monkeypatch) -> None:
    """Trace write and cost."""
    monkeypatch.setenv("SE_TRACE_TO_POSTGRES", "true")
    from shared.postgres import get_conn
    from software_engineering_team.shared import trace_store

    rec = _Rec(
        job_id="j9",
        task_id="t1",
        model="deepseek-v4-pro:cloud",
        prompt_tokens=1000,
        completion_tokens=500,
        total_tokens=1500,
        cache_read_tokens=300,
        cost_usd=0.42,
        latency_ms=1200,
        objective="write code",
        request_id="rid1",
    )

    assert trace_store.write_trace(rec) is True
    summary = trace_store.fetch_cost_since(datetime.now(tz=timezone.utc) - timedelta(days=1))
    assert summary["total_cost_usd"] == pytest.approx(0.42)
    assert summary["by_job"]["j9"] == pytest.approx(0.42)

    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT cache_read_tokens, cache_creation_tokens FROM se_agent_traces WHERE job_id = %s",
            ("j9",),
        )
        row = cur.fetchone()
    assert row == (300, 0)


def test_trace_write_cache_creation_and_no_cache_usage(_schema, monkeypatch) -> None:
    """A record reporting cache creation, and one reporting neither, both round-trip."""
    monkeypatch.setenv("SE_TRACE_TO_POSTGRES", "true")
    from shared.postgres import get_conn
    from software_engineering_team.shared import trace_store

    rec_create = _Rec(job_id="jCreate", task_id="t1", cache_creation_tokens=200, request_id="rc1")
    rec_none = _Rec(job_id="jNone", task_id="t1", request_id="rn1")

    assert trace_store.write_trace(rec_create) is True
    assert trace_store.write_trace(rec_none) is True

    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT job_id, cache_read_tokens, cache_creation_tokens FROM se_agent_traces "
            "WHERE job_id IN (%s, %s) ORDER BY job_id",
            ("jCreate", "jNone"),
        )
        rows = {r[0]: (r[1], r[2]) for r in cur.fetchall()}
    assert rows["jCreate"] == (0, 200)
    assert rows["jNone"] == (0, 0)  # no cache attrs on the record -> writes 0, never NULL


def test_write_rows_batch_roundtrip(_schema, monkeypatch) -> None:
    """The batched executemany path writes rows identical to the one-shot path."""
    monkeypatch.setenv("SE_TRACE_TO_POSTGRES", "true")
    from software_engineering_team.shared import trace_store

    recs = [
        _Rec(
            job_id="jB",
            task_id=f"t{i}",
            cache_read_tokens=50 * (i + 1),
            cost_usd=0.01 * (i + 1),
            request_id=f"r{i}",
        )
        for i in range(3)
    ]
    rows = [trace_store._record_to_row(r) for r in recs]

    assert trace_store.write_rows(rows) == 3
    assert trace_store.write_rows([]) == 0  # empty batch is a no-op

    summary = trace_store.fetch_cost_since(datetime.now(tz=timezone.utc) - timedelta(days=1))
    assert summary["by_job"]["jB"] == pytest.approx(0.01 + 0.02 + 0.03)

    from shared.postgres import get_conn

    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT cache_read_tokens FROM se_agent_traces WHERE job_id = %s ORDER BY task_id",
            ("jB",),
        )
        assert [r[0] for r in cur.fetchall()] == [50, 100, 150]


def test_prune_traces_deletes_only_stale_rows(_schema, monkeypatch) -> None:
    """prune_traces deletes rows older than the retention window and leaves
    recent ones intact, returning the exact count removed — and does so with
    SE_TRACE_TO_POSTGRES explicitly disabled (not merely unset — the flag now
    defaults to enabled), proving prune_traces ignores the flag's value
    entirely: rows written while tracing was on must still be pruned after
    it's turned off."""
    monkeypatch.setenv("SE_TRACE_TO_POSTGRES", "true")
    from shared.postgres import get_conn
    from software_engineering_team.shared import trace_store

    now = datetime.now(tz=timezone.utc)
    old_ts = (now - timedelta(days=40)).timestamp()
    recent_ts = (now - timedelta(days=5)).timestamp()
    recs = [
        _Rec(job_id="jOld", task_id=f"told{i}", timestamp=old_ts, request_id=f"rold{i}")
        for i in range(3)
    ] + [
        _Rec(job_id="jNew", task_id=f"tnew{i}", timestamp=recent_ts, request_id=f"rnew{i}")
        for i in range(2)
    ]
    assert trace_store.write_rows([trace_store._record_to_row(r) for r in recs]) == 5

    monkeypatch.setenv("SE_TRACE_TO_POSTGRES", "false")
    removed = trace_store.prune_traces(retention_days=30)
    assert removed == 3

    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT DISTINCT job_id FROM se_agent_traces WHERE job_id IN (%s, %s)",
            ("jOld", "jNew"),
        )
        remaining_jobs = {r[0] for r in cur.fetchall()}
    assert remaining_jobs == {"jNew"}


def test_prune_traces_zero_retention_is_noop(_schema, monkeypatch) -> None:
    """A retention_days of 0 (or less) prunes nothing, matching the existing
    _retention_days()<=0 short-circuit — no accidental full-table wipe. Also
    runs with SE_TRACE_TO_POSTGRES explicitly disabled, same reasoning as the
    test above."""
    monkeypatch.setenv("SE_TRACE_TO_POSTGRES", "true")
    from shared.postgres import get_conn
    from software_engineering_team.shared import trace_store

    old_ts = (datetime.now(tz=timezone.utc) - timedelta(days=400)).timestamp()
    rec = _Rec(job_id="jAncient", task_id="t0", timestamp=old_ts, request_id="r0")
    assert trace_store.write_rows([trace_store._record_to_row(rec)]) == 1

    monkeypatch.setenv("SE_TRACE_TO_POSTGRES", "false")
    assert trace_store.prune_traces(retention_days=0) == 0
    assert trace_store.prune_traces(retention_days=-1) == 0

    with get_conn() as conn, conn.cursor() as cur:
        cur.execute("SELECT COUNT(*) FROM se_agent_traces WHERE job_id = %s", ("jAncient",))
        assert cur.fetchone()[0] == 1


def test_compute_agent_rollup_groups_written_traces(_schema, monkeypatch) -> None:
    """The end-to-end read path: written traces round-trip through compute_agent_rollup,
    grouped by agent_key/phase, with a job_id filter narrowing to one job's rows.

    Exercises the actual column names on both sides of the wire (schema, insert,
    select) — the fake-cursor unit tests can't catch a drift there, since the fake
    never validates against a real schema.
    """
    monkeypatch.setenv("SE_TRACE_TO_POSTGRES", "true")
    from software_engineering_team.metrics.agent_rollup import compute_agent_rollup
    from software_engineering_team.shared import trace_store

    assert (
        trace_store.write_trace(
            _Rec(
                job_id="jRollup1",
                agent_key="backend",
                phase="execution",
                cost_usd=1.0,
                prompt_tokens=100,
                completion_tokens=50,
                cache_read_tokens=20,
                latency_ms=150,
                request_id="rr1",
            )
        )
        is True
    )
    assert (
        trace_store.write_trace(
            _Rec(
                job_id="jRollup2",
                agent_key="frontend",
                phase="design",
                cost_usd=2.0,
                prompt_tokens=200,
                completion_tokens=80,
                cache_creation_tokens=40,
                latency_ms=300,
                request_id="rr2",
            )
        )
        is True
    )

    m = compute_agent_rollup(1.0)
    assert m.by_agent["backend"].call_count == 1
    assert m.by_agent["frontend"].call_count == 1
    assert m.by_phase["execution"].call_count == 1
    assert m.by_phase["design"].call_count == 1
    assert m.by_agent_phase["backend"]["execution"].total_cost_usd == pytest.approx(1.0)

    narrowed = compute_agent_rollup(1.0, job_id="jRollup1")
    assert narrowed.by_agent["backend"].call_count == 1
    assert "frontend" not in narrowed.by_agent
    assert narrowed.by_phase["execution"].call_count == 1
    assert "design" not in narrowed.by_phase
