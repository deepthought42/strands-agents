"""Unit tests for the guarded (no-Postgres) behaviour of the SE observability stores."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from software_engineering_team.shared import learnings_store, se_events, trace_store

from ._observability_test_doubles import _FIELDS, _FakeCursor, install_fake_cursor
from ._observability_test_doubles import TraceCallRecord as _Rec

# --- se_events -------------------------------------------------------------


def test_record_event_requires_type() -> None:
    """record_event rejects an empty event type."""
    with pytest.raises(ValueError):
        se_events.record_event("")


def test_record_event_noop_without_postgres() -> None:
    """record_event is a guarded no-op returning False when Postgres is unconfigured."""
    # Default test env has POSTGRES_HOST unset → guarded no-op returns False.
    assert se_events.record_event("task_created", job_id="j", task_id="t") is False


def test_fetch_events_empty_without_postgres() -> None:
    """fetch_events_since returns an empty list when Postgres is unconfigured."""
    assert se_events.fetch_events_since(datetime.now(tz=timezone.utc)) == []


def test_prune_events_noop() -> None:
    """prune_events returns 0 when Postgres is unconfigured."""
    assert se_events.prune_events(0) == 0
    assert se_events.prune_events(30) == 0


# --- learnings_store -------------------------------------------------------


def test_fingerprint_is_stable_and_normalized() -> None:
    """fingerprint is case/whitespace-normalized, 32 chars, and category-sensitive."""
    a = learnings_store.fingerprint("Build  FAILED", "missing import", "qa")
    b = learnings_store.fingerprint("build failed", "Missing Import", "QA")
    assert a == b
    assert len(a) == 32
    c = learnings_store.fingerprint("build failed", "missing import", "security")
    assert c != a


def test_upsert_requires_pattern() -> None:
    """upsert_learning rejects a blank pattern."""
    with pytest.raises(ValueError):
        learnings_store.upsert_learning(pattern="   ")


def test_upsert_noop_without_postgres() -> None:
    """upsert_learning is a no-op returning False when Postgres is unconfigured."""
    assert learnings_store.upsert_learning(pattern="p", trigger="t", counter_measure="c") is False


def test_upsert_batch_empty_is_noop() -> None:
    """upsert_learnings_batch short-circuits to 0 for an empty entry list."""
    assert learnings_store.upsert_learnings_batch([]) == 0


def test_upsert_batch_rejects_blank_pattern() -> None:
    """upsert_learnings_batch rejects a blank pattern anywhere in the batch."""
    entries = [
        learnings_store.LearningEntry(pattern="ok"),
        learnings_store.LearningEntry(pattern="   "),
    ]
    with pytest.raises(ValueError):
        learnings_store.upsert_learnings_batch(entries)


def test_upsert_batch_noop_without_postgres() -> None:
    """upsert_learnings_batch is a no-op returning 0 when Postgres is unconfigured."""
    entries = [
        learnings_store.LearningEntry(pattern="p1"),
        learnings_store.LearningEntry(pattern="p2"),
    ]
    assert learnings_store.upsert_learnings_batch(entries) == 0


def test_retrieve_empty_query_returns_empty() -> None:
    """retrieve_learnings returns an empty list for a blank query."""
    assert learnings_store.retrieve_learnings("") == []
    assert learnings_store.retrieve_learnings("   ") == []


def test_retrieve_top_n_must_be_positive() -> None:
    """retrieve_learnings rejects a non-positive top_n."""
    with pytest.raises(ValueError):
        learnings_store.retrieve_learnings("anything", top_n=0)


def test_retrieve_noop_without_postgres() -> None:
    """retrieve_learnings returns an empty list when Postgres is unconfigured."""
    assert learnings_store.retrieve_learnings("some spec text") == []


def test_count_and_prune_noop() -> None:
    """count_learnings and prune_learnings return 0 when Postgres is unconfigured."""
    assert learnings_store.count_learnings() == 0
    assert learnings_store.prune_learnings(0) == 0


def test_or_tsquery_terms_builds_or_query() -> None:
    """_or_tsquery_terms lowercases, strips, dedups, drops short words, and OR-joins terms."""
    f = learnings_store._or_tsquery_terms
    # Empty / whitespace / all-too-short → empty string.
    assert f("") == ""
    assert f("   ") == ""
    assert f("a b cd") == ""  # every term < 3 chars
    # Lowercased, special chars stripped, de-duplicated (order preserved), OR-joined.
    assert f("Foo foo BAR!") == "foo | bar"
    assert f("special!char @# test") == "special | char | test"
    # Words shorter than 3 chars are dropped; digits are kept.
    assert f("the api v2 gateway") == "the | api | gateway"
    # The term limit caps how many are emitted.
    assert f("aaa bbb ccc ddd", limit=2) == "aaa | bbb"


def test_learnings_retention_days_env(monkeypatch) -> None:
    """_retention_days defaults to 365, parses overrides, and clamps garbage and negatives."""
    monkeypatch.delenv("SE_LEARNINGS_RETENTION_DAYS", raising=False)
    assert learnings_store._retention_days() == 365.0
    monkeypatch.setenv("SE_LEARNINGS_RETENTION_DAYS", "10")
    assert learnings_store._retention_days() == 10.0
    monkeypatch.setenv("SE_LEARNINGS_RETENTION_DAYS", "garbage")
    assert learnings_store._retention_days() == 365.0  # bad value → default
    monkeypatch.setenv("SE_LEARNINGS_RETENTION_DAYS", "-5")
    assert learnings_store._retention_days() == 0.0  # clamped to floor


# --- trace_store -----------------------------------------------------------

# Positions of the two cache-token columns in ``_INSERT_SQL`` / the tuple
# ``_record_to_row`` builds. Every cache-token assertion in this module reads
# through these (via :func:`_cache_tokens`) rather than slicing by literal, so a
# column reorder is a one-line change here — and
# ``test_insert_sql_pins_cache_column_positions`` fails loudly if these drift
# from what the statement actually declares.
_CACHE_READ_IDX = 10
_CACHE_CREATION_IDX = 11


def _cache_tokens(row) -> tuple:
    """The ``(cache_read_tokens, cache_creation_tokens)`` pair read from ``row``.

    Preconditions:
        ``row`` is a positional row tuple in ``_INSERT_SQL`` column order — one
        built by ``trace_store._record_to_row``, or the params of a recorded
        INSERT.
    Postconditions:
        Returns the two cache-token values at the pinned indices, in
        (read, creation) order. Indexes each column independently, so the pair
        does not assume the two columns stay adjacent.
    """
    return (row[_CACHE_READ_IDX], row[_CACHE_CREATION_IDX])


def test_trace_enabled_env(monkeypatch) -> None:
    """_trace_enabled defaults to True (unset) and follows explicit SE_TRACE_TO_POSTGRES overrides."""
    monkeypatch.delenv("SE_TRACE_TO_POSTGRES", raising=False)
    assert trace_store._trace_enabled() is True
    monkeypatch.setenv("SE_TRACE_TO_POSTGRES", "true")
    assert trace_store._trace_enabled() is True
    monkeypatch.setenv("SE_TRACE_TO_POSTGRES", "no")
    assert trace_store._trace_enabled() is False


class _TraceRec:
    """Minimal write_trace-shaped stub shared by the tests below."""

    timestamp = 0.0
    team = "software_engineering"
    job_id = "j"


def test_write_trace_noop_without_postgres_when_enabled_by_default(monkeypatch) -> None:
    """write_trace returns False when Postgres is unconfigured, even though the sink is
    enabled by default (unset SE_TRACE_TO_POSTGRES) — pg_cursor yields no cursor."""
    monkeypatch.delenv("SE_TRACE_TO_POSTGRES", raising=False)
    monkeypatch.delenv(
        "POSTGRES_HOST", raising=False
    )  # force the documented "Postgres disabled" no-op path

    assert trace_store.write_trace(_TraceRec()) is False


def test_write_trace_disabled_explicitly(monkeypatch) -> None:
    """write_trace returns False when the sink is explicitly opted out."""
    monkeypatch.setenv("SE_TRACE_TO_POSTGRES", "false")

    assert trace_store.write_trace(_TraceRec()) is False


def test_fetch_cost_empty_without_postgres() -> None:
    """fetch_cost_since returns a zeroed summary when Postgres is unconfigured."""
    out = trace_store.fetch_cost_since(datetime.now(tz=timezone.utc))
    assert out == {"total_cost_usd": 0.0, "by_job": {}}


# --- trace_store.fetch_traces_since (rollup's read path, no live Postgres) -----


def test_fetch_traces_since_naive_cutoff_rejected() -> None:
    """A naive cutoff raises before any DB work — a silently shifted window is a caller bug."""
    with pytest.raises(ValueError):
        trace_store.fetch_traces_since(datetime.now())


def test_fetch_traces_empty_without_postgres() -> None:
    """fetch_traces_since returns [] when Postgres is unconfigured."""
    assert trace_store.fetch_traces_since(datetime.now(tz=timezone.utc)) == []


def test_fetch_traces_since_noop_attempts_no_connection(monkeypatch) -> None:
    """Without POSTGRES_HOST, fetch_traces_since never reaches get_conn.

    Same "gate actually short-circuited, not just swallowed" proof as
    test_prune_traces_noop above: a return-value-only assertion can't tell the
    two apart.
    """
    monkeypatch.delenv("POSTGRES_HOST", raising=False)

    from shared.postgres import client as pg_client

    connection_attempts = []

    def _record_and_fail(*args, **kwargs):
        connection_attempts.append((args, kwargs))
        raise RuntimeError("fetch_traces_since must not reach get_conn without POSTGRES_HOST")

    monkeypatch.setattr(pg_client, "get_conn", _record_and_fail)

    assert trace_store.fetch_traces_since(datetime.now(tz=timezone.utc)) == []
    assert connection_attempts == [], (
        "fetch_traces_since attempted a DB connection without POSTGRES_HOST"
    )


def test_fetch_traces_since_selects_only_rollup_columns(_fake_cursor) -> None:
    """The SELECT names exactly the rollup's columns — never a wide SELECT *."""
    cursor = _fake_cursor(rows=[])

    trace_store.fetch_traces_since(datetime.now(tz=timezone.utc))

    sql, _ = cursor.executed[0]
    assert "select *" not in sql.lower()
    for column in trace_store._ROLLUP_COLUMNS:
        assert column in sql


