"""Shared predicate evaluator for engine runtime and alignment audit.

Promotes the deterministic predicate-evaluation logic from the alignment
gate (``alignment_checks.py``) into a reusable module behind a
``HistoryView`` protocol.  Both the engine's per-bar entry/exit
dispatchers and the post-hoc alignment audit consume these functions,
guaranteeing identical evaluation semantics.

The module is intentionally side-effect-free: every function takes an
immutable view of market data and returns a pure result.
"""

from __future__ import annotations

import math
from collections import deque
from dataclasses import dataclass
from typing import Any, Dict, Literal, Optional, Protocol, Sequence, Tuple

import pandas as pd

from ..indicators.streaming import IndicatorRegistry, resolve_indicator
from ..runtime_window import STREAMING_WINDOW_BARS
from ..spec_dsl import (
    AllOf,
    AnyOf,
    EntryRule,
    IndicatorRef,
    Predicate,
    PredicateTree,
    SignalExitRule,
)

# ---------------------------------------------------------------------------
# HistoryView protocol
# ---------------------------------------------------------------------------

_PRICE_REF_FIELDS: dict[str, str] = {
    "bar.close": "close",
    "bar.high": "high",
    "bar.low": "low",
    "bar.volume": "volume",
}


class HistoryView(Protocol):
    """Read-only view over a symbol's bar history + indicator values."""

    def length(self) -> int: ...
    def bar_field(self, field_name: str, i: int) -> float: ...
    def indicator(self, ref: IndicatorRef, i: int) -> Optional[float]: ...


# ---------------------------------------------------------------------------
# EvaluationResult
# ---------------------------------------------------------------------------

EvalStatus = Literal["satisfied", "miss", "warmup"]


@dataclass(frozen=True)
class EvaluationResult:
    status: EvalStatus
    lhs: Optional[float] = None
    rhs: Optional[float] = None
    rel_miss: Optional[float] = None


# ---------------------------------------------------------------------------
# Pure evaluation functions
# ---------------------------------------------------------------------------


def resolve_side_value(
    side: Any,
    view: HistoryView,
    i: int,
) -> Optional[float]:
    """Resolve one side of a predicate to a scalar at bar index ``i``.

    Pre: ``i`` is in ``[0, view.length())``.
    Post: returns ``None`` when the indicator value is NaN (warmup);
    ``float`` literals and bar-field references are always resolvable.
    """
    if isinstance(side, IndicatorRef):
        return view.indicator(side, i)
    if isinstance(side, str):
        col = _PRICE_REF_FIELDS.get(side)
        if col is None:
            raise ValueError(f"unexpected bar-ref string: {side!r}")
        return view.bar_field(col, i)
    if isinstance(side, (int, float)) and not isinstance(side, bool):
        return float(side)
    raise TypeError(f"unsupported predicate side type: {type(side).__name__}")


def compare(
    op: str,
    lhs: float,
    rhs: float,
    *,
    prev_lhs: Optional[float] = None,
    prev_rhs: Optional[float] = None,
) -> bool:
    """Evaluate a comparison op on two scalars.

    ``cross_above`` / ``cross_below`` require previous-bar values;
    returns ``False`` (fail-closed) when they are unavailable.
    """
    if op == "<":
        return lhs < rhs
    if op == "<=":
        return lhs <= rhs
    if op == ">":
        return lhs > rhs
    if op == ">=":
        return lhs >= rhs
    if op == "==":
        return math.isclose(lhs, rhs, rel_tol=1e-9, abs_tol=1e-12)
    if op == "cross_above":
        if prev_lhs is None or prev_rhs is None:
            return False
        return prev_lhs <= prev_rhs and lhs > rhs
    if op == "cross_below":
        if prev_lhs is None or prev_rhs is None:
            return False
        return prev_lhs >= prev_rhs and lhs < rhs
    raise ValueError(f"unknown comparison op: {op!r}")


def relative_miss(computed: float, threshold: float) -> float:
    """``|computed - threshold| / max(|threshold|, |computed|, 1e-12)``."""
    denom = max(abs(threshold), abs(computed), 1e-12)
    return abs(computed - threshold) / denom


