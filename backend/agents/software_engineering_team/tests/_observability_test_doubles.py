"""Shared test doubles for the SE observability tests (se_events/trace_store/rollup).

Extracted from ``test_trace_flusher.py`` and ``test_observability_stores.py``,
which each defined field-for-field identical stand-ins for
:class:`llm_service.telemetry.LLMCallRecord` — the only thing the two modules'
copies disagreed on was default values, not shape. Matches the extraction
pattern already used by ``_coding_team_orchestrator_doubles.py`` and
``_review_fallback_test_doubles.py``.

Also holds ``_FakeCursor`` — a recording, ``fetchall``-capable stand-in for a
psycopg cursor, moved here from ``test_observability_stores.py`` when the
agent/phase rollup's Postgres-reading wrapper needed a fake that could return
queued rows, not just record writes. Converging it into this module (rather
than hand-rolling a third copy) keeps this package down to one ``pg_cursor``
fake used by every SE observability test file. A drifted near-duplicate still
lives in ``llm_service/tests/test_usage_store.py`` — a different team's
package, so importing it here would reach into another team's private
``tests/`` — and that convergence is left for later. Note this is a distinct
seam from the ``get_conn``-shaped ``_FakeCursor`` classes hand-rolled in
``test_coding_team_resolve_attempt_store_offline.py`` and
``test_coding_team_review_history_store_offline.py``; those patch a different
entry point and are not part of this convergence.

Not a test module itself -- its ``_``-prefixed name prevents pytest from
collecting it (same convention as those two modules).

Further, a deliberately unconverged copy of the ``TraceCallRecord`` field set
exists in this package's own live-Postgres integration sibling
(``test_observability_stores_pg.py``, run only under ``-m integration``).
Converging that one is a larger change than this extraction and is left for
later rather than folded in here.
"""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, Optional, Sequence

# The full field set _record_to_row reads, including cache_read_tokens/
# cache_creation_tokens — deliberately NOT pre-set by __init__ (see its
# Postconditions), so validation below whitelists by name rather than by
# hasattr(), which would reject exactly the override that field exists for.
_FIELDS = frozenset(
    {
        "timestamp",
        "team",
        "agent_key",
        "job_id",
        "task_id",
        "phase",
        "model",
        "prompt_tokens",
        "completion_tokens",
        "total_tokens",
        "cost_usd",
        "latency_ms",
        "status",
        "outcome",
        "objective",
        "request_id",
        "cache_read_tokens",
        "cache_creation_tokens",
    }
)


class _FakeCursorContractViolation(BaseException):
    """Raised when a statement/row pair would be rejected by real psycopg.

    Deliberately derives from ``BaseException``, not ``Exception``: both write
    paths wrap their cursor work in ``except Exception`` (DEBUG log, no raise),
    so an ``Exception`` raised by the fake would be swallowed by the code under
    test and resurface as an opaque ``IndexError`` on ``cursor.executed[0]``.
    Deriving from ``BaseException`` lets the violation propagate to pytest with
    its own message intact.
    """