def test_fetch_traces_since_filters_on_window(_fake_cursor) -> None:
    """With no job_id, the SQL binds only the cutoff and carries no job_id predicate."""
    cursor = _fake_cursor(rows=[])
    cutoff = datetime.now(tz=timezone.utc) - timedelta(days=7)

    trace_store.fetch_traces_since(cutoff)

    sql, params = cursor.executed[0]
    assert "ts >= %s" in sql
    assert "job_id" not in sql
    assert params == (cutoff,)


def test_fetch_traces_since_filters_on_job_id(_fake_cursor) -> None:
    """A job_id filter appends AND job_id = %s, bound after the cutoff."""
    cursor = _fake_cursor(rows=[])
    cutoff = datetime.now(tz=timezone.utc)

    trace_store.fetch_traces_since(cutoff, job_id="j1")

    sql, params = cursor.executed[0]
    assert "ts >= %s" in sql
    assert "job_id = %s" in sql
    assert params == (cutoff, "j1")


def test_fetch_traces_since_empty_job_id_is_a_real_filter(_fake_cursor) -> None:
    """job_id="" filters on the empty-string job — it is not a "no filter" sentinel."""
    cursor = _fake_cursor(rows=[])
    cutoff = datetime.now(tz=timezone.utc)

    trace_store.fetch_traces_since(cutoff, job_id="")

    sql, params = cursor.executed[0]
    assert "job_id = %s" in sql
    assert params == (cutoff, "")