def evaluate_predicate(
    pred: Predicate,
    view: HistoryView,
    i: int,
) -> EvaluationResult:
    """Evaluate a single predicate at bar index ``i``.

    Pre: ``i`` is in ``[0, view.length())``.
    Post: ``status`` is ``"satisfied"`` when the predicate is true,
    ``"miss"`` when it is false, or ``"warmup"`` when an indicator
    returns ``None`` (insufficient history).
    """
    try:
        lhs_val = resolve_side_value(pred.lhs, view, i)
        rhs_val = resolve_side_value(pred.rhs, view, i)
    except (ValueError, TypeError):
        return EvaluationResult(status="warmup")

    if lhs_val is None or rhs_val is None:
        return EvaluationResult(status="warmup")

    prev_lhs: Optional[float] = None
    prev_rhs: Optional[float] = None
    if pred.op in ("cross_above", "cross_below") and i > 0:
        try:
            prev_lhs = resolve_side_value(pred.lhs, view, i - 1)
            prev_rhs = resolve_side_value(pred.rhs, view, i - 1)
        except (ValueError, TypeError):
            pass

    is_cross = pred.op in ("cross_above", "cross_below")
    if is_cross and (prev_lhs is None or prev_rhs is None) and i == 0:
        return EvaluationResult(status="warmup", lhs=lhs_val, rhs=rhs_val)

    satisfied = compare(
        pred.op,
        lhs_val,
        rhs_val,
        prev_lhs=prev_lhs,
        prev_rhs=prev_rhs,
    )
    if satisfied:
        return EvaluationResult(status="satisfied", lhs=lhs_val, rhs=rhs_val, rel_miss=0.0)

    rm = None if is_cross else relative_miss(lhs_val, rhs_val)
    return EvaluationResult(status="miss", lhs=lhs_val, rhs=rhs_val, rel_miss=rm)


def evaluate_tree(node: PredicateTree, view: HistoryView, i: int) -> EvaluationResult:
    """Evaluate a predicate tree (leaf ``Predicate`` or ``all_of`` / ``any_of``).

    Pre: ``node`` is a ``Predicate`` / ``AllOf`` / ``AnyOf``; ``i`` is in
    ``[0, view.length())``.
    Post: ``status`` is ``"satisfied"`` / ``"miss"`` / ``"warmup"`` with the
    boolean semantics below. ``lhs`` / ``rhs`` / ``rel_miss`` are populated only
    for a leaf predicate (a combinator has no single pair of scalars), so a
    composite node returns them as ``None``.

      * ``all_of`` — ``satisfied`` iff every child is satisfied; ``miss`` as
        soon as any child misses (the conjunction is then definitively false);
        otherwise ``warmup`` (a child is still warming up, so the verdict is
        not yet decidable).
      * ``any_of`` — ``satisfied`` as soon as any child is satisfied;
        ``warmup`` if no child is satisfied but one is still warming up;
        otherwise ``miss`` (every child is a definite miss).

    Warmup propagation is the load-bearing subtlety: an AND with one warming-up
    leg must NOT read as a miss, or the engine would enter/exit a bar early.
    """
    if isinstance(node, Predicate):
        return evaluate_predicate(node, view, i)
    if isinstance(node, (AllOf, AnyOf)) and len(node.of) < 2:
        # The DSL's ``Field(min_length=2)`` forbids this on the validated path;
        # guard the ``model_construct`` / mutation path so a malformed tree fails
        # fast. An empty tree would otherwise return a vacuous satisfied (AND) /
        # miss (OR); a 1-child tree would silently violate the ≥2-children
        # canonical-shape invariant in the AllOf/AnyOf docstrings.
        raise ValueError(f"{type(node).__name__}.of must have at least 2 children")
    if isinstance(node, AllOf):
        saw_warmup = False
        for child in node.of:
            res = evaluate_tree(child, view, i)
            if res.status == "miss":
                return EvaluationResult(status="miss")
            if res.status == "warmup":
                saw_warmup = True
        return EvaluationResult(status="warmup" if saw_warmup else "satisfied")
    if isinstance(node, AnyOf):
        saw_warmup = False
        for child in node.of:
            res = evaluate_tree(child, view, i)
            if res.status == "satisfied":
                return EvaluationResult(status="satisfied")
            if res.status == "warmup":
                saw_warmup = True
        return EvaluationResult(status="warmup" if saw_warmup else "miss")
    raise TypeError(f"unsupported predicate-tree node: {type(node).__name__}")


