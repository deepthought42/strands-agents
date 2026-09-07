"""Postgres sink for per-LLM-call traces (``se_agent_traces``), enabled by default.

Unless ``SE_TRACE_TO_POSTGRES`` is explicitly disabled, an :mod:`llm_service`
call observer persists every SE-attributed LLM call as a row in
``se_agent_traces``. This is the substrate the DORA/cost endpoint reads for
per-job and total spend, so cost metrics work even without an OTLP collector.
Default on (opt out with ``SE_TRACE_TO_POSTGRES=false``); always a no-op when
Postgres is disabled. Writes never raise into the LLM call path.
"""

from __future__ import annotations

import logging
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Optional, Sequence

from shared.postgres import pg_cursor
from software_engineering_team.shared.env_config import env_bool, env_float

logger = logging.getLogger(__name__)

# The single INSERT statement shared by the one-shot and batched write paths.
# Column order here is the contract; :func:`_record_to_row` produces the matching
# positional tuple. Keeping both on this string means a column change is one edit.
_INSERT_SQL = (
    "INSERT INTO se_agent_traces (ts, team, agent_key, job_id, task_id, phase, model, "
    "input_tokens, output_tokens, total_tokens, cache_read_tokens, cache_creation_tokens, "
    "cost_usd, latency_ms, status, outcome, objective, request_id) VALUES "
    "(%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)"
)

# The exact column set the agent/phase rollup's pure computation reads (see
# metrics.agent_rollup._rollup_for_group) — deliberately not ``SELECT *``.
# se_agent_traces carries 19 columns; the rollup query reads only these eight.
# ``ts`` and ``job_id`` are the two other columns fetch_traces_since touches, but
# only in its WHERE clause, not projected here. The remaining nine — ``model``,
# ``status``, ``outcome``, ``objective``, ``request_id``, ``task_id``, ``team``,
# ``total_tokens``, ``id`` — are not needed at all, so a wide trace table never
# becomes a wide read.
_ROLLUP_COLUMNS = (
    "agent_key",
    "phase",
    "cost_usd",
    "input_tokens",
    "output_tokens",
    "cache_read_tokens",
    "cache_creation_tokens",
    "latency_ms",
)


def _trace_enabled() -> bool:
    """True unless ``SE_TRACE_TO_POSTGRES`` opts the Postgres trace sink out (default on)."""
    return env_bool("SE_TRACE_TO_POSTGRES", True)


def _retention_days() -> float:
    return env_float("SE_TRACE_RETENTION_DAYS", 30.0, 0.0)


def _record_to_row(record: Any) -> tuple:
    """Build the 18-element positional tuple for ``_INSERT_SQL`` from a record.

    Pure (no I/O): used both by :func:`write_trace` (single INSERT) and by the
    batched trace flusher (``executemany``) so the two paths cannot drift on
    column order. The row is resolved eagerly — the record's mutable fields are
    snapshotted into the tuple here, so a caller mutating the record afterwards
    cannot corrupt a buffered row.

    Preconditions:
        - ``record`` exposes the :class:`llm_service.telemetry.LLMCallRecord`
          attributes (``timestamp``, ``team``, ``model``, token counts, etc.);
          missing numeric fields default to 0/0.0, missing strings to "".
    Postconditions:
        - Returns an 18-element tuple in ``_INSERT_SQL`` column order. A record
          reporting no cache usage (missing or falsy ``cache_read_tokens`` /
          ``cache_creation_tokens``) writes 0, never NULL.
    Invariants:
        - Pure: no I/O, no mutation of ``record``. The tuple's column order
          always matches ``_INSERT_SQL`` — both write paths build rows through
          this single function so they cannot drift from one another.
    """
    # Use the record's own timestamp; fall back to *now* (not the 1970 epoch) for
    # a missing/invalid value so the row stays inside cost-query windows.
    raw_ts = getattr(record, "timestamp", None)
    epoch = raw_ts if isinstance(raw_ts, (int, float)) and raw_ts > 0 else time.time()
    ts = datetime.fromtimestamp(epoch, tz=timezone.utc)
    return (
        ts,
        getattr(record, "team", "") or "",
        getattr(record, "agent_key", "") or "",
        getattr(record, "job_id", "") or "",
        getattr(record, "task_id", "") or "",
        getattr(record, "phase", "") or "",
        getattr(record, "model", "") or "",
        int(getattr(record, "prompt_tokens", 0) or 0),
        int(getattr(record, "completion_tokens", 0) or 0),
        int(getattr(record, "total_tokens", 0) or 0),
        int(getattr(record, "cache_read_tokens", 0) or 0),
        int(getattr(record, "cache_creation_tokens", 0) or 0),
        float(getattr(record, "cost_usd", 0.0) or 0.0),
        int(getattr(record, "latency_ms", 0) or 0),
        getattr(record, "status", "") or "",
        getattr(record, "outcome", "") or "",
        getattr(record, "objective", "") or "",
        getattr(record, "request_id", "") or "",
    )


