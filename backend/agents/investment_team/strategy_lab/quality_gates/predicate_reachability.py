"""Pre-backtest data-driven reachability probe.

The closed-form reachability check in :mod:`spec_readiness`
(``_check_predicate_reachability``) catches only *structural* dead code — a
bounded indicator compared against an out-of-range constant (``rsi > 100``), or
an identical-reference tautology/contradiction (``close < close``). It cannot
see *data-dependent* dead code: an ``all_of`` whose legs never co-occur on the
fetched bars, or ``sma(5) > sma(200)`` that simply never crosses in the window.

The post-backtest :class:`RuleFiringRateGate` catches some of that, but only
*after* a (doomed) backtest has run, only on the compiled path, and without
per-leg diagnostics. This probe runs *before* the backtest and evaluates each
entry rule's authored ``PredicateTree`` against the REAL fetched bars using the
exact same ``evaluate_tree`` the compiled engine uses. So on the compiled path
"zero predicate fires over the post-warmup window" provably means "zero entry
orders" — the strategy cannot generate a single trade as authored — and the
probe reports it, per-rule and per-leg, as an early authoring-time signal.

Beyond a rule that never fires at all, the probe also reports a rule that fires
plenty on its own but whose every fire lands on a bar some earlier,
higher-priority rule already covers — "structurally starved" in
``evaluate_entry_rules``' terms, a distinct finding kind from dead code because
it calls for a different fix (reorder or loosen, not delete). The verdict is
taken against the UNION of every earlier rule, and only once at least
``_MIN_STARVATION_FIRES`` covered fires have been observed; below that the probe
abstains with an ``info`` rather than mistaking a rarely-firing rule for a
starved one. A rule whose only unshadowed fires land on the warmup prefix —
where the earlier rules cannot yet fire, and where ``replay_entries`` therefore
really does select it — is reported as a ``warning`` instead, since it does
open positions and the ``critical``'s "contributes no entries" claim would be
false about it.

Path semantics:
  * Compiled path (``requires_custom_code=False``): the engine decides entries
    with this very evaluator, so an unreachable predicate is a **critical** — the
    backtest is guaranteed to be a no-op for that rule.
  * Custom path (``requires_custom_code=True``): the engine runs LLM-authored
    code that may diverge from the spec's DSL, so an unreachable authored
    predicate is a **warning** — the spec's *intent* is untestable on this data,
    but the executed code might still trade.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any, ClassVar, Dict, List, Literal, Sequence

from ..executor.predicate_evaluator import EvalStatus, PandasHistoryView, evaluate_tree
from ..spec_dsl import EntryRule, iter_leaf_predicates
from .alignment_checks import _bars_to_frame, _format_predicate
from .models import GateResultsMixin, QualityGateResult, StrategyLabPhase

GATE = "predicate_reachability_probe"

# Minimum post-warmup bars a rule must be evaluated over before "never fires" is
# read as dead code rather than an artefact of a too-short / all-warmup window.
# Below this the probe abstains (an ``info``) — a short window is a coverage
# problem the warmup / data checks own, not a reachability verdict.
_MIN_EVALUATED_BARS = 20

# Minimum COVERED fires a later rule must show before "every one of its fires is
# also covered by an earlier rule" is read as structural starvation rather than
# coincidence. Under the null hypothesis that the later rule's fires land
# independently of the earlier rules' coverage fraction ``p``, seeing all ``f``
# of them covered has probability ``p ** f``: at ``p = 0.5`` one fire is a coin
# flip and four fires is ~6%, while five is ~3% — the first count at which "all
# covered" stops being ordinary luck. Below this the probe abstains (an
# ``info``) rather than reporting a rarely-firing rule as starved: a false
# critical costs the author's trust in every later finding, a missed rare case
# costs one window.
_MIN_STARVATION_FIRES = 5


def _entry_rules(spec: Any) -> List[EntryRule]:
    """Entry rules of ``spec`` in authored order — the probe's index space.

    Preconditions: ``spec`` is a ``StrategySpec`` (or any object exposing an
    ``entry_rules`` attribute).
    Postconditions: returns every ``EntryRule`` in ``spec.entry_rules``, in
    order, skipping any non-``EntryRule`` element; ``[]`` when ``spec`` has no
    ``entry_rules`` attribute or it is falsy. Shared by :meth:`probe` and
    :meth:`probe_pairs` so both index into the SAME filtered list.
    """
    return [r for r in (getattr(spec, "entry_rules", None) or []) if isinstance(r, EntryRule)]


def _build_views(market_data: Any) -> List[PandasHistoryView]:
    """Build one PandasHistoryView per symbol with usable bars.

    Preconditions: ``market_data`` is ``Optional[Dict[str, List[OHLCVBar]]]`` or
    falsy.
    Postconditions: one view per symbol with non-empty bars and a non-empty
    frame, in ``market_data``'s iteration order; an empty list when
    ``market_data`` is falsy or every symbol's bars are empty/unusable. Pure;
    no caching across calls.
    """
    views: List[PandasHistoryView] = []
    if not market_data:
        return views
    for bars in market_data.values():
        if not bars:
            continue
        df = _bars_to_frame(bars)
        if df.empty:
            continue
        views.append(PandasHistoryView(df, {}))
    return views


@dataclass(frozen=True)
class _LegReachability:
    """Per-leaf-predicate firing tally within one entry rule."""

    predicate: str
    evaluated: int
    fires: int


@dataclass(frozen=True)
class _RuleReachability:
    """Whole-rule firing tally (and per-leg breakdown when the rule is dead)."""

    rule_index: int
    side: str
    evaluated: int
    fires: int
    legs: tuple[_LegReachability, ...]

    @property
    def judged(self) -> bool:
        """True when enough post-warmup bars exist to trust the verdict."""
        return self.evaluated >= _MIN_EVALUATED_BARS

    @property
    def dead(self) -> bool:
        """True when the rule was judged and never fired."""
        return self.judged and self.fires == 0


@dataclass(frozen=True)
class _PairLegCooccurrence:
    """Per-leaf-predicate co-occurrence tally for one leaf of the LATER rule,
    evaluated against one specific earlier rule (mirrors :class:`_LegReachability`).
    """

    predicate: str
    evaluated: int
    fires: int
    independent_fires: int


@dataclass(frozen=True)
class _PairCooccurrence:
    """Pairwise co-occurrence tally: does the later rule ever fire on a bar
    where the earlier rule doesn't, over the fetched bars.

    Invariants: ``earlier_index < later_index`` (an ordered pair; both index
    into the SAME filtered ``entry_rules`` list :meth:`PredicateReachabilityProbe.probe_pairs`
    builds, matching :meth:`PredicateReachabilityProbe.probe`'s existing indexing
    convention) — enforced in :meth:`__post_init__`.
    """

    earlier_index: int
    later_index: int
    earlier_side: str
    later_side: str
    evaluated: int
    later_fires: int
    later_independent_fires: int
    legs: tuple[_PairLegCooccurrence, ...]

    def __post_init__(self) -> None:
        """Enforce the ``earlier_index < later_index`` invariant at construction time."""
        assert self.earlier_index < self.later_index, (
            "earlier_index must be less than later_index (an ordered pair)"
        )

    @property
    def judged(self) -> bool:
        """True when enough jointly-judged bars exist to trust the verdict."""
        return self.evaluated >= _MIN_EVALUATED_BARS

    @property
    def later_dead(self) -> bool:
        """True when the later rule never fires at all against this pair's
        jointly-judged bars.

        This is the pre-existing "dead" concept (already reported by
        :meth:`PredicateReachabilityProbe.probe`/``to_gate_results``) — NOT
        this analysis's new "never independent" verdict. Kept so callers can
        tell the two apart rather than conflating them.
        """
        return self.judged and self.later_fires == 0

    @property
    def later_never_independent(self) -> bool:
        """True when the later rule fires, but only on bars the earlier rule
        also fires on.

        This pair alone would starve the later rule; the true, union-based
        "structurally starved" verdict (checked against every earlier rule at
        once, not just this one) is a later step's responsibility, not this
        analysis's.
        """
        return self.judged and self.later_fires > 0 and self.later_independent_fires == 0


_StarvationVerdict = Literal[
    "abstained_bars", "dead", "abstained_thin", "starved", "warmup_only", "reachable"
]


@dataclass(frozen=True)
class _RuleStarvation:
    """Union-based starvation verdict for ONE entry rule against every rule
    listed before it.

    This is the verdict ``evaluate_entry_rules``' docstring defines: rule ``j``
    is starved when its firing set is non-empty but contained in the UNION of
    the earlier rules' firing sets. It is strictly stronger than
    :class:`_PairCooccurrence`'s per-pair view, which can only see a single
    earlier rule at a time and therefore misses rules that several earlier
    rules jointly cover without any one of them being a superset.

    ``evaluated``/``fires``/``independent_fires``/``coverage`` are all counted
    over the SAME denominator: the bars where rule ``rule_index`` and EVERY
    rule before it are post-warmup — the steady-state window, where every
    earlier rule is actually able to compete for priority.

    :attr:`warmup_independent_fires` is the one field counted OUTSIDE that
    window, and it exists because the steady-state window alone cannot answer
    the question the finding claims to answer. ``evaluate_entry_rules`` selects
    a rule only on ``"satisfied"``, so an earlier rule that is still warming up
    does NOT win its bar — and ``replay_entries`` walks every bar from index 0,
    warmup included. A rule that fires on the warmup prefix while no earlier
    rule is satisfied therefore DOES open positions, however thoroughly the
    steady-state window shadows it. Counting those bars is what keeps
    :attr:`verdict` from claiming "contributes no entries" about a rule that
    demonstrably contributes some.

    Invariants: ``rule_index >= 1`` (rule 0 has nothing before it and can never
    be starved); ``0 <= independent_fires <= fires <= evaluated``;
    ``warmup_independent_fires >= 0``; ``coverage`` is ordered by descending
    covered-fire count then ascending rule index, and holds only earlier rules
    that covered at least one fire — enforced in :meth:`__post_init__`.
    """

    rule_index: int
    side: str
    evaluated: int
    fires: int
    independent_fires: int
    coverage: tuple[tuple[int, int], ...]
    legs: tuple[_PairLegCooccurrence, ...]
    warmup_independent_fires: int = 0

    def __post_init__(self) -> None:
        """Enforce the counting and ordering invariants at construction time."""
        assert self.rule_index >= 1, "rule_index must be >= 1 (rule 0 has no earlier rule)"
        assert 0 <= self.independent_fires <= self.fires <= self.evaluated, (
            "independent_fires <= fires <= evaluated must hold"
        )
        assert self.warmup_independent_fires >= 0, "warmup_independent_fires must be >= 0"
        assert all(count > 0 for _, count in self.coverage), (
            "coverage must hold only earlier rules that covered at least one fire"
        )
        assert all(index < self.rule_index for index, _ in self.coverage), (
            "coverage may only name rules listed before rule_index"
        )
        assert list(self.coverage) == sorted(self.coverage, key=lambda kv: (-kv[1], kv[0])), (
            "coverage must be ordered by descending covered fires, then ascending index"
        )

    @property
    def dominant_index(self) -> int:
        """Index of the earlier rule covering the most of this rule's fires.

        Preconditions: ``coverage`` is non-empty (true whenever ``fires >
        independent_fires``).
        Postconditions: returns the first entry's rule index — the coverer the
        per-leg diagnostic is computed against, so the diagnostic names the
        single earlier rule that explains most of the shadowing.
        """
        assert self.coverage, "dominant_index requires a non-empty coverage set"
        return self.coverage[0][0]

    @property
    def verdict(self) -> _StarvationVerdict:
        """Which rung of the starvation ladder this rule lands on.

        Postconditions: returns the FIRST matching rung, checked in this
        order —
          * ``"abstained_bars"`` — fewer than ``_MIN_EVALUATED_BARS`` bars
            judged against every earlier rule; a window-coverage problem, not a
            reachability verdict.
          * ``"reachable"`` — fires at least once, in the steady-state window,
            on a bar no earlier rule covers, so first-match-wins can actually
            select it.
          * ``"warmup_only"`` — never independent in the steady-state window,
            but fires on at least one warmup-prefix bar where no earlier rule
            is satisfied. ``evaluate_entry_rules`` DOES select it there, so it
            is neither starved (it contributes entries) nor plainly reachable
            (it stops contributing the moment the earlier rules warm up).
          * ``"dead"`` — never fires in the steady-state window, and never
            independently on the warmup prefix either. Already reported once,
            per rule, by :meth:`PredicateReachabilityProbe.to_gate_results`, so
            starvation reporting deliberately stays silent about it rather than
            double-reporting the same rule under two finding kinds.
          * ``"abstained_thin"`` — fires, none of them independent, but fewer
            than ``_MIN_STARVATION_FIRES`` of them: too few observations to
            separate structural starvation from a merely rarely-firing rule.
          * ``"starved"`` — fires enough times, and never on a bar that no
            earlier rule covers, on the warmup prefix or after. This is the
            reportable finding.
        ``"warmup_only"`` is checked before ``"dead"`` deliberately: a rule
        whose every fire lands on the warmup prefix has ``fires == 0`` in the
        steady-state window, yet :meth:`PredicateReachabilityProbe.probe`
        counts those same bars and reports it as firing — calling it dead here
        would contradict that and lose the finding entirely. The bottom two
        rungs are mutually exclusive with ``"reachable"``/``"warmup_only"``
        regardless of check order (both require zero independent fires of
        either kind). Deterministic; depends only on this instance's counts.
        """
        if self.evaluated < _MIN_EVALUATED_BARS:
            return "abstained_bars"
        if self.independent_fires > 0:
            return "reachable"
        if self.warmup_independent_fires > 0:
            return "warmup_only"
        if self.fires == 0:
            return "dead"
        if self.fires < _MIN_STARVATION_FIRES:
            return "abstained_thin"
        return "starved"


def _sweep(node: Any, views: List[PandasHistoryView]) -> tuple[int, int]:
    """Count ``(evaluated, fires)`` for ``node`` across every bar of every view.

    Pre: ``node`` is a ``PredicateTree`` (whole ``when`` tree or a leaf); ``views``
    are :class:`PandasHistoryView`s over each symbol's bars.
    Post: ``evaluated`` counts non-warmup bars (a warming-up leg yields ``warmup``,
    which is excluded so an all-warmup window never reads as dead code); ``fires``
    counts bars where the tree evaluated to ``satisfied``. Deterministic.

    Performance: O(symbols × bars × tree-nodes) scalar evaluations, but each
    indicator series is computed once per (symbol, indicator) and cached on the
    shared :class:`PandasHistoryView` (O(1) numpy reads thereafter) — NOT recomputed
    per bar. Callers pass the SAME views to every ``_sweep`` (whole tree and each
    leg), so the cache is warm after the first sweep and the per-bar cost is a
    small fraction of the backtest this probe precedes.

    The loop deliberately does NOT skip a computed warmup prefix: doing so would
    need each indicator's required lookback ahead of time, and that formula is
    already independently duplicated in three places in this codebase (the
    synthesis compiler, the factors compiler, and the executor registry — a
    known, separately-tracked duplication hazard). Reusing or re-deriving it here
    would add a fourth copy that can drift from the others. The bars this loop
    "wastes" evaluating are a cheap early return (``evaluate_predicate`` sees a
    ``None`` indicator value and returns ``warmup`` without doing any comparison
    work), and the probe itself is memoized per round in the orchestrator, so
    this cost is paid at most once per distinct entry-rule set — not once per
    refinement round.
    """
    assert node is not None, "node must be non-None"
    assert isinstance(views, list), "views must be a list of PandasHistoryView"
    statuses = _sweep_statuses(node, views)
    evaluated = sum(1 for s in statuses if s != "warmup")
    fires = sum(1 for s in statuses if s == "satisfied")
    return evaluated, fires


def _sweep_statuses(node: Any, views: List[PandasHistoryView]) -> List[EvalStatus]:
    """Per-bar evaluation status of ``node`` across every bar of every view.

    Preconditions: ``node`` is a ``PredicateTree`` (whole ``when`` tree or a
    leaf); ``views`` are :class:`PandasHistoryView`s over each symbol's bars.
    Postconditions: returns ``evaluate_tree(node, view, i).status`` for every
    ``(view, i)`` pair, in view-major/bar-minor order — the same length and
    order as any other call given the SAME ``views`` list, so two such calls'
    results are positionally alignable per bar. Deterministic; no I/O.
    """
    assert node is not None, "node must be non-None"
    assert isinstance(views, list), "views must be a list of PandasHistoryView"
    return [evaluate_tree(node, view, i).status for view in views for i in range(view.length())]


def _cooccurrence_counts(
    later_statuses: Sequence[EvalStatus], earlier_statuses: Sequence[EvalStatus]
) -> tuple[int, int, int]:
    """Pairwise co-occurrence tally between two same-length status sequences.

    Preconditions: ``later_statuses`` and ``earlier_statuses`` have equal
    length and are positionally aligned — both produced by ``_sweep_statuses``
    over the SAME ``views`` list, so index ``k`` names the same bar in both.
    Postconditions: pure (no I/O, no bar-walking); returns ``(evaluated,
    later_fires, later_independent_fires)``. ``evaluated`` counts bars where
    BOTH sequences are non-``"warmup"`` — the only bars where "did the earlier
    rule also fire here" is a judged fact rather than an unknowable warmup
    gap. ``later_fires`` counts the subset of those bars where
    ``later_statuses[k] == "satisfied"``. ``later_independent_fires`` counts
    the further subset where ``earlier_statuses[k] != "satisfied"`` — i.e. the
    later rule fired on a bar the earlier rule did not.
    """
    assert len(later_statuses) == len(earlier_statuses), "status sequences must be aligned"
    evaluated = 0
    later_fires = 0
    later_independent_fires = 0
    for later_status, earlier_status in zip(later_statuses, earlier_statuses):
        if later_status == "warmup" or earlier_status == "warmup":
            continue
        evaluated += 1
        if later_status == "satisfied":
            later_fires += 1
            if earlier_status != "satisfied":
                later_independent_fires += 1
    return evaluated, later_fires, later_independent_fires


def _starvation_verdicts(
    statuses: Sequence[Sequence[EvalStatus]], sides: Sequence[str]
) -> List[_RuleStarvation]:
    """Union-based starvation verdict for every rule after the first.

    Preconditions: ``statuses`` holds one per-bar status sequence per entry
    rule, in authored order, all of equal length and positionally aligned (all
    produced by ``_sweep_statuses`` over the SAME ``views`` list, so index
    ``k`` names the same bar in every sequence); ``sides`` is the matching
    ``EntryRule.side`` per rule.
    Postconditions: pure (no I/O, no bar-walking, no predicate evaluation);
    returns one :class:`_RuleStarvation` per rule index ``j >= 1``, in
    ascending ``j`` order — an empty list when there are fewer than 2 rules.
    ``evaluated``/``fires``/``independent_fires``/``coverage`` are taken over
    the steady-state window — the bars where rule ``j`` AND all rules before it
    are non-``"warmup"``: ``fires`` counts rule ``j``'s satisfied bars there,
    ``independent_fires`` the subset where NO earlier rule is satisfied (the
    bars on which first-match-wins could actually select ``j``), and
    ``coverage`` attributes each remaining fire to every earlier rule satisfied
    on it. ``warmup_independent_fires`` counts the bars OUTSIDE that window —
    rule ``j`` non-``"warmup"`` and satisfied, at least one earlier rule still
    warming up, and no earlier rule satisfied — because ``evaluate_entry_rules``
    selects on ``"satisfied"`` alone and so genuinely returns ``j`` there.
    ``legs`` is always empty here — the per-leg diagnostic needs the views this
    function deliberately does not take, and is filled in by
    :meth:`PredicateReachabilityProbe.probe_starvation`.

    The earlier-rule state is folded in one rule at a time as ``j`` advances,
    so the whole sweep is O(rules x bars) plus the per-fire attribution work,
    not O(rules^2 x bars).
    """
    rule_count = len(statuses)
    assert len(sides) == rule_count, "sides must have one entry per status sequence"
    if rule_count < 2:
        return []
    bar_count = len(statuses[0])
    assert all(len(row) == bar_count for row in statuses), "status sequences must be aligned"

    any_earlier_warmup = [False] * bar_count
    earlier_hits: List[List[int]] = [[] for _ in range(bar_count)]

    out: List[_RuleStarvation] = []
    for j in range(1, rule_count):
        for k, status in enumerate(statuses[j - 1]):
            if status == "warmup":
                any_earlier_warmup[k] = True
            elif status == "satisfied":
                earlier_hits[k].append(j - 1)

        evaluated = 0
        fires = 0
        independent_fires = 0
        warmup_independent_fires = 0
        covered: Dict[int, int] = {}
        for k, status in enumerate(statuses[j]):
            if status == "warmup":
                continue
            if any_earlier_warmup[k]:
                if status == "satisfied" and not earlier_hits[k]:
                    warmup_independent_fires += 1
                continue
            evaluated += 1
            if status != "satisfied":
                continue
            fires += 1
            hits = earlier_hits[k]
            if not hits:
                independent_fires += 1
                continue
            for index in hits:
                covered[index] = covered.get(index, 0) + 1

        out.append(
            _RuleStarvation(
                rule_index=j,
                side=sides[j],
                evaluated=evaluated,
                fires=fires,
                independent_fires=independent_fires,
                coverage=tuple(sorted(covered.items(), key=lambda kv: (-kv[1], kv[0]))),
                legs=(),
                warmup_independent_fires=warmup_independent_fires,
            )
        )
    return out


class PredicateReachabilityProbe(GateResultsMixin):
    """Evaluate each entry rule's authored predicate against the real bars.

    Invariants: deterministic; reads no state; reuses the engine's own
    ``evaluate_tree`` and indicator math so its verdict matches the compiled
    engine's entry decisions bar-for-bar.
    """

    GATE: ClassVar[str] = GATE

    def probe(self, spec: Any, market_data: Any) -> List[_RuleReachability]:
        """Reachability tally for every entry rule against ``market_data``.

        Pre: ``spec`` is a ``StrategySpec``; ``market_data`` is
        ``Optional[Dict[str, List[OHLCVBar]]]`` (the fetched bars) or falsy.
        Post: one :class:`_RuleReachability` per ``EntryRule`` (empty when there
        are no entry rules or no usable bars). The per-leg breakdown is computed
        only for a rule that never fired (the diagnostic is only needed then),
        reusing one indicator-cached view per symbol so indicators are computed
        at most once per (symbol, indicator).
        """
        assert spec is not None, "spec must be a StrategySpec"
        entry_rules = _entry_rules(spec)
        if not entry_rules or not market_data:
            return []
        views = _build_views(market_data)
        if not views:
            return []

        out: List[_RuleReachability] = []
        for idx, rule in enumerate(entry_rules):
            evaluated, fires = _sweep(rule.when, views)
            legs: tuple[_LegReachability, ...] = ()
            if fires == 0 and evaluated >= _MIN_EVALUATED_BARS:
                leaves = list(iter_leaf_predicates(rule.when))
                if len(leaves) > 1:
                    legs = tuple(
                        _LegReachability(_format_predicate(leaf), *_sweep(leaf, views))
                        for leaf in leaves
                    )
            out.append(
                _RuleReachability(
                    rule_index=idx, side=rule.side, evaluated=evaluated, fires=fires, legs=legs
                )
            )
        return out

    def probe_pairs(self, spec: Any, market_data: Any) -> List[_PairCooccurrence]:
        """Pairwise co-occurrence tally for every ordered (earlier, later)
        entry-rule pair against ``market_data``.

        Preconditions: ``spec`` is a ``StrategySpec``; ``market_data`` is
        ``Optional[Dict[str, List[OHLCVBar]]]`` (the fetched bars) or falsy.
        Postconditions: one :class:`_PairCooccurrence` per ordered pair
        ``(i, j)`` with ``i < j`` over ``spec.entry_rules`` (same ``EntryRule``
        filtering, and hence the same index space, as :meth:`probe`) — empty
        when there are fewer than 2 entry rules or no usable bars. Every rule
        pairs with every earlier rule regardless of ``side``, matching
        ``evaluate_entry_rules``'s default ``side_filter=None`` (the current
        sole caller doesn't pass it, so priority applies across long/short
        alike). Per-leg diagnostics are computed only for a pair where the
        later rule fires but never independently of that specific earlier
        rule (the diagnostic is only needed then), decomposing the LATER
        rule's own leaves — never the earlier rule's, mirroring
        ``_leg_diagnostic``'s single-rule decomposition pattern. This is a
        pure computation over already-evaluated predicate results: no
        severity, no ``QualityGateResult`` — finding emission is a separate,
        later step.

        This is the per-pair view of shadowing. Finding emission uses the
        stronger union verdict (:func:`_starvation_verdicts`, via
        :meth:`probe_starvation`), which also catches a rule that several
        earlier rules jointly cover without any one of them being a superset;
        this method stays as the directly-inspectable pairwise analysis and as
        the source of the per-leg co-occurrence tally.
        """
        assert spec is not None, "spec must be a StrategySpec"
        entry_rules = _entry_rules(spec)
        if len(entry_rules) < 2 or not market_data:
            return []
        views = _build_views(market_data)
        if not views:
            return []

        statuses = [_sweep_statuses(rule.when, views) for rule in entry_rules]
        leaves = [list(iter_leaf_predicates(rule.when)) for rule in entry_rules]
        leaf_status_cache: Dict[int, tuple[List[EvalStatus], ...]] = {}

        out: List[_PairCooccurrence] = []
        for j in range(1, len(entry_rules)):
            for i in range(j):
                evaluated, later_fires, later_independent = _cooccurrence_counts(
                    statuses[j], statuses[i]
                )
                legs: tuple[_PairLegCooccurrence, ...] = ()
                if (
                    evaluated >= _MIN_EVALUATED_BARS
                    and later_fires > 0
                    and later_independent == 0
                    and len(leaves[j]) > 1
                ):
                    if j not in leaf_status_cache:
                        leaf_status_cache[j] = tuple(
                            _sweep_statuses(leaf, views) for leaf in leaves[j]
                        )
                    legs = tuple(
                        _PairLegCooccurrence(
                            _format_predicate(leaf),
                            *_cooccurrence_counts(leaf_statuses, statuses[i]),
                        )
                        for leaf, leaf_statuses in zip(leaves[j], leaf_status_cache[j])
                    )
                out.append(
                    _PairCooccurrence(
                        earlier_index=i,
                        later_index=j,
                        earlier_side=entry_rules[i].side,
                        later_side=entry_rules[j].side,
                        evaluated=evaluated,
                        later_fires=later_fires,
                        later_independent_fires=later_independent,
                        legs=legs,
                    )
                )
        return out

    def probe_starvation(self, spec: Any, market_data: Any) -> List[_RuleStarvation]:
        """Union-based starvation verdict for every entry rule after the first.

        Preconditions: ``spec`` is a ``StrategySpec``; ``market_data`` is
        ``Optional[Dict[str, List[OHLCVBar]]]`` (the fetched bars) or falsy.
        Postconditions: one :class:`_RuleStarvation` per rule index ``j >= 1``
        over ``spec.entry_rules`` (same ``EntryRule`` filtering, and hence the
        same index space, as :meth:`probe` and :meth:`probe_pairs`) — empty
        when there are fewer than 2 entry rules or no usable bars. Each rule's
        verdict is taken against the UNION of every rule listed before it, per
        ``evaluate_entry_rules``' definition, rather than one earlier rule at a
        time. Every rule pairs with every earlier rule regardless of ``side``,
        matching ``evaluate_entry_rules``' default ``side_filter=None``.

        Unlike :meth:`probe_pairs` — which drops any bar either side of a pair
        is warming up on, since a pairwise tally has no notion of "the earlier
        rules as a whole" — the starvation verdict keeps a rule's warmup-prefix
        fires as :attr:`_RuleStarvation.warmup_independent_fires`. That is not
        an inconsistency between the two: ``probe_pairs`` reports no findings,
        while this verdict does, and a finding that ignored those bars would
        call a rule starved that ``replay_entries`` demonstrably trades.

        Per-leg diagnostics are computed only for a ``"starved"`` verdict on a
        multi-leaf rule, decomposing the STARVED rule's own leaves against its
        :attr:`_RuleStarvation.dominant_index` coverer — the earlier rule that
        explains most of the shadowing — so the diagnostic reads exactly like
        the dead-rule and pairwise ones. The per-rule sweeps are shared with
        the verdict computation, so the indicator cache is already warm when
        a starved multi-leaf rule's additional per-leg sweeps run.
        """
        assert spec is not None, "spec must be a StrategySpec"
        entry_rules = _entry_rules(spec)
        if len(entry_rules) < 2 or not market_data:
            return []
        views = _build_views(market_data)
        if not views:
            return []

        statuses = [_sweep_statuses(rule.when, views) for rule in entry_rules]
        verdicts = _starvation_verdicts(statuses, [rule.side for rule in entry_rules])

        out: List[_RuleStarvation] = []
        for verdict in verdicts:
            if verdict.verdict != "starved":
                out.append(verdict)
                continue
            leaves = list(iter_leaf_predicates(entry_rules[verdict.rule_index].when))
            if len(leaves) <= 1:
                out.append(verdict)
                continue
            dominant = statuses[verdict.dominant_index]
            legs = tuple(
                _PairLegCooccurrence(
                    _format_predicate(leaf),
                    *_cooccurrence_counts(_sweep_statuses(leaf, views), dominant),
                )
                for leaf in leaves
            )
            out.append(replace(verdict, legs=legs))
        return out

    def all_entries_dead(self, reach: List[_RuleReachability]) -> bool:
        """True iff EVERY entry rule was judged and never fires.

        Pre: ``reach`` is the output of :meth:`probe`.
        Post: True only when there is at least one rule, all rules had enough
        post-warmup bars to judge, and none fired — the condition under which a
        *compiled* strategy is guaranteed to emit zero entries. A rule with too
        few bars to judge makes this ``False`` (we cannot prove zero entries).
        """
        return bool(reach) and all(r.judged for r in reach) and all(r.fires == 0 for r in reach)

    def to_gate_results(
        self, reach: List[_RuleReachability], spec: Any, *, phase: StrategyLabPhase = "synthesis"
    ) -> List[QualityGateResult]:
        """Render a :meth:`probe` tally into phase-tagged gate results.

        Pre: ``reach`` is the output of :meth:`probe` for ``spec``.
        Post: one result per rule — ``critical`` (compiled path) / ``warning``
        (custom path) for an unreachable rule, ``info`` for a reachable rule or
        one with too few bars to judge. Never empty when ``reach`` is non-empty.
        """
        custom = bool(getattr(spec, "requires_custom_code", False))
        with self._using_phase(phase):
            if not reach:
                return [
                    self._info("Predicate reachability probe: no entry rules or bars to probe.")
                ]
            results: List[QualityGateResult] = []
            for r in reach:
                rule_key = f"entry[{r.rule_index}]"
                if not r.judged:
                    results.append(
                        self._info(
                            f"Entry rule {rule_key} (side={r.side}): only {r.evaluated} post-warmup "
                            "bar(s) available — too few to judge reachability; skipped.",
                            rule_id=rule_key,
                        )
                    )
                elif r.fires == 0:
                    detail = (
                        f"Entry rule {rule_key} (side={r.side}) never satisfies its predicate across "
                        f"{r.evaluated} post-warmup bar(s) of the fetched data — it cannot generate "
                        f"entries as authored. {_leg_diagnostic(r)}"
                    )
                    if custom:
                        results.append(
                            self._warning(
                                detail
                                + " (custom-code path: the executed code may differ from the spec, "
                                "but the authored entry logic is unreachable on this data.)",
                                rule_id=rule_key,
                            )
                        )
                    else:
                        results.append(self._critical(detail, rule_id=rule_key))
                else:
                    results.append(
                        self._info(
                            f"Entry rule {rule_key} (side={r.side}) satisfied on {r.fires}/"
                            f"{r.evaluated} post-warmup bar(s).",
                            rule_id=rule_key,
                        )
                    )
            return results

    def to_starvation_gate_results(
        self, verdicts: List[_RuleStarvation], spec: Any, *, phase: StrategyLabPhase = "synthesis"
    ) -> List[QualityGateResult]:
        """Render a :meth:`probe_starvation` verdict list into phase-tagged findings.

        Pre: ``verdicts`` is the output of :meth:`probe_starvation` for ``spec``.
        Post: at most ONE result per rule — never the O(entry_rules^2)
        one-result-per-pair reporting a pairwise view would imply, so a rule
        shadowed by several earlier rules yields one finding naming all of them
        rather than one finding per coverer. By verdict:

          * ``"starved"`` → ``critical`` on the compiled path, ``warning`` on
            the custom path, mirroring :meth:`to_gate_results`' severity model
            exactly: under the union verdict plus the ``_MIN_STARVATION_FIRES``
            evidence floor, a starved rule provably contributes zero entries on
            this data — the same grade of fact a dead rule is. The wording
            leads with the evidence (fire counts and which earlier rules cover
            them) and ends with the three resolutions the design prompt already
            teaches, so a deliberate priority ordering is adjudicable rather
            than merely accused.
          * ``"warmup_only"`` → ``warning`` on both paths. The rule DOES open
            positions — on the warmup prefix, where the earlier rules cannot
            yet fire — so ``critical``'s "contributes no entries" claim would
            be false; but it stops contributing the moment those rules warm up,
            which is a shadowing bug the author still has to see. A warning on
            the compiled path says exactly that, and the custom path adds its
            usual caveat without escalating.
          * ``"abstained_bars"`` / ``"abstained_thin"`` → ``info``, so an
            abstention is visible on the gate timeline instead of being
            indistinguishable from "checked, nothing found".
          * ``"dead"`` → NOTHING. A rule that never fires at all is already
            reported once, per rule, by :meth:`to_gate_results`; this is a
            DISTINCT finding kind and must not double-report it.
          * ``"reachable"`` → NOTHING. :meth:`to_gate_results` already reports
            every rule's firing count, so a per-rule "not starved" info would
            only dilute it.
        """
        custom = bool(getattr(spec, "requires_custom_code", False))
        with self._using_phase(phase):
            results: List[QualityGateResult] = []
            for v in verdicts:
                rule_id = f"entry[{v.rule_index}]"
                kind = v.verdict
                if kind == "abstained_bars":
                    results.append(
                        self._info(
                            f"Entry rule {rule_id} (side={v.side}): only {v.evaluated} bar(s) "
                            "judged against every earlier rule — too few to judge structural "
                            "starvation; skipped.",
                            rule_id=rule_id,
                        )
                    )
                elif kind == "abstained_thin":
                    results.append(
                        self._info(
                            f"Entry rule {rule_id} (side={v.side}) fires on {v.fires}/"
                            f"{v.evaluated} post-warmup bar(s), all of them also covered by an "
                            f"earlier rule ({_coverage_text(v.coverage)}) — fewer than "
                            f"{_MIN_STARVATION_FIRES} covered fires is too few to separate "
                            "structural starvation from a merely rarely-firing rule, so it is "
                            "not reported as starved.",
                            rule_id=rule_id,
                        )
                    )
                elif kind == "warmup_only":
                    steady_state = (
                        f"it fires {v.fires} time(s), every one of them also covered by an "
                        f"earlier rule ({_coverage_text(v.coverage)})"
                        if v.fires
                        else "it never fires at all"
                    )
                    detail = (
                        f"Entry rule {rule_id} (side={v.side}) is selectable only while an "
                        f"earlier rule is still warming up: it fires on "
                        f"{v.warmup_independent_fires} warmup-prefix bar(s) that no earlier rule "
                        f"is satisfied on, but across the {v.evaluated} bar(s) where every "
                        f"earlier rule is warm {steady_state}. It will open positions at the "
                        "start of the window and then never again, so its contribution is an "
                        "artefact of the fetched window's left edge rather than of the strategy. "
                        "Resolve it the same way as a starved rule — fold its conditions into "
                        "the earlier rule's all_of, list it BEFORE the broader rule if it is the "
                        "intended higher priority, or loosen it so it can fire where the earlier "
                        "rules don't."
                    )
                    if custom:
                        detail += (
                            " (custom-code path: the executed code may differ from the spec, but "
                            "the authored entry logic is shadowed on this data.)"
                        )
                    results.append(self._warning(detail, rule_id=rule_id))
                elif kind == "starved":
                    detail = (
                        f"Entry rule {rule_id} (side={v.side}) is structurally starved: it fires "
                        f"on {v.fires}/{v.evaluated} post-warmup bar(s), and an earlier, "
                        f"higher-priority rule fires on every one of them "
                        f"({_coverage_text(v.coverage)}) — under first-match-wins priority "
                        f"{rule_id} is never the rule selected, so it contributes no entries as "
                        "ordered. Resolve by folding its conditions into the earlier rule's "
                        "all_of, listing it BEFORE the broader rule if it is the intended "
                        "higher priority, or loosening it so it can fire where the earlier "
                        f"rules don't. {_independence_leg_diagnostic(v.legs, v.dominant_index)}"
                    )
                    if custom:
                        results.append(
                            self._warning(
                                detail
                                + " (custom-code path: the executed code may differ from the "
                                "spec, but the authored entry logic is unreachable on this "
                                "data.)",
                                rule_id=rule_id,
                            )
                        )
                    else:
                        results.append(self._critical(detail, rule_id=rule_id))
            return results

    def check(
        self, spec: Any, market_data: Any, *, phase: StrategyLabPhase = "synthesis"
    ) -> List[QualityGateResult]:
        """Convenience: :meth:`probe` then :meth:`to_gate_results` (used in tests)."""
        return self.to_gate_results(self.probe(spec, market_data), spec, phase=phase)

    def check_starvation(
        self, spec: Any, market_data: Any, *, phase: StrategyLabPhase = "synthesis"
    ) -> List[QualityGateResult]:
        """Convenience: :meth:`probe_starvation` then :meth:`to_starvation_gate_results`."""
        return self.to_starvation_gate_results(
            self.probe_starvation(spec, market_data), spec, phase=phase
        )


def _leg_diagnostic(r: _RuleReachability) -> str:
    """Human diagnostic for a dead rule, naming the bottleneck leg(s).

    Pre: ``r`` is a dead :class:`_RuleReachability` (``fires == 0``, judged).
    Post: for a single-condition rule, states the condition never holds; for a
    conjunction, names the conjunct(s) that never hold on their own, or — when
    every conjunct holds individually — reports that they never co-occur (the
    all_of is unsatisfiable on this data). Empty legs → a generic message.
    """
    if not r.legs:
        return "The predicate never holds on any bar."
    never = [leg.predicate for leg in r.legs if leg.fires == 0]
    if never:
        return f"These condition(s) never hold on their own: {never}."
    return (
        "Every condition holds on its own but they never co-occur on the same bar "
        "(the all_of conjunction is unsatisfiable on this data)."
    )


def _coverage_text(coverage: tuple[tuple[int, int], ...]) -> str:
    """Render a starvation coverage set as "entry[0] covers 37, entry[1] covers 12".

    Preconditions: ``coverage`` is a non-empty :attr:`_RuleStarvation.coverage`
    tuple (already ordered by descending covered-fire count).
    Postconditions: names every earlier rule that covered at least one of the
    starved rule's fires, most-covering first, with the count each accounts
    for — so the reader can see whether one earlier rule explains the shadowing
    or several jointly do. Pure.
    """
    assert coverage, "coverage must be non-empty to be rendered"
    return ", ".join(f"entry[{index}] covers {count}" for index, count in coverage)


def _independence_leg_diagnostic(legs: tuple[_PairLegCooccurrence, ...], earlier_index: int) -> str:
    """Human diagnostic for a shadowed rule, naming the bottleneck leg(s).

    Preconditions: ``legs`` is the per-leaf co-occurrence tally of the shadowed
    rule against the rule at ``earlier_index`` (empty for a single-condition
    rule, or when no per-leg breakdown was computed).
    Postconditions: for a single-condition rule, states it never fires
    independently of the earlier rule; for a conjunction, names the leaf(ves)
    that never fire independently on their own, or — when every leaf CAN fire
    independently on its own — reports that they only co-occur with each other
    on bars the earlier rule also covers (mirrors :func:`_leg_diagnostic`'s two
    branches, keyed on ``independent_fires`` instead of ``fires``). Empty legs
    → a generic message. Pure.
    """
    if not legs:
        return "The predicate never fires independently of the earlier rule."
    never = [leg.predicate for leg in legs if leg.fires > 0 and leg.independent_fires == 0]
    if never:
        return f"These condition(s) never fire independently of entry[{earlier_index}]: {never}."
    return (
        f"Every condition can fire independently of entry[{earlier_index}] on its own, "
        f"but they only co-occur with each other on bars entry[{earlier_index}] also covers."
    )