def evaluate_entry_rules(
    rules: Sequence[EntryRule],
    view: HistoryView,
    i: int,
    *,
    side_filter: Optional[str] = None,
) -> Optional[Tuple[EntryRule, int]]:
    """Return the first entry rule whose predicate tree fires at bar ``i``.

    Pre: ``rules`` is the spec's ``entry_rules`` list.
    Post: returns ``(rule, original_index)`` or ``None``.

    Entry-rule priority semantics (authoritative decision):
        ``rules`` list order IS the spec's priority order — rule 0 has the
        highest priority, rule 1 the next, and so on. First-match-wins
        (returning the first ``satisfied`` rule and ignoring the rest) is
        **intentional**, not an incidental consequence of this loop's
        implementation: it is the one disambiguation model authors are taught
        (see ``prompts/design_system.md``, "Hypothesis <-> predicates must
        agree") and it costs no evaluator complexity or determinism risk. The
        defect this decision fixes is not the semantics themselves but their
        former silence — a later rule shadowed by an earlier one produced no
        signal that it was structurally unreachable.

        ``side_filter`` is optional, and the current sole caller
        (``executor/reference_entries.py``'s ``replay_entries``) does not pass
        it — so in practice priority is applied across ALL rules regardless of
        ``side``: a ``long`` rule listed before a ``short`` rule shadows it on
        any bar where both fire. A caller that does pass ``side_filter`` only
        ever sees rules of that one side, so priority is scoped to that subset
        for that call.

    Structurally starved (the concept the reachability probe detects,
    distinct from "dead"):
        Fix a spec + fetched market-data window (the same data-dependent frame
        :class:`~..quality_gates.predicate_reachability.PredicateReachabilityProbe`
        already evaluates against). For entry-rule index ``j``, let ``S_j`` be
        the set of (symbol, bar) pairs where rule ``j``'s predicate tree
        (``evaluate_tree(rule.when, view, i)``) evaluates to ``"satisfied"``.
        Membership keys on ``"satisfied"`` and nothing else, because that is
        the only status the loop below acts on: a rule still warming up at bar
        ``i`` is simply not in ``S_i``, exactly as a rule whose predicate is
        false there is not. This matters for the set relations that follow —
        the warmup prefix is not a hole in the analysis, it is a stretch of
        bars where the warming-up rules are absent from the union and the
        later rules can therefore win.

        * **dead** (existing probe concept, unchanged) — ``S_j`` is empty:
          the rule never fires under any ordering.
        * **structurally starved** (new) — ``S_j`` is non-empty, but
          ``S_j ⊆ union(S_i for i < j)``: every bar where rule ``j`` would
          fire, some earlier rule already does, so this loop can never
          actually return ``j`` — ``evaluate_entry_rules`` always picks the
          earlier rule first. Note this must be checked against the UNION of
          every earlier rule's firing set, not a pairwise superset check
          against a single earlier rule: several earlier rules can jointly
          cover ``S_j`` (fully starving rule ``j``) even when none of them
          individually is a superset of it.
        * **reachable** — ``S_j \\ union(S_i for i < j)`` is non-empty: there
          is at least one bar where rule ``j`` fires and no earlier rule does,
          so it CAN be the value this loop returns.

        Because warmup does not put a rule into ``S_i``, a rule ``j`` whose
        only members of ``S_j \\ union(S_i for i < j)`` sit in the window's
        warmup prefix is *reachable* by this definition, not starved — and it
        genuinely is: ``executor/reference_entries.py``'s ``replay_entries``
        walks every bar from index 0 and will open positions from it there.
        It is still worth flagging, because it stops contributing the moment
        the earlier rules warm up; the probe reports that case as its own
        lesser finding rather than folding it into either verdict.

        Like "dead"/"reachable", "structurally starved" is a per-dataset,
        data-dependent verdict — the same spec can be starved on one fetched
        window and reachable on another — so a probe built against this
        definition should share the existing ``_MIN_EVALUATED_BARS``
        "judged" abstention gate rather than treating starvation as a
        universal, data-independent property of the spec.
    """
    for idx, rule in enumerate(rules):
        if not isinstance(rule, EntryRule):
            continue
        if side_filter is not None and rule.side != side_filter:
            continue
        result = evaluate_tree(rule.when, view, i)
        if result.status == "satisfied":
            return rule, idx
    return None