def test_fetch_traces_since_returns_empty_result_set(_fake_cursor) -> None:
    """An empty result set returns [] — not a raise, not a synthesized row."""
    _fake_cursor(rows=[])
    assert trace_store.fetch_traces_since(datetime.now(tz=timezone.utc)) == []


def test_fetch_traces_since_returns_queued_rows_unmodified(_fake_cursor) -> None:
    """Rows come back exactly as the cursor yields them — no reshaping in the store."""
    row = {
        "agent_key": "backend",
        "phase": "execution",
        "cost_usd": 1.5,
        "input_tokens": 100,
        "output_tokens": 50,
        "cache_read_tokens": 10,
        "cache_creation_tokens": 5,
        "latency_ms": 250,
    }
    _fake_cursor(rows=[row])

    rows = trace_store.fetch_traces_since(datetime.now(tz=timezone.utc))

    assert rows == [row]


def test_fetch_traces_since_never_raises_on_cursor_failure(_fake_cursor) -> None:
    """A cursor failure degrades to [] rather than propagating."""
    _fake_cursor(raise_on_execute=True)
    assert trace_store.fetch_traces_since(datetime.now(tz=timezone.utc)) == []


def test_trace_observer_ignores_non_se(monkeypatch) -> None:
    """The trace observer does not enqueue traces for non-SE teams."""
    from software_engineering_team.shared import trace_flusher

    trace_flusher._reset_for_test()

    trace_flusher._trace_observer(_Rec(team="blogging"))
    assert trace_flusher._buffer_size() == 0  # other team → not enqueued
    trace_flusher._reset_for_test()