def write_trace(record: Any) -> bool:
    """Persist one LLM call ``record`` to ``se_agent_traces`` (sync one-shot).

    Preconditions:
        - ``record`` exposes the :class:`llm_service.telemetry.LLMCallRecord`
          attributes (``timestamp``, ``team``, ``model``, token counts, etc.).
    Postconditions:
        - Returns ``True`` when a row was written; ``False`` when the sink is
          disabled, Postgres is disabled, or the write failed (logged at DEBUG).
    Invariants:
        - Never raises into the caller — every failure mode (disabled sink,
          disabled Postgres, DB error) resolves to a boolean return.
    """
    if not _trace_enabled():
        return False
    try:
        with pg_cursor() as cur:
            if cur is None:
                return False
            cur.execute(_INSERT_SQL, _record_to_row(record))
        return True
    except Exception:
        logger.debug("failed to write se_agent_trace", exc_info=True)
        return False


def write_rows(rows: Sequence[tuple]) -> int:
    """Batch-persist pre-built trace row tuples via a single ``executemany``.

    The batched path used by :mod:`trace_flusher`: each row is a tuple already
    produced by :func:`_record_to_row`, so column order is fixed at the caller.
    Disabled-sink / disabled-Postgres / failure cases return 0 (logged at
    DEBUG) — a flush failure never raises into the flusher thread.

    Preconditions:
        - Every element of ``rows`` is an 18-element tuple in ``_INSERT_SQL``
          column order (build them with :func:`_record_to_row`).
    Postconditions:
        - Returns the number of rows written; 0 when the sink or Postgres is
          disabled or the write failed.
    Invariants:
        - Never raises into the caller. The write is all-or-nothing per batch:
          a failure never yields a partial-count result, only 0.
    """
    if not rows:
        return 0
    if not _trace_enabled():
        return 0
    try:
        with pg_cursor() as cur:
            if cur is None:
                return 0
            cur.executemany(_INSERT_SQL, list(rows))
        return len(rows)
    except Exception:
        logger.debug("failed to batch-write %d se_agent_traces", len(rows), exc_info=True)
        return 0


def fetch_cost_since(cutoff: datetime) -> dict[str, Any]:
    """Aggregate cost over ``se_agent_traces`` with ``ts >= cutoff``.

    Postconditions:
        - Returns ``{"total_cost_usd": float, "by_job": {job_id: cost}}``;
          zeros / empty when Postgres is disabled or on error.
    Raises:
        - ``ValueError`` if ``cutoff`` is naive — a naive bound is compared in the
          session TimeZone and would silently shift the aggregation window.
    """
    if cutoff.tzinfo is None:
        raise ValueError("cutoff must be a timezone-aware datetime")
    empty: dict[str, Any] = {"total_cost_usd": 0.0, "by_job": {}}
    try:
        with pg_cursor(dict_rows=True) as cur:
            if cur is None:
                return empty
            cur.execute(
                "SELECT job_id, SUM(cost_usd) AS cost FROM se_agent_traces "
                "WHERE ts >= %s AND job_id <> '' GROUP BY job_id",
                (cutoff,),
            )
            by_job = {r["job_id"]: float(r["cost"] or 0.0) for r in cur.fetchall()}
        return {"total_cost_usd": round(sum(by_job.values()), 6), "by_job": by_job}
    except Exception:
        logger.debug("failed to fetch cost since %s", cutoff, exc_info=True)
        return empty