def evaluate_signal_exit_rules(
    rules: Sequence[Any],
    view: HistoryView,
    i: int,
) -> Optional[Tuple[SignalExitRule, int]]:
    """Return the first signal-exit rule whose predicate fires at bar ``i``.

    Pre: ``rules`` is the spec's ``exit_rules`` list (may contain
    non-``SignalExitRule`` members, which are skipped).
    Post: returns ``(rule, original_index)`` or ``None``.
    """
    for idx, rule in enumerate(rules):
        if not isinstance(rule, SignalExitRule):
            continue
        result = evaluate_tree(rule.when, view, i)
        if result.status == "satisfied":
            return rule, idx
    return None


# ---------------------------------------------------------------------------
# Indicator computation (alignment audit) — registry-backed full-frame series
# ---------------------------------------------------------------------------


# Trailing-window bound for the alignment/coverage walk — the engine's retention
# ceiling, shared with ``StreamingHistoryView.max_bars``, the compiler's
# ``_VWAP_HISTORY``, and the conformance shadow context. Cumulative indicators
# (``vwap``, ``obv``) re-base to the window start, so all sites derive from the one
# ``STREAMING_WINDOW_BARS`` constant to keep validation and runtime windows identical.
_SERIES_WINDOW: int = STREAMING_WINDOW_BARS


class _FrameBar:
    """Bar adapter over one OHLCV frame row, for the registry walk below."""

    __slots__ = ("open", "high", "low", "close", "volume")

    def __init__(self, open: float, high: float, low: float, close: float, volume: float) -> None:
        self.open = open
        self.high = high
        self.low = low
        self.close = close
        self.volume = volume


def compute_indicator_series(ref: IndicatorRef, df: pd.DataFrame) -> pd.Series:
    """Full indicator series for ``ref`` on ``df`` via the streaming registry.

    Pre: ``df`` has the standard OHLCV columns; ``ref.name`` is a known DSL
    indicator name.
    Post: returns a ``pd.Series`` aligned with ``df``'s index whose element ``i``
    is the registry's trailing value over ``df[: i + 1]`` — byte-identical to
    the engine's per-bar value (``StreamingHistoryView``), so the alignment
    audit re-evaluates predicates with the same math the engine ran. ``NaN``
    during warm-up (the registry returns ``None``).

    Forward-walks one fresh ``IndicatorRegistry`` over the frame, feeding it a
    deque bounded to ``_SERIES_WINDOW`` (the same retention ceiling as the engine's
    ``StreamingHistoryView``) so it advances by a single recurrence step per row.
    The bound is what keeps the cumulative indicators (``vwap``, ``obv``) faithful:
    they re-base to the window start, so an unbounded prefix would report a
    full-history value while the runtime traded on the trailing window. Windowed
    indicators are unaffected (they only read the trailing ``period`` bars). This is
    a post-hoc, per-(ref, symbol) cached pass — not the engine hot path — so the
    walk's cost (O(window) per row) is acceptable.
    """
    n = len(df)
    if n == 0:
        return pd.Series([], dtype=float, index=df.index)

    def _col(name: str):
        return df[name].to_numpy() if name in df.columns else None

    opens, highs, lows = _col("open"), _col("high"), _col("low")
    closes, volumes = _col("close"), _col("volume")

    def _at(arr, idx: int) -> float:
        return float(arr[idx]) if arr is not None else 0.0

    # Imported here, not at module top: ``batch_cache_context`` creates a
    # module-level ``threading.Lock`` on import, which the temporalio workflow
    # sandbox forbids — and this module is transitively imported (via
    # ``rule_compiler``) when the workflow module loads in that sandbox. This
    # function only runs in an activity/executor context (never the sandbox), so
    # a local import keeps the module's top level sandbox-safe.
    from ..batch_cache_context import new_registry

    reg = new_registry()
    window: deque[_FrameBar] = deque(maxlen=_SERIES_WINDOW)
    out: list[float] = []
    for idx in range(n):
        window.append(
            _FrameBar(
                _at(opens, idx),
                _at(highs, idx),
                _at(lows, idx),
                _at(closes, idx),
                _at(volumes, idx),
            )
        )
        # ``list(window)`` mirrors ``StreamingHistoryView._ensure_bars_list``: the
        # registry needs a sliceable/indexable sequence, and the deque has already
        # dropped any bar beyond the trailing window so engine and audit agree.
        value = _registry_indicator(reg, ref, list(window))
        out.append(math.nan if value is None else value)
    return pd.Series(out, index=df.index, dtype=float)