def test_record_to_row_cache_tokens() -> None:
    """_record_to_row carries cache_read/cache_creation tokens, defaulting to 0 not NULL."""

    class _RecWithRead:
        timestamp = 0.0
        team = "software_engineering"
        job_id = "j"
        cache_read_tokens = 42
        cache_creation_tokens = 0

    class _RecWithCreation:
        timestamp = 0.0
        team = "software_engineering"
        job_id = "j"
        cache_read_tokens = 0
        cache_creation_tokens = 17

    class _RecWithNeither:
        timestamp = 0.0
        team = "software_engineering"
        job_id = "j"

    row_read = trace_store._record_to_row(_RecWithRead())
    row_creation = trace_store._record_to_row(_RecWithCreation())
    row_neither = trace_store._record_to_row(_RecWithNeither())

    assert _cache_tokens(row_read) == (42, 0)
    assert _cache_tokens(row_creation) == (0, 17)
    assert _cache_tokens(row_neither) == (0, 0)  # missing attrs -> 0, never NULL


def test_trace_retention_days_env(monkeypatch) -> None:
    """_retention_days defaults to 30, parses overrides, and falls back on garbage."""
    monkeypatch.delenv("SE_TRACE_RETENTION_DAYS", raising=False)
    assert trace_store._retention_days() == 30.0
    monkeypatch.setenv("SE_TRACE_RETENTION_DAYS", "7")
    assert trace_store._retention_days() == 7.0
    monkeypatch.setenv("SE_TRACE_RETENTION_DAYS", "garbage")
    assert trace_store._retention_days() == 30.0  # bad value → default