class _FakeCursor:
    """Records every execute/executemany call and serves queued fetchall/fetchone rows.

    No live Postgres involved. Mirrors psycopg's arity check: a statement whose
    ``%s`` count does not match the row width is a hard error, not a
    silently-recorded call. Without this the fake would happily accept a row
    that real psycopg rejects, and since the write paths swallow exceptions
    (DEBUG log, no raise) the drift would surface only as silently-dropped
    trace rows in production.

    A single fake serves both the write-path tests (``execute``/``executemany``
    recording, optional ``raise_on_execute``) and the read-path tests
    (``fetchall``/``fetchone`` over rows queued at construction) — the two
    write paths (``trace_store.write_trace``/``write_rows``) and the rollup's
    read path (``trace_store.fetch_traces_since``) all go through the same
    ``with pg_cursor(...) as cur:`` shape, so one fake covers all three.
    """

    def __init__(
        self, raise_on_execute: bool = False, rows: Optional[Sequence[Any]] = None
    ) -> None:
        """Construct a recording cursor, optionally pre-loaded with result rows.

        Preconditions:
            ``rows``, if given, is a sequence of dict-like rows — the shape
            ``dict_rows=True`` callers expect back from ``fetchall``.
        Postconditions:
            ``self.executed`` is an empty list. ``self._raise`` is
            ``raise_on_execute`` — when true, every subsequent ``execute``/
            ``executemany`` call raises ``RuntimeError`` instead of recording.
            ``self._rows`` is ``list(rows)`` if given, else ``[]`` — the values
            ``fetchall``/``fetchone`` serve; queuing no rows is the "empty
            result set" case, not a distinct code path.
        """
        self.executed: list[tuple] = []
        self._raise = raise_on_execute
        self._rows: list[Any] = list(rows) if rows is not None else []

    @staticmethod
    def _check_arity(sql: str, params, expected: int) -> None:
        """Reject a ``params``/row whose length does not match ``sql``'s own ``%s`` count.

        Preconditions:
            ``expected`` is ``sql.count("%s")``, computed once by the caller — passed
            in rather than recomputed here so a caller iterating many rows against
            the same ``sql`` (``executemany``) does the ``str.count`` scan once.
        Postconditions:
            Returns ``None`` when ``len(params or ())`` equals ``expected``.
        Raises:
            ``_FakeCursorContractViolation`` — deliberately a ``BaseException``, not an
            ``Exception`` — when the lengths differ, naming both counts.
        """
        actual = len(params or ())
        if expected != actual:
            raise _FakeCursorContractViolation(f"SQL expects {expected} params, row has {actual}")

    def execute(self, sql: str, params=None) -> None:
        """Record one ``(sql, params)`` call, or raise if ``raise_on_execute`` is set.

        Preconditions:
            ``params`` is ``None`` or a sequence whose length matches ``sql``'s ``%s``
            placeholder count.
        Postconditions:
            When not configured to raise, appends ``(sql, params)`` to ``self.executed``
            and returns ``None``.
        Raises:
            ``RuntimeError`` when ``raise_on_execute`` was set at construction, before
            any arity check or recording. ``_FakeCursorContractViolation`` when
            ``params``'s length does not match ``sql``'s placeholder count.
        """
        if self._raise:
            raise RuntimeError("boom")
        self._check_arity(sql, params, sql.count("%s"))
        self.executed.append((sql, params))

    def executemany(self, sql: str, seq) -> None:
        """Record one ``(sql, rows)`` call for a batch, or raise if ``raise_on_execute`` is set.

        Preconditions:
            Every row in ``seq`` is a sequence whose length matches ``sql``'s ``%s``
            placeholder count.
        Postconditions:
            When not configured to raise, appends ``(sql, list(seq))`` to
            ``self.executed`` and returns ``None``. ``sql.count("%s")`` is computed
            once and reused across every row in ``seq``, since it is invariant for a
            single call.
        Raises:
            ``RuntimeError`` when ``raise_on_execute`` was set at construction, before
            any row is checked or recorded. ``_FakeCursorContractViolation`` on the
            first row whose length does not match ``sql``'s placeholder count.
        """
        if self._raise:
            raise RuntimeError("boom")
        expected = sql.count("%s")
        rows = list(seq)
        for row in rows:
            self._check_arity(sql, row, expected)
        self.executed.append((sql, rows))

    @staticmethod
    def _copy_row(row: Any) -> Any:
        """Return a shallow copy of a queued row, or ``row`` unchanged if it isn't a dict.

        Preconditions:
            None.
        Postconditions:
            Returns ``dict(row)`` when ``row`` is a dict (every ``dict_rows=True``
            caller's shape); returns ``row`` itself otherwise (e.g. a tuple, already
            immutable). Shared by ``fetchall``/``fetchone`` so a caller mutating a
            returned row can never corrupt ``self._rows`` or a later fetch on the
            same cursor — the two methods stay symmetric rather than one copying
            and the other handing out a live reference.
        """
        return dict(row) if isinstance(row, dict) else row

    def fetchall(self) -> list[Any]:
        """Return the rows queued at construction.

        Preconditions:
            None.
        Postconditions:
            Returns a fresh list of :func:`_copy_row` copies — mutating an entry
            in the returned list, or the list itself, cannot corrupt ``self._rows``
            or what a later ``fetchall``/``fetchone`` call on the same cursor
            would return.
        """
        return [self._copy_row(row) for row in self._rows]

    def fetchone(self) -> Optional[Any]:
        """Return a copy of the first queued row, or ``None`` when none were queued.

        Preconditions:
            None.
        Postconditions:
            Returns :func:`_copy_row` of ``self._rows[0]`` if ``self._rows`` is
            non-empty, else ``None`` — mutating the returned row cannot corrupt
            ``self._rows`` or what a later ``fetchall``/``fetchone`` call would
            return, matching ``fetchall``'s own guarantee.
        """
        return self._copy_row(self._rows[0]) if self._rows else None