# ---------------------------------------------------------------------------
# PandasHistoryView — wraps the alignment gate's (DataFrame, cache) pair
# ---------------------------------------------------------------------------


class PandasHistoryView:
    """``HistoryView`` backed by a pre-built DataFrame + indicator cache.

    Used by the alignment gate to evaluate predicates against full
    market-data frames. The indicator cache is populated lazily and
    shared across trades for the same symbol.
    """

    def __init__(self, df: pd.DataFrame, indicator_cache: Dict[str, pd.Series]) -> None:
        self._df = df
        self._cache = indicator_cache
        # Private per-view ``ndarray`` views for O(1) scalar reads on the
        # predicate hot path. pandas ``.iloc[i]`` scalar indexing carries heavy
        # per-call overhead (label resolution, scalar boxing); indexing a cached
        # numpy array does not, and the values are bit-identical. The shared
        # ``indicator_cache`` (``Dict[str, pd.Series]``) contract is unchanged —
        # these arrays are derived from it lazily and never replace it.
        self._col_arrays: Dict[str, Any] = {}
        self._series_arrays: Dict[str, Any] = {}

    def length(self) -> int:
        return len(self._df)

    def bar_field(self, field_name: str, i: int) -> float:
        arr = self._col_arrays.get(field_name)
        if arr is None:
            arr = self._df[field_name].to_numpy()
            self._col_arrays[field_name] = arr
        return float(arr[i])

    def indicator(self, ref: IndicatorRef, i: int) -> Optional[float]:
        key = ref.sig_id
        arr = self._series_arrays.get(key)
        if arr is None:
            series = self._cache.get(key)
            if series is None:
                series = compute_indicator_series(ref, self._df)
                self._cache[key] = series
            arr = series.to_numpy()
            self._series_arrays[key] = arr
        if i >= len(arr):
            return None
        value = arr[i]
        if value is None or (isinstance(value, float) and math.isnan(value)):
            return None
        return float(value)


# ---------------------------------------------------------------------------
# StreamingHistoryView — deque-backed, for the engine's per-bar runtime
# ---------------------------------------------------------------------------


@dataclass
class BarRecord:
    """Minimal bar representation for the streaming view."""

    timestamp: str
    open: float
    high: float
    low: float
    close: float
    volume: float
    # Per-symbol views carry ``symbol=None`` (a single bar stream), which the
    # registry's MACD symbol-slotted cache key handles as one slot. Populated
    # by the engine so the registry's multi-stream precondition holds explicitly
    # if a view is ever shared across symbols.
    symbol: Optional[str] = None