def test_prune_traces_noop(monkeypatch) -> None:
    """prune_traces is a safe no-op without Postgres configured — proven by
    absence of a real connection attempt, not just the return value.

    prune_traces() swallows any failure and also returns 0 for that case, so
    a return-value-only assertion can't tell "the POSTGRES_HOST gate
    correctly short-circuited before pg_cursor opened a connection" apart
    from "the gate broke, a connection was attempted and failed, and the
    failure got swallowed" — the second is exactly the regression this test
    exists to catch. Recording the attempt in a list (rather than asserting
    inside the patched get_conn) is required, not stylistic: prune_traces's
    own `except Exception: return 0` would otherwise swallow an assertion
    raised from inside get_conn too, same as any other exception, and this
    test would pass either way.
    """
    monkeypatch.delenv("POSTGRES_HOST", raising=False)
    monkeypatch.delenv("SE_TRACE_RETENTION_DAYS", raising=False)

    from shared.postgres import client as pg_client

    connection_attempts = []

    def _record_and_fail(*args, **kwargs):
        connection_attempts.append((args, kwargs))
        raise RuntimeError("prune_traces must not reach get_conn without POSTGRES_HOST")

    monkeypatch.setattr(pg_client, "get_conn", _record_and_fail)

    assert trace_store.prune_traces(0) == 0
    assert trace_store.prune_traces(30) == 0
    assert trace_store.prune_traces() == 0  # uses SE_TRACE_RETENTION_DAYS default
    assert connection_attempts == [], "prune_traces attempted a DB connection without POSTGRES_HOST"


# --- trace_store cache-token persistence (single-row + batch, no live Postgres) --------


# _FakeCursor / _FakeCursorContractViolation now live in
# _observability_test_doubles.py (shared with the rollup wrapper's SQL tests);
# this file uses them via install_fake_cursor.
@pytest.fixture
def _fake_cursor(monkeypatch):
    """Enable tracing and swap trace_store.pg_cursor for a recording FakeCursor.

    Preconditions:
        ``monkeypatch`` is the pytest fixture — the substitution it installs is
        undone automatically at test teardown.
    Postconditions:
        ``SE_TRACE_TO_POSTGRES`` is set for the duration of the test (the write
        path's own gate; the read path does not consult it). Returns a factory —
        see :func:`_observability_test_doubles.install_fake_cursor` for the
        ``raise_on_execute``/``rows`` arguments it forwards. Each call installs a
        fresh cursor and re-patches ``trace_store.pg_cursor`` to yield it.
    """
    monkeypatch.setenv("SE_TRACE_TO_POSTGRES", "true")

    def _make(raise_on_execute: bool = False, rows=None) -> _FakeCursor:
        return install_fake_cursor(
            monkeypatch, trace_store, raise_on_execute=raise_on_execute, rows=rows
        )

    return _make


def _insert_columns() -> list[str]:
    """The column names of ``_INSERT_SQL``, in statement order.

    Preconditions:
        ``_INSERT_SQL`` is a single ``INSERT INTO <table> (<cols>) VALUES (...)``
        statement whose first parenthesised group is the column list.
    Postconditions:
        Returns the column names, stripped, in the order the statement declares
        them — the order Postgres binds positional params to.
    """
    columns = trace_store._INSERT_SQL.split("(", 1)[1].split(")", 1)[0]
    return [c.strip() for c in columns.split(",")]


def test_insert_sql_placeholder_count_matches_row_width() -> None:
    """``_INSERT_SQL``'s ``%s`` count must equal the tuple width ``_record_to_row`` builds.

    Adding a column to the statement without a matching value (or vice versa) makes
    every real INSERT fail — and because both write paths swallow exceptions, that
    failure is invisible: trace rows are silently dropped and the cost endpoint
    reports zero. This pins the two halves of the contract against each other
    directly, so drift fails here with a legible message rather than as a
    swallowed error inside a write-path test.
    """
    assert trace_store._INSERT_SQL.count("%s") == len(trace_store._record_to_row(_Rec()))