def install_fake_cursor(
    monkeypatch,
    module: Any,
    *,
    raise_on_execute: bool = False,
    rows: Optional[Sequence[Any]] = None,
) -> _FakeCursor:
    """Patch ``module.pg_cursor`` to yield a fresh :class:`_FakeCursor`; returns it.

    The one installation routine every SE observability test file uses to
    substitute ``shared.postgres.pg_cursor`` — write-path tests read
    ``cursor.executed`` afterward, read-path tests pass ``rows`` up front for
    ``cursor.fetchall()``/``fetchone()`` to serve back.

    Preconditions:
        ``monkeypatch`` is the pytest fixture — the substitution it installs is
        undone automatically at test teardown. ``module`` is the module object
        whose ``pg_cursor`` name should be patched (e.g. ``trace_store``) — it
        must expose a module-level ``pg_cursor`` imported the way the real
        ``shared.postgres.pg_cursor`` is.
    Postconditions:
        ``module.pg_cursor`` is patched to a context manager matching the real
        ``pg_cursor(*, dict_rows: bool = False, database=None)`` signature that
        yields a fresh :class:`_FakeCursor` constructed with ``raise_on_execute``
        and ``rows``. Returns that cursor so the caller can assert against
        ``cursor.executed`` or pass a different ``rows`` queue on a later call —
        each call installs an independent cursor; they never share call history.
    """
    cursor = _FakeCursor(raise_on_execute=raise_on_execute, rows=rows)

    @contextmanager
    def _pg_cursor(*, dict_rows: bool = False, database=None):
        """Stand-in for ``shared.postgres.pg_cursor``; yields the fake cursor.

        Preconditions:
            Signature must track the real ``pg_cursor`` — a keyword-only
            ``dict_rows`` and ``database``, both with matching defaults — so
            this fake stays a valid substitute if the real one's callers change
            how they invoke it.
        Postconditions:
            Yields ``cursor`` unconditionally; both parameters are accepted but
            unused, since the fake never distinguishes row-factory mode.
        """
        yield cursor

    monkeypatch.setattr(module, "pg_cursor", _pg_cursor)
    return cursor


class TraceCallRecord:
    """Minimal stand-in for :class:`llm_service.telemetry.LLMCallRecord`.

    Preconditions:
        Every key in ``overrides`` is one of :data:`_FIELDS` — an unknown key
        raises ``AttributeError`` rather than silently creating an unused
        attribute (a misspelled override would otherwise surface only as a
        confusing downstream assertion diff).
    Postconditions:
        The constructed instance exposes every field
        :func:`trace_store._record_to_row` reads *except*
        ``cache_read_tokens``/``cache_creation_tokens``, with any
        ``overrides`` values applied on top of those defaults. The two cache
        fields are set only when passed as an override — a bare
        ``TraceCallRecord()`` has no cache-token attributes at all (not
        merely zero), which is what lets it double as the "cache fields
        missing entirely" case without a separate stub.
    """

    def __init__(self, **overrides: Any) -> None:
        self.timestamp = datetime.now(tz=timezone.utc).timestamp()
        self.team = "software_engineering"
        self.agent_key = "backend"
        self.job_id = "j1"
        self.task_id = "t1"
        self.phase = "execution"
        self.model = "m"
        self.prompt_tokens = 10
        self.completion_tokens = 5
        self.total_tokens = 15
        self.cost_usd = 0.01
        self.latency_ms = 100
        self.status = "success"
        self.outcome = "success"
        self.objective = "o"
        self.request_id = "r1"
        for k, v in overrides.items():
            if k not in _FIELDS:
                raise AttributeError(f"Unknown TraceCallRecord attribute: {k!r}")
            setattr(self, k, v)


__all__ = ["TraceCallRecord", "_FakeCursor", "_FakeCursorContractViolation", "install_fake_cursor"]