# Map each DSL ``IndicatorName`` to the registry method + the params it reads
# from the ``IndicatorRef``. Mirrors ``_INDICATOR_SERIES_DISPATCH`` but routes
# the engine's per-bar reads through the streaming ``IndicatorRegistry`` (O(1)
# amortised recurrences) instead of a full pandas ``pd.Series`` recompute.
def _registry_indicator(
    reg: IndicatorRegistry, ref: IndicatorRef, bars: Sequence[Any]
) -> Optional[float]:
    """Trailing-bar value of ``ref`` over ``bars`` via the streaming registry.

    Pre: ``ref.name`` is a known DSL indicator; ``ref.params`` has its
    defaults filled (guaranteed by ``IndicatorRef`` validation). ``bars`` is a
    list-like the registry can slice/index (``bars[-period:]``, ``bars[i]``).
    Post: the indicator's scalar value at ``bars[-1]``, or ``None`` during
    warm-up — byte-identical to a fresh ``IndicatorRegistry`` over the same
    ``bars`` (so engine and sandbox ``ctx.indicator`` agree).

    Thin wrapper over ``indicators.streaming.resolve_indicator`` — the single
    dispatch shared with ``executor.strategy_indicators.indicator_value``,
    which previously carried a second, structurally-parallel 16-way if/elif
    reaching the same registry methods.
    """
    return resolve_indicator(reg, ref.name, bars, source=ref.source, **ref.params)