def test_insert_sql_pins_cache_column_positions() -> None:
    """The cache columns must sit at the positions the value assertions index.

    Every other test reads cache tokens by *position* (``_CACHE_READ_IDX`` /
    ``_CACHE_CREATION_IDX``, via :func:`_cache_tokens`) and checks only that the
    column names appear somewhere in the statement. That pair of assertions cannot
    see a reordering: swapping ``cache_read_tokens`` and ``cache_creation_tokens``
    in the column list leaves the params where they are, so Postgres would write
    each value into the other column while the value assertions stay green. This
    test is the sole tie between those index constants and the statement's own
    column list — it is what makes reading by index safe everywhere else.
    """
    columns = _insert_columns()
    assert len(columns) == trace_store._INSERT_SQL.count("%s")
    assert columns[_CACHE_READ_IDX] == "cache_read_tokens"
    assert columns[_CACHE_CREATION_IDX] == "cache_creation_tokens"


def test_trace_call_record_fields_reach_record_to_row() -> None:
    """Every _FIELDS entry the shared test double whitelists is actually read
    by _record_to_row, at the position _record_to_row's own docstring says it
    occupies.

    _FIELDS guards one drift direction already (an override for a name
    _record_to_row never reads raises AttributeError at construction time).
    This guards the other: a field _record_to_row stops reading — dropped,
    renamed, or reordered — leaves _FIELDS still silently accepting overrides
    for it, which then vanish into an unused attribute with nothing else in
    this suite noticing, since every other trace test uses realistic (often
    zero/default-shaped) values rather than values chosen to be distinguishable
    by position.
    """
    overrides = {
        "team": "sentinel-team",
        "agent_key": "sentinel-agent_key",
        "job_id": "sentinel-job_id",
        "task_id": "sentinel-task_id",
        "phase": "sentinel-phase",
        "model": "sentinel-model",
        "prompt_tokens": 101,
        "completion_tokens": 102,
        "total_tokens": 103,
        "cache_read_tokens": 104,
        "cache_creation_tokens": 105,
        "cost_usd": 1.5,
        "latency_ms": 106,
        "status": "sentinel-status",
        "outcome": "sentinel-outcome",
        "objective": "sentinel-objective",
        "request_id": "sentinel-request_id",
    }
    # timestamp is deliberately excluded and checked separately below:
    # _record_to_row derives a UTC datetime from it rather than passing it
    # through unchanged, so it isn't a same-value round trip like every other
    # field here. If this assertion fails, _FIELDS gained or lost a field
    # that `overrides` (and the row comparison below) needs updating for too.
    assert set(overrides) | {"timestamp"} == _FIELDS

    row = trace_store._record_to_row(_Rec(**overrides))

    # Position order per _record_to_row's own docstring / _INSERT_SQL's
    # column list; row[0] (ts) is skipped since timestamp isn't a passthrough.
    assert row[1:] == (
        overrides["team"],
        overrides["agent_key"],
        overrides["job_id"],
        overrides["task_id"],
        overrides["phase"],
        overrides["model"],
        overrides["prompt_tokens"],
        overrides["completion_tokens"],
        overrides["total_tokens"],
        overrides["cache_read_tokens"],
        overrides["cache_creation_tokens"],
        overrides["cost_usd"],
        overrides["latency_ms"],
        overrides["status"],
        overrides["outcome"],
        overrides["objective"],
        overrides["request_id"],
    )


def test_trace_call_record_rejects_unknown_override() -> None:
    """An override key that isn't in _FIELDS raises AttributeError with the
    documented message, rather than silently creating an unused attribute or
    raising some other exception type — the behavior the class docstring's
    Preconditions promise but that no other test in this suite exercises,
    since every other _Rec(...) call here passes only valid overrides."""
    with pytest.raises(AttributeError, match="Unknown TraceCallRecord attribute: 'prompt_toknes'"):
        _Rec(prompt_toknes=1)  # deliberately misspelled

    # A valid override, including an optional cache field, still applies normally.
    rec = _Rec(cache_read_tokens=7)
    assert rec.cache_read_tokens == 7


def test_write_trace_persists_cache_read_tokens(_fake_cursor) -> None:
    """write_trace (single-row path) carries cache_read_tokens through to the INSERT params."""
    cursor = _fake_cursor()
    assert trace_store.write_trace(_Rec(cache_read_tokens=42, cache_creation_tokens=0)) is True
    sql, params = cursor.executed[0]
    assert "cache_read_tokens" in sql
    assert "cache_creation_tokens" in sql
    assert _cache_tokens(params) == (42, 0)