def fetch_traces_since(cutoff: datetime, *, job_id: Optional[str] = None) -> list[dict[str, Any]]:
    """Return ``se_agent_traces`` rows with ``ts >= cutoff``, for the agent/phase rollup.

    A thin, narrow read: it selects only :data:`_ROLLUP_COLUMNS` — the columns
    :func:`software_engineering_team.metrics.agent_rollup.compute_from_traces` reads —
    and does no aggregation or arithmetic of its own; grouping and percentiles are
    the pure function's job, not this one's.

    Preconditions:
        - ``cutoff`` is a timezone-aware datetime.
        - ``job_id``, if given, is matched by exact equality. ``""`` is a real,
          queryable ``job_id`` (the column's default for calls that aren't
          job-attributed), not a "no filter" sentinel — pass ``None`` (the
          default) to skip the ``job_id`` predicate entirely and read every job.
    Postconditions:
        - Returns a list of dict rows (keys: the members of :data:`_ROLLUP_COLUMNS`);
          ``[]`` when Postgres is disabled or the query fails (logged at DEBUG).
        - Rows are returned in no particular order: the caller is a pure
          aggregation that does not depend on row order, so no ``ORDER BY`` is
          applied — sorting here would cost a query-time sort for no behavioral
          gain.
        - A wide window with no ``job_id`` filter can return every matching row;
          percentiles cannot be computed SQL-side, so this intentionally has no
          row cap. The column projection above is the narrowing this function
          applies; truncating rows would silently understate the rollup's
          counts and percentiles, which is worse than a slow query.
    Raises:
        - ``ValueError`` if ``cutoff`` is naive — a naive bound is compared in the
          session TimeZone and would silently shift the aggregation window.
    """
    if cutoff.tzinfo is None:
        raise ValueError("cutoff must be a timezone-aware datetime")
    try:
        with pg_cursor(dict_rows=True) as cur:
            if cur is None:
                return []
            where = "ts >= %s"
            params: list[Any] = [cutoff]  # 1) WHERE ts >= %s
            if job_id is not None:
                where += " AND job_id = %s"
                params.append(job_id)  # 2) optional exact job_id match
            cur.execute(
                f"SELECT {', '.join(_ROLLUP_COLUMNS)} FROM se_agent_traces WHERE {where}",
                tuple(params),
            )
            rows = list(cur.fetchall())
        return rows
    except Exception:
        logger.debug("failed to fetch traces since %s (job_id=%r)", cutoff, job_id, exc_info=True)
        return []


def prune_traces(retention_days: float | None = None) -> int:
    """Delete traces older than the retention window; returns rows removed."""
    days = _retention_days() if retention_days is None else retention_days
    if days <= 0:
        return 0
    try:
        with pg_cursor() as cur:
            if cur is None:
                return 0
            cutoff = datetime.now(tz=timezone.utc) - timedelta(days=days)
            cur.execute("DELETE FROM se_agent_traces WHERE ts < %s", (cutoff,))
            removed = cur.rowcount or 0
        return removed
    except Exception:
        logger.debug("failed to prune se_agent_traces", exc_info=True)
        return 0


__all__ = [
    "write_trace",
    "write_rows",
    "fetch_cost_since",
    "fetch_traces_since",
    "prune_traces",
]