class StreamingHistoryView:
    """``HistoryView`` backed by a bounded deque of bars + a streaming registry.

    Designed for the engine's per-bar loop. Each appended bar advances a
    retained :class:`IndicatorRegistry` by a single recurrence step, and the
    resulting scalar is appended to a per-``ref.sig_id`` buffer aligned 1:1 with
    the bounded bars deque. Indexed reads — ``indicator(ref, i)`` is called with
    an explicit bar index ``i``, and ``cross_above`` / ``cross_below`` read both
    ``i`` and ``i - 1`` — are served straight from that buffer, so no indicator
    is ever recomputed over the full window.

    This replaces the previous design, which rebuilt the entire pandas
    DataFrame from the deque and recomputed every indicator's full ``pd.Series``
    (``rolling`` / ``ewm``) on every appended bar — ``O(window × num_indicators)``
    pandas work per bar. The registry recurrences are O(1) amortised (MACD) or
    O(window) (the windowed indicators), independent of how many bars have
    streamed through.

    Invariants:
    * ``len(self._scalar_buffers[sig_id]) == len(self._bars)`` for every
      registered ref once synced — buffer index ``i`` holds the indicator value
      at ``self._bars[i]``. Both are ``deque(maxlen=max_bars)`` and one value is
      pushed per appended bar, so they roll over in lockstep.
    * Warm-up returns ``None`` (the registry returns ``None`` until it has
      enough history), preserving the previous NaN→``None`` boundary semantics.

    Cache identity is driven by a monotonic per-instance ``_append_counter``
    bumped on every :meth:`append`. It anchors both the lazy ``list(deque)``
    snapshot (rebuilt once per bar, shared across all refs queried that bar) and
    each buffer's ``synced`` watermark, so a ref first queried mid-stream
    backfills correctly and a sparsely-queried ref catches up without a stale
    read. The counter is never recycled within a process.

    The deque is bounded to ``max_bars`` (default :data:`STREAMING_WINDOW_BARS`,
    matching the ``StrategyContext._ingest_bar`` retention ceiling — engine and
    sandbox must compute MACD/VWAP over the same trailing window for the
    conformance gate).
    """

    def __init__(self, max_bars: int = STREAMING_WINDOW_BARS) -> None:
        self._bars: deque[BarRecord] = deque(maxlen=max_bars)
        self._max_bars = max_bars
        self._append_counter: int = 0
        # Lazy ``list(self._bars)`` snapshot — the registry needs a sliceable,
        # randomly-indexable sequence (a deque supports neither). Rebuilt once
        # per bar (keyed by the counter) and shared across every ref query on
        # that bar; bounded by ``max_bars`` so this is O(max_bars), not O(bars
        # seen), and carries none of pandas' per-call overhead.
        self._bars_list: list[BarRecord] = []
        self._bars_list_counter: Optional[int] = None
        # One registry for the view's lifetime; per-``sig_id`` scalar buffers.
        # Built via ``new_registry`` so it shares the active batch cache when one
        # is bound. This is inert today for the streaming path: the batch cache
        # is consulted (in ``resolve_indicator``) only when the trailing bar
        # carries both a non-empty ``symbol`` and a ``date`` attribute, and this
        # view is fed ``contract.Bar``-shaped bars (``timestamp``, no ``date``),
        # so consultation — and its ``reg._state.clear()``-on-hit path — is never
        # reached here. If the streaming bar type ever gains a ``date``, that
        # interaction with the long-lived per-bar streaming state must be
        # revisited. Imported locally (not at module top): ``batch_cache_context``
        # creates a module-level lock on import, which the temporalio workflow
        # sandbox that transitively loads this module forbids; this __init__ only
        # runs in an activity/executor context, never the sandbox.
        from ..batch_cache_context import new_registry

        self._registry = new_registry()
        # sig_id -> {"buf": deque[Optional[float]], "synced": int}
        self._buffers: Dict[str, Dict[str, Any]] = {}

    def append(self, bar: BarRecord) -> None:
        """Append a bar; buffers advance lazily on the next :meth:`indicator`."""
        self._bars.append(bar)
        self._append_counter += 1

    def length(self) -> int:
        return len(self._bars)

    def bar_field(self, field_name: str, i: int) -> float:
        b = self._bars[i]
        return float(getattr(b, field_name))

    def indicator(self, ref: IndicatorRef, i: int) -> Optional[float]:
        if not self._bars:
            return None
        bars_list = self._ensure_bars_list()
        st = self._buffers.get(ref.sig_id)
        if st is None:
            st = {"buf": deque(maxlen=self._max_bars), "synced": 0}
            self._buffers[ref.sig_id] = st
        self._sync_buffer(ref, st, bars_list)
        buf = st["buf"]
        if i < 0 or i >= len(buf):
            return None
        return buf[i]

    def _ensure_bars_list(self) -> list[BarRecord]:
        """Return ``list(self._bars)``, cached until the next append."""
        if self._bars_list_counter == self._append_counter:
            return self._bars_list
        self._bars_list = list(self._bars)
        self._bars_list_counter = self._append_counter
        return self._bars_list

    def _sync_buffer(
        self, ref: IndicatorRef, st: Dict[str, Any], bars_list: list[BarRecord]
    ) -> None:
        """Advance ``st['buf']`` so it holds one value per current bar.

        Pre: ``bars_list`` is the current ``list(self._bars)`` snapshot.
        Post: ``len(st['buf']) == len(bars_list)`` and ``buf[i]`` is the
        indicator value at ``bars_list[i]``; ``st['synced'] == _append_counter``.
        """
        ac = self._append_counter
        synced = st["synced"]
        if synced == ac:
            return  # already current (same-bar repeat query)
        buf = st["buf"]
        length = len(bars_list)
        base = ac - length  # absolute append-index of bars_list[0]
        reg = self._registry
        if synced < base:
            # The buffer fell behind by more than the window — the bars between
            # ``synced`` and ``base`` were evicted and can no longer be computed
            # or addressed. Rebuild over the whole currently-addressable deque.
            buf.clear()
            start = 0
        else:
            # Contiguous catch-up: only the bars appended since ``synced`` are
            # unfilled; they begin at index ``start`` in ``bars_list``.
            start = synced - base
        if start == length - 1:
            # Common engine case: exactly one new bar. Pass ``bars_list`` directly
            # so the registry sees the expand/slide step with no slice or copy.
            buf.append(_registry_indicator(reg, ref, bars_list))
        else:
            # Cold rebuild (start == 0) or a multi-bar gap. Grow a prefix list
            # (append-only, same bar objects so the registry still detects each
            # expand/slide step) rather than slicing ``bars_list[:k]`` per step —
            # the slices would total O(length^2); this is O(length) in
            # list-building plus the registry's O(window) per step.
            prefix = bars_list[:start]
            for idx in range(start, length):
                prefix.append(bars_list[idx])
                buf.append(_registry_indicator(reg, ref, prefix))
        st["synced"] = ac