def test_write_trace_persists_cache_creation_tokens(_fake_cursor) -> None:
    """write_trace (single-row path) carries cache_creation_tokens through to the INSERT params."""
    cursor = _fake_cursor()
    assert trace_store.write_trace(_Rec(cache_read_tokens=0, cache_creation_tokens=17)) is True
    sql, params = cursor.executed[0]
    assert "cache_creation_tokens" in sql
    assert _cache_tokens(params) == (0, 17)


def test_write_trace_writes_zero_for_no_cache_usage(_fake_cursor) -> None:
    """A record reporting neither cache reads nor creation writes 0 for both, never NULL."""
    cursor = _fake_cursor()
    assert trace_store.write_trace(_Rec(cache_read_tokens=0, cache_creation_tokens=0)) is True
    _, params = cursor.executed[0]
    assert _cache_tokens(params) == (0, 0)


def test_write_trace_never_raises_on_missing_cache_fields(_fake_cursor) -> None:
    """The never-raise contract holds even when the record has no cache attrs at all."""
    cursor = _fake_cursor()
    assert trace_store.write_trace(_Rec()) is True
    _, params = cursor.executed[0]
    assert _cache_tokens(params) == (0, 0)


def test_write_rows_persists_cache_read_tokens_batch(_fake_cursor) -> None:
    """write_rows (batch path) carries cache_read_tokens through identically to write_trace."""
    cursor = _fake_cursor()
    row = trace_store._record_to_row(_Rec(cache_read_tokens=42, cache_creation_tokens=0))
    assert trace_store.write_rows([row]) == 1
    sql, rows = cursor.executed[0]
    assert "cache_read_tokens" in sql
    assert "cache_creation_tokens" in sql
    assert _cache_tokens(rows[0]) == (42, 0)


def test_write_rows_persists_cache_creation_tokens_batch(_fake_cursor) -> None:
    """write_rows (batch path) carries cache_creation_tokens through identically to write_trace."""
    cursor = _fake_cursor()
    row = trace_store._record_to_row(_Rec(cache_read_tokens=0, cache_creation_tokens=17))
    assert trace_store.write_rows([row]) == 1
    sql, rows = cursor.executed[0]
    assert "cache_creation_tokens" in sql
    assert _cache_tokens(rows[0]) == (0, 17)


def test_write_rows_writes_zero_for_no_cache_usage_batch(_fake_cursor) -> None:
    """write_rows (batch path) writes 0/0 for a record reporting neither, never NULL."""
    cursor = _fake_cursor()
    row = trace_store._record_to_row(_Rec())
    assert trace_store.write_rows([row]) == 1
    _, rows = cursor.executed[0]
    assert _cache_tokens(rows[0]) == (0, 0)


def test_write_trace_never_raises_on_cursor_failure(_fake_cursor) -> None:
    """A DB failure on the single-row path degrades to False, never raises.

    The return value is the whole contract here: dropping ``write_trace``'s
    ``except Exception`` guard lets the cursor's error propagate, which fails this
    test outright, and a write that somehow succeeded would return True. (Real
    atomicity is Postgres's, not something this in-memory double can attest to.)
    """
    _fake_cursor(raise_on_execute=True)
    assert trace_store.write_trace(_Rec(cache_read_tokens=5, cache_creation_tokens=0)) is False


def test_write_rows_never_raises_on_cursor_failure(_fake_cursor) -> None:
    """A DB failure on the batch path degrades to 0, never raises (mirrors write_trace)."""
    _fake_cursor(raise_on_execute=True)
    row = trace_store._record_to_row(_Rec(cache_read_tokens=5, cache_creation_tokens=0))
    assert trace_store.write_rows([row]) == 0
