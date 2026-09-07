"""Unit tests for the pre-backtest ``PredicateReachabilityProbe``.

Synthetic OHLCV series with known shapes drive each reachability verdict:
a monotonically rising series makes a fast/slow SMA cross never happen, so a
``sma(fast) < sma(slow)`` entry is provably data-dependent dead code, while a
``close > sma(slow)`` entry always holds.
"""

from __future__ import annotations

from investment_team.market_data_service import OHLCVBar
from investment_team.models import StrategySpec
from investment_team.strategy_lab.quality_gates.predicate_reachability import (
    PredicateReachabilityProbe,
    _RuleStarvation,
)
from investment_team.strategy_lab.spec_dsl import (
    DEFAULT_SIZING_PAYLOAD,
    AllOf,
    AnyOf,
    EntryRule,
    IndicatorRef,
    Predicate,
    StopLossRule,
)


def _rising_bars(n: int = 300) -> list[OHLCVBar]:
    """Monotonically rising closes — a fast SMA stays above a slow SMA forever."""
    return [
        OHLCVBar(
            date=f"2020-{(i // 28) + 1:02d}-{(i % 28) + 1:02d}",
            open=100.0 + i,
            high=101.0 + i,
            low=99.0 + i,
            close=100.0 + i,
            volume=1000.0 + i,
        )
        for i in range(n)
    ]


def _spec(when, *, custom: bool = False, extra_entries=None) -> StrategySpec:
    entries = [EntryRule(side="long", when=when)]
    entries += list(extra_entries or [])
    spec = StrategySpec(
        strategy_id="t",
        authored_by="x",
        asset_class="stocks",
        hypothesis="h",
        signal_definition="s",
        timeframe="1d",
        entry_rules=entries,
        exit_rules=[StopLossRule(pct=0.05)],
        sizing=DEFAULT_SIZING_PAYLOAD,
        target_symbols=["AAA"],
    )
    return spec.model_copy(update={"requires_custom_code": custom})


def _sma(period: int) -> IndicatorRef:
    return IndicatorRef(name="sma", params={"period": period})


_MD = {"AAA": _rising_bars()}
_DEAD = Predicate(lhs=_sma(5), op="<", rhs=_sma(200))  # fast<slow never on a rising series
_ALIVE = Predicate(lhs="bar.close", op=">", rhs=_sma(200))  # close>slow always on a rising series


def _details(results):
    return [(r.severity, r.details) for r in results]


def test_reachable_predicate_is_info() -> None:
    probe = PredicateReachabilityProbe()
    reach = probe.probe(_spec(_ALIVE), _MD)
    results = probe.to_gate_results(reach, _spec(_ALIVE))
    assert all(r.severity == "info" for r in results), _details(results)
    assert probe.all_entries_dead(reach) is False


def test_dead_predicate_compiled_is_critical() -> None:
    probe = PredicateReachabilityProbe()
    spec = _spec(_DEAD)
    reach = probe.probe(spec, _MD)
    results = probe.to_gate_results(reach, spec)
    crit = [r for r in results if r.severity == "critical"]
    assert crit and "never satisfies" in crit[0].details
    assert crit[0].rule_id == "entry[0]"
    assert probe.all_entries_dead(reach) is True


def test_dead_predicate_custom_is_warning_not_critical() -> None:
    probe = PredicateReachabilityProbe()
    spec = _spec(_DEAD, custom=True)
    results = probe.to_gate_results(probe.probe(spec, _MD), spec)
    assert any(r.severity == "warning" for r in results)
    assert not any(r.severity == "critical" for r in results)


def test_all_of_with_one_leg_never_holds_diagnostic() -> None:
    # One leg is always true and one is always false on a rising series, so the
    # conjunction is dead because a leg never holds ON ITS OWN — exercises the
    # "never hold on their own" diagnostic branch (the "never co-occur" branch,
    # where every leg fires but never together, is covered by
    # ``test_leg_diagnostic_never_co_occur_branch``).
    never_holds = AllOf(
        of=[
            Predicate(lhs="bar.close", op=">", rhs=_sma(200)),  # always true
            Predicate(lhs="bar.close", op="<", rhs=_sma(200)),  # always false
        ]
    )
    probe = PredicateReachabilityProbe()
    spec = _spec(never_holds)
    results = probe.to_gate_results(probe.probe(spec, _MD), spec)
    crit = [r for r in results if r.severity == "critical"]
    assert crit, _details(results)
    # The false leg never holds on its own → "never hold on their own" branch.
    assert "never hold on their own" in crit[0].details


def test_leg_diagnostic_never_co_occur_branch() -> None:
    # Direct unit test of the diagnostic: when every conjunct fires on its own but
    # the whole rule never does, report the unsatisfiable-conjunction message.
    from investment_team.strategy_lab.quality_gates.predicate_reachability import (
        _leg_diagnostic,
        _LegReachability,
        _RuleReachability,
    )

    r = _RuleReachability(
        rule_index=0,
        side="long",
        evaluated=100,
        fires=0,
        legs=(
            _LegReachability("A>B", evaluated=100, fires=40),
            _LegReachability("C>D", evaluated=100, fires=60),
        ),
    )
    assert "never co-occur" in _leg_diagnostic(r)


def test_insufficient_bars_abstains_with_info() -> None:
    probe = PredicateReachabilityProbe()
    spec = _spec(_DEAD)
    short_md = {"AAA": _rising_bars(30)}  # < 200 warmup → almost no post-warmup bars
    results = probe.to_gate_results(probe.probe(spec, short_md), spec)
    # No critical: too few post-warmup bars to call it dead code.
    assert not any(r.severity == "critical" for r in results)
    assert any("too few to judge" in r.details for r in results)


def test_no_entry_rules_or_no_data_returns_empty_probe() -> None:
    probe = PredicateReachabilityProbe()
    assert probe.probe(_spec(_ALIVE), None) == []
    assert probe.probe(_spec(_ALIVE), {}) == []
    assert probe.probe(_spec(_ALIVE), {"AAA": []}) == []


def test_all_entries_dead_requires_every_rule_dead() -> None:
    probe = PredicateReachabilityProbe()
    # One dead + one alive → not all dead → no forced-redesign signal.
    spec = _spec(_DEAD, extra_entries=[EntryRule(side="long", when=_ALIVE)])
    reach = probe.probe(spec, _MD)
    assert probe.all_entries_dead(reach) is False
    # Both dead → all dead.
    both_dead = _spec(
        _DEAD,
        extra_entries=[EntryRule(side="long", when=Predicate(lhs=_sma(3), op="<", rhs=_sma(200)))],
    )
    assert probe.all_entries_dead(probe.probe(both_dead, _MD)) is True


def test_check_convenience_wraps_probe_and_format() -> None:
    probe = PredicateReachabilityProbe()
    spec = _spec(_DEAD)
    results = probe.check(spec, _MD, phase="synthesis")
    assert all(
        r.phase == "synthesis" and r.gate_name == "predicate_reachability_probe" for r in results
    )
    assert any(r.severity == "critical" for r in results)


# ---------------------------------------------------------------------------
# Additional shapes: partial reachability, cross_above, any_of, multi-symbol,
# and the exact _MIN_EVALUATED_BARS boundary.
# ---------------------------------------------------------------------------


def _v_shaped_bars(n: int = 120) -> list[OHLCVBar]:
    """Decline for the first half, then rebound steeply — a fast SMA starts
    below a slow SMA and crosses above it exactly once during the rebound."""
    bars = []
    px = 200.0
    for i in range(n):
        px += -1.0 if i < n // 2 else 3.0
        bars.append(
            OHLCVBar(
                date=f"2020-{(i // 28) + 1:02d}-{(i % 28) + 1:02d}",
                open=px,
                high=px + 1,
                low=px - 1,
                close=px,
                volume=1000.0,
            )
        )
    return bars


def _oscillating_bars(n: int = 200) -> list[OHLCVBar]:
    """A sine-wave close series — an RSI threshold fires on roughly half the bars."""
    import math

    return [
        OHLCVBar(
            date=f"2020-{(i // 28) + 1:02d}-{(i % 28) + 1:02d}",
            open=100.0,
            high=101.0,
            low=99.0,
            close=100.0 + 10 * math.sin(i / 5.0),
            volume=1000.0,
        )
        for i in range(n)
    ]


def _declining_bars(n: int = 300) -> list[OHLCVBar]:
    """Monotonically declining closes — the mirror of ``_rising_bars``."""
    return [
        OHLCVBar(
            date=f"2020-{(i // 28) + 1:02d}-{(i % 28) + 1:02d}",
            open=500.0 - i,
            high=501.0 - i,
            low=499.0 - i,
            close=500.0 - i,
            volume=1000.0,
        )
        for i in range(n)
    ]


def test_partial_reachability_fires_on_some_bars_not_all() -> None:
    # RSI oscillates around 50 on a sine-wave series, so 'rsi < 50' fires on
    # roughly (but not exactly) half the post-warmup bars — neither always-true
    # nor always-false.
    when = Predicate(lhs=IndicatorRef(name="rsi", params={"period": 14}), op="<", rhs=50.0)
    probe = PredicateReachabilityProbe()
    spec = _spec(when)
    reach = probe.probe(spec, {"AAA": _oscillating_bars()})
    assert reach[0].judged
    assert 0 < reach[0].fires < reach[0].evaluated, reach[0]


def test_cross_above_fires_exactly_at_the_crossing_bar() -> None:
    # A fast SMA starts below a slow SMA (decline) and crosses above it exactly
    # once during the rebound — cross_above depends on previous-bar state, the
    # operator most likely to diverge between the probe's loop and the engine's.
    when = Predicate(
        lhs=IndicatorRef(name="sma", params={"period": 3}),
        op="cross_above",
        rhs=IndicatorRef(name="sma", params={"period": 10}),
    )
    probe = PredicateReachabilityProbe()
    spec = _spec(when)
    reach = probe.probe(spec, {"AAA": _v_shaped_bars()})
    assert reach[0].judged
    assert reach[0].fires == 1, reach[0]  # crosses exactly once
    results = probe.to_gate_results(reach, spec)
    assert all(r.severity == "info" for r in results)  # reachable, not dead


def test_any_of_reachable_when_one_branch_is_alive() -> None:
    # One branch never holds, the other always does — the any_of as a whole
    # must be reachable (every bar satisfies at least the alive branch).
    when = AnyOf(
        of=[
            Predicate(lhs="bar.close", op="<", rhs=_sma(200)),  # always false
            Predicate(lhs="bar.close", op=">", rhs=_sma(200)),  # always true
        ]
    )
    probe = PredicateReachabilityProbe()
    spec = _spec(when)
    reach = probe.probe(spec, _MD)
    assert reach[0].fires == reach[0].evaluated  # fires on every judged bar
    assert not any(r.severity == "critical" for r in probe.to_gate_results(reach, spec))


def test_multi_symbol_mixed_reachability_aggregates_across_symbols() -> None:
    # AAA never satisfies the predicate (declining); BBB always does (rising).
    # The rule as a whole is reachable because it fires on BBB's bars, and the
    # evaluated count aggregates both symbols' post-warmup bars.
    when = Predicate(lhs="bar.close", op=">", rhs=_sma(200))
    probe = PredicateReachabilityProbe()
    spec = _spec(when)
    reach = probe.probe(spec, {"AAA": _declining_bars(), "BBB": _rising_bars()})
    assert reach[0].judged
    assert 0 < reach[0].fires < reach[0].evaluated
    assert not any(r.severity == "critical" for r in probe.to_gate_results(reach, spec))


def test_min_evaluated_bars_boundary_19_vs_20() -> None:
    # sma(period=5) values start at index 4 (0-indexed), so N bars yields
    # evaluated = N - 4. N=23 -> evaluated=19 (below threshold, unjudged);
    # N=24 -> evaluated=20 (at threshold, judged) — the exact abstention edge.
    when = Predicate(lhs="bar.close", op=">", rhs=_sma(5))
    probe = PredicateReachabilityProbe()
    spec = _spec(when)

    below = probe.probe(spec, {"AAA": _rising_bars(23)})
    assert below[0].evaluated == 19
    assert below[0].judged is False
    below_results = probe.to_gate_results(below, spec)
    assert not any(r.severity == "critical" for r in below_results)
    assert any("too few to judge" in r.details for r in below_results)

    at = probe.probe(spec, {"AAA": _rising_bars(24)})
    assert at[0].evaluated == 20
    assert at[0].judged is True


# ---------------------------------------------------------------------------
# probe_pairs: pairwise entry-predicate co-occurrence analysis.
# ---------------------------------------------------------------------------


def test_pair_later_fires_independently_of_earlier() -> None:
    # Disjoint firing sets (no indicators, so neither predicate warms up):
    # earlier fires on the early bars, later fires on the late bars.
    earlier = Predicate(lhs="bar.close", op="<", rhs=150.0)  # fires i in [0, 49]
    later = Predicate(lhs="bar.close", op=">", rhs=250.0)  # fires i in [151, 299]
    probe = PredicateReachabilityProbe()
    spec = _spec(earlier, extra_entries=[EntryRule(side="long", when=later)])
    pairs = probe.probe_pairs(spec, _MD)
    assert len(pairs) == 1
    pair = pairs[0]
    assert pair.earlier_index == 0 and pair.later_index == 1
    assert pair.judged
    assert pair.later_fires > 0
    assert pair.later_independent_fires == pair.later_fires
    assert pair.later_never_independent is False
    assert pair.later_dead is False


def test_pair_later_never_fires_independently_of_earlier() -> None:
    # _ALIVE (close>sma(200)) is judged (and always fires) only from i=199 on
    # (sma(200) warmup). A later rule whose firing window sits entirely inside
    # that judged range can never fire independently of it.
    later = Predicate(lhs="bar.close", op=">", rhs=310.0)  # fires i > 210
    probe = PredicateReachabilityProbe()
    spec = _spec(_ALIVE, extra_entries=[EntryRule(side="long", when=later)])
    pairs = probe.probe_pairs(spec, _MD)
    assert len(pairs) == 1
    pair = pairs[0]
    assert pair.judged
    assert pair.later_fires > 0
    assert pair.later_independent_fires == 0
    assert pair.later_never_independent is True
    assert pair.later_dead is False


def test_pair_later_dead_is_distinct_from_never_independent() -> None:
    # A later rule that never fires at all is "dead" (already reported
    # elsewhere) — the pairwise analysis must not also call it "never
    # independent", which is reserved for a rule that fires but is shadowed.
    probe = PredicateReachabilityProbe()
    spec = _spec(_ALIVE, extra_entries=[EntryRule(side="long", when=_DEAD)])
    pairs = probe.probe_pairs(spec, _MD)
    assert len(pairs) == 1
    pair = pairs[0]
    assert pair.judged
    assert pair.later_fires == 0
    assert pair.later_dead is True
    assert pair.later_never_independent is False


def test_pair_leg_diagnostics_decompose_later_rule_two_leg_all_of() -> None:
    # Leg 1 (close>300) fires i in [201, 299] — entirely inside _ALIVE's
    # judged, always-firing range [199, 299]. Leg 2 (close<360) fires i in
    # [0, 259]; its early fires (i < 199) land on bars where _ALIVE is still
    # in warmup, which the co-occurrence tally excludes from `evaluated`
    # altogether. So both legs — and hence the all_of as a whole — never fire
    # independently of _ALIVE, exercising the per-leg breakdown.
    later = AllOf(
        of=[
            Predicate(lhs="bar.close", op=">", rhs=300.0),  # fires i > 200
            Predicate(lhs="bar.close", op="<", rhs=360.0),  # fires i < 260
        ]
    )
    probe = PredicateReachabilityProbe()
    spec = _spec(_ALIVE, extra_entries=[EntryRule(side="long", when=later)])
    pairs = probe.probe_pairs(spec, _MD)
    assert len(pairs) == 1
    pair = pairs[0]
    assert pair.later_never_independent is True
    assert len(pair.legs) == 2
    for leg in pair.legs:
        assert leg.evaluated == pair.evaluated
        assert leg.fires > 0
        assert leg.independent_fires == 0


def test_pair_mixed_side_still_pairs() -> None:
    probe = PredicateReachabilityProbe()
    spec = _spec(_ALIVE, extra_entries=[EntryRule(side="short", when=_DEAD)])
    pairs = probe.probe_pairs(spec, _MD)
    assert len(pairs) == 1
    pair = pairs[0]
    assert pair.earlier_side == "long"
    assert pair.later_side == "short"


def test_pair_fewer_than_two_rules_returns_empty() -> None:
    probe = PredicateReachabilityProbe()
    assert probe.probe_pairs(_spec(_ALIVE), _MD) == []


def test_pair_no_market_data_returns_empty() -> None:
    probe = PredicateReachabilityProbe()
    spec = _spec(_ALIVE, extra_entries=[EntryRule(side="long", when=_DEAD)])
    assert probe.probe_pairs(spec, None) == []
    assert probe.probe_pairs(spec, {}) == []
    assert probe.probe_pairs(spec, {"AAA": []}) == []


def test_pair_min_evaluated_bars_boundary_19_vs_20() -> None:
    # Neither predicate involves an indicator, so evaluated == the bar count
    # exactly (no warmup on either side) — the exact abstention edge.
    earlier = Predicate(lhs="bar.close", op=">", rhs=0.0)
    later = Predicate(lhs="bar.close", op=">", rhs=50.0)
    probe = PredicateReachabilityProbe()
    spec = _spec(earlier, extra_entries=[EntryRule(side="long", when=later)])

    below = probe.probe_pairs(spec, {"AAA": _rising_bars(19)})
    assert below[0].evaluated == 19
    assert below[0].judged is False

    at = probe.probe_pairs(spec, {"AAA": _rising_bars(20)})
    assert at[0].evaluated == 20
    assert at[0].judged is True


def test_cooccurrence_counts_pure_function_hand_built() -> None:
    # Direct unit test of the pure computation: no probe, spec, or bars
    # involved — just two hand-built, positionally-aligned status sequences.
    from investment_team.strategy_lab.quality_gates.predicate_reachability import (
        _cooccurrence_counts,
    )

    later_statuses = ["satisfied", "satisfied", "warmup", "miss"]
    earlier_statuses = ["miss", "satisfied", "satisfied", "miss"]
    # Bar 2 excluded (later is warmup there). Of the remaining 3: later fires
    # at 0 and 1 (evaluated=3, later_fires=2); of those, only bar 0 has the
    # earlier rule not satisfied (later_independent_fires=1).
    assert _cooccurrence_counts(later_statuses, earlier_statuses) == (3, 2, 1)


def test_cooccurrence_counts_pure_function_excludes_earlier_side_warmup() -> None:
    # Same pure function, but pinning the OTHER side of the warmup contract:
    # a bar where the EARLIER rule is warmup (not the later one) must also be
    # excluded from `evaluated`, not silently counted as an independent fire.
    from investment_team.strategy_lab.quality_gates.predicate_reachability import (
        _cooccurrence_counts,
    )

    later_statuses = ["satisfied", "satisfied", "satisfied"]
    earlier_statuses = ["warmup", "satisfied", "miss"]
    # Bar 0 excluded (earlier is warmup there). Of the remaining 2: later
    # fires at both (evaluated=2, later_fires=2); of those, only bar 2 has
    # the earlier rule not satisfied (later_independent_fires=1).
    assert _cooccurrence_counts(later_statuses, earlier_statuses) == (2, 2, 1)


def test_sweep_statuses_matches_sweep_aggregate() -> None:
    # Guards the _sweep refactor: its (evaluated, fires) counts must still
    # match what _sweep_statuses's raw per-bar sequence implies.
    from investment_team.strategy_lab.quality_gates.predicate_reachability import (
        _build_views,
        _sweep,
        _sweep_statuses,
    )

    views = _build_views(_MD)
    statuses = _sweep_statuses(_ALIVE, views)
    evaluated = sum(1 for s in statuses if s != "warmup")
    fires = sum(1 for s in statuses if s == "satisfied")
    assert _sweep(_ALIVE, views) == (evaluated, fires)


# ---------------------------------------------------------------------------
# to_starvation_gate_results / check_starvation: turning the union-based
# starvation verdict into a distinct "structurally starved" finding, separate
# from dead code — and, above all, one that does not cry wolf.
#
# Fixtures here use bare close thresholds rather than indicators wherever the
# point is a set relation: on the rising series `close > 250` is a strict
# subset of `close > 150` BY CONSTRUCTION, so the test can never flake on the
# window happening not to produce an overlap.
# ---------------------------------------------------------------------------

_BROAD = Predicate(lhs="bar.close", op=">", rhs=150.0)  # fires i >= 51
_NARROW = Predicate(lhs="bar.close", op=">", rhs=250.0)  # fires i >= 151 (subset of _BROAD)


def _entry(when, side: str = "long") -> EntryRule:
    return EntryRule(side=side, when=when)


def _starvation(spec, market_data=_MD):
    return PredicateReachabilityProbe().check_starvation(spec, market_data)


def test_genuinely_starved_second_rule_is_reported() -> None:
    # Broad-then-narrow: entry[1]'s firing set is a strict subset of entry[0]'s,
    # so it can never win the first-match-wins scan.
    spec = _spec(_BROAD, extra_entries=[_entry(_NARROW)])
    results = _starvation(spec)
    assert len(results) == 1
    r = results[0]
    assert r.severity == "critical"
    assert r.passed is False
    assert r.rule_id == "entry[1]"
    assert "structurally starved" in r.details
    assert "entry[0] covers" in r.details
    # Distinct from the dead-code phrasing used by to_gate_results.
    assert "never satisfies its predicate" not in r.details


def test_starved_rule_custom_path_is_warning_not_critical() -> None:
    spec = _spec(_BROAD, custom=True, extra_entries=[_entry(_NARROW)])
    results = _starvation(spec)
    assert len(results) == 1
    assert results[0].severity == "warning"
    assert "custom-code path" in results[0].details


def test_independently_reachable_rules_yield_no_starvation_finding() -> None:
    # Disjoint firing sets: neither shadows the other.
    earlier = Predicate(lhs="bar.close", op="<", rhs=150.0)
    later = Predicate(lhs="bar.close", op=">", rhs=250.0)
    spec = _spec(earlier, extra_entries=[_entry(later)])
    assert _starvation(spec) == []


def test_narrow_then_broad_priority_ordering_is_not_flagged() -> None:
    # THE false-positive case. Exactly the two rules of
    # test_genuinely_starved_second_rule_is_reported, listed the other way
    # round: the narrow rule takes precedence and the broad one still wins the
    # scan on every bar between the two thresholds. Deliberate priority
    # ordering must produce nothing at all.
    spec = _spec(_NARROW, extra_entries=[_entry(_BROAD)])
    assert _starvation(spec) == []


def test_starvation_finding_leads_with_evidence_and_names_remedies() -> None:
    # The finding has to be adjudicable: an author must be able to see how much
    # evidence stands behind it and what to do about it, without leaving the
    # message.
    spec = _spec(_BROAD, extra_entries=[_entry(_NARROW)])
    detail = _starvation(spec)[0].details
    assert "fires on 149/300 post-warmup bar(s)" in detail  # the evidence
    assert "entry[0] covers 149" in detail  # who shadows it, and by how much
    assert "folding its conditions" in detail  # remedy 1: fold
    assert "listing it BEFORE the broader rule" in detail  # remedy 2: reorder
    assert "loosening it" in detail  # remedy 3: loosen


def test_three_rule_mixed_reachability_reports_only_the_starved_rule() -> None:
    # entry[1] is shadowed by entry[0]; entry[2] fires only on early bars that
    # neither of them reaches. Exactly one finding, and it names entry[1].
    spec = _spec(
        _BROAD,
        extra_entries=[
            _entry(_NARROW),
            _entry(Predicate(lhs="bar.close", op="<", rhs=120.0)),  # fires i <= 19
        ],
    )
    results = _starvation(spec)
    assert [r.rule_id for r in results] == ["entry[1]"]
    assert results[0].severity == "critical"


def test_jointly_starved_rule_is_reported_once_naming_every_coverer() -> None:
    # entry[0] and entry[1] partition the bars between them, so entry[2] is
    # covered by their UNION while neither is a superset of it on its own.
    # The pairwise view cannot see this; the union verdict must.
    probe = PredicateReachabilityProbe()
    spec = _spec(
        Predicate(lhs="bar.close", op="<", rhs=200.0),  # fires i <= 99
        extra_entries=[
            _entry(Predicate(lhs="bar.close", op=">", rhs=199.5)),  # fires i >= 100
            _entry(_BROAD),  # fires i >= 51 — every one of them covered
        ],
    )

    # Pairwise: entry[2] fires independently of EACH earlier rule, so the
    # per-pair analysis finds nothing to report.
    for pair in probe.probe_pairs(spec, _MD):
        if pair.later_index == 2:
            assert pair.later_independent_fires > 0

    results = probe.check_starvation(spec, _MD)
    assert [r.rule_id for r in results] == ["entry[2]"]
    # One finding, not one per coverer, and the most-covering rule is named
    # first so the reader sees the dominant cause up front.
    assert "entry[1] covers 200, entry[0] covers 49" in results[0].details


def test_rarely_firing_rule_below_the_evidence_floor_abstains_with_info() -> None:
    # entry[1] fires 4 times, all of them inside entry[0]'s range. Four covered
    # fires is ordinary coincidence, not proof of structure — so this abstains
    # loudly rather than accusing.
    spec = _spec(_BROAD, extra_entries=[_entry(Predicate(lhs="bar.close", op=">", rhs=395.0))])
    results = _starvation(spec)
    assert len(results) == 1
    r = results[0]
    assert r.severity == "info"
    assert r.passed is True
    assert r.rule_id == "entry[1]"
    assert "fires on 4/300" in r.details
    assert "rarely-firing" in r.details
    assert "structurally starved" not in r.details


def test_evidence_floor_boundary_four_versus_five_covered_fires() -> None:
    # The exact edge: 4 covered fires abstains, 5 is reported.
    four = _spec(_BROAD, extra_entries=[_entry(Predicate(lhs="bar.close", op=">", rhs=395.0))])
    five = _spec(_BROAD, extra_entries=[_entry(Predicate(lhs="bar.close", op=">", rhs=394.0))])

    below = _starvation(four)
    assert [r.severity for r in below] == ["info"]

    at = _starvation(five)
    assert [r.severity for r in at] == ["critical"]
    assert "fires on 5/300" in at[0].details


def test_a_single_independent_fire_makes_a_rare_rule_reachable() -> None:
    # Same rare rule as above, plus ONE bar it wins outright. That is enough:
    # first-match-wins can select it, so there is nothing to report.
    later = AnyOf(
        of=[
            Predicate(lhs="bar.close", op=">", rhs=395.0),  # 4 covered fires
            Predicate(lhs="bar.close", op="<", rhs=101.0),  # 1 independent fire (i == 0)
        ]
    )
    spec = _spec(_BROAD, extra_entries=[_entry(later)])
    assert _starvation(spec) == []


def test_dead_later_rule_not_double_reported_as_starved() -> None:
    # A later rule that never fires at all must be reported as dead code
    # (by to_gate_results) but NOT additionally as structurally starved.
    probe = PredicateReachabilityProbe()
    spec = _spec(_ALIVE, extra_entries=[_entry(_DEAD)])

    dead_results = probe.to_gate_results(probe.probe(spec, _MD), spec)
    assert any(r.severity == "critical" and r.rule_id == "entry[1]" for r in dead_results)

    assert probe.check_starvation(spec, _MD) == []


def test_unjudged_window_abstains_with_info_rather_than_silence() -> None:
    # Too short a window is a coverage problem, not a reachability verdict —
    # but silence would be indistinguishable from "checked, nothing found".
    spec = _spec(
        Predicate(lhs="bar.close", op=">", rhs=0.0),
        extra_entries=[_entry(Predicate(lhs="bar.close", op=">", rhs=50.0))],
    )
    results = _starvation(spec, {"AAA": _rising_bars(19)})
    assert len(results) == 1
    assert results[0].severity == "info"
    assert results[0].rule_id == "entry[1]"
    assert "too few to judge structural starvation" in results[0].details


def test_starved_all_of_rule_reports_the_per_leg_diagnostic() -> None:
    # Both legs of the later all_of never fire independently of _ALIVE, so the
    # finding carries the same per-leg detail dead-rule findings do.
    later = AllOf(
        of=[
            Predicate(lhs="bar.close", op=">", rhs=300.0),
            Predicate(lhs="bar.close", op="<", rhs=360.0),
        ]
    )
    spec = _spec(_ALIVE, extra_entries=[_entry(later)])
    results = _starvation(spec)
    assert len(results) == 1
    assert "never fire independently of entry[0]" in results[0].details


def test_starvation_probe_mixed_side_still_pairs() -> None:
    # Priority applies across long/short alike, matching evaluate_entry_rules'
    # default side_filter=None.
    spec = _spec(_BROAD, extra_entries=[_entry(_NARROW, side="short")])
    results = _starvation(spec)
    assert len(results) == 1
    assert "side=short" in results[0].details


def test_starvation_probe_fewer_than_two_rules_or_no_data_returns_empty() -> None:
    probe = PredicateReachabilityProbe()
    assert probe.probe_starvation(_spec(_ALIVE), _MD) == []
    spec = _spec(_BROAD, extra_entries=[_entry(_NARROW)])
    assert probe.probe_starvation(spec, None) == []
    assert probe.probe_starvation(spec, {}) == []
    assert probe.probe_starvation(spec, {"AAA": []}) == []


def test_rule_shadowed_only_after_an_earlier_rule_warms_up_is_a_warning_not_a_critical() -> None:
    # entry[0] needs 200 bars before it can fire at all; entry[1] is a bare
    # close threshold that fires from bar 51. Over bars 51..198 `entry[0]` is
    # still warming up, so `evaluate_entry_rules` returns entry[1] and the
    # engine opens positions from it — calling it starved ("contributes no
    # entries") would be a false critical.
    spec = _spec(
        Predicate(lhs="bar.close", op=">", rhs=_sma(200)),
        extra_entries=[_entry(_BROAD)],
    )
    results = _starvation(spec)
    assert [(r.severity, r.rule_id) for r in results] == [("warning", "entry[1]")]
    detail = results[0].details
    assert "selectable only while an earlier rule is still warming up" in detail
    assert "warmup-prefix bar(s)" in detail
    assert "entry[0] covers" in detail


def test_warmup_only_finding_carries_the_custom_path_caveat() -> None:
    spec = _spec(
        Predicate(lhs="bar.close", op=">", rhs=_sma(200)),
        custom=True,
        extra_entries=[_entry(_BROAD)],
    )
    results = _starvation(spec)
    assert [r.severity for r in results] == ["warning"]
    assert "custom-code path" in results[0].details


def test_warmup_only_finding_reads_never_fires_when_the_steady_window_is_empty() -> None:
    from investment_team.strategy_lab.quality_gates.predicate_reachability import (
        _RuleStarvation,
    )

    verdict = _RuleStarvation(
        rule_index=1,
        side="long",
        evaluated=40,
        fires=0,
        independent_fires=0,
        coverage=(),
        legs=(),
        warmup_independent_fires=7,
    )
    results = PredicateReachabilityProbe().to_starvation_gate_results(
        [verdict], _spec(_BROAD, extra_entries=[_entry(_NARROW)])
    )
    assert [r.severity for r in results] == ["warning"]
    assert "it never fires at all" in results[0].details
    assert "covers" not in results[0].details


def test_check_starvation_convenience_wraps_probe_and_format() -> None:
    spec = _spec(_BROAD, extra_entries=[_entry(_NARROW)])
    results = PredicateReachabilityProbe().check_starvation(spec, _MD, phase="synthesis")
    assert all(
        r.phase == "synthesis" and r.gate_name == "predicate_reachability_probe" for r in results
    )
    assert any(r.severity == "critical" and r.rule_id == "entry[1]" for r in results)


# ---------------------------------------------------------------------------
# _starvation_verdicts: the pure union computation, driven directly from
# hand-built status sequences — every rung of the ladder, no bars, no specs.
# ---------------------------------------------------------------------------


def _verdict(later: list[str], *earlier: list[str]) -> _RuleStarvation:
    from investment_team.strategy_lab.quality_gates.predicate_reachability import (
        _starvation_verdicts,
    )

    rows = [*earlier, later]
    sides = ["long"] * len(rows)
    return _starvation_verdicts(rows, sides)[-1]


def _pattern(total: int, satisfied: range) -> list[str]:
    return ["satisfied" if i in satisfied else "miss" for i in range(total)]


def test_starvation_verdict_abstains_below_min_evaluated_bars() -> None:
    v = _verdict(_pattern(19, range(19)), _pattern(19, range(19)))
    assert v.evaluated == 19
    assert v.verdict == "abstained_bars"


def test_starvation_verdict_dead_rule_takes_precedence_over_starvation() -> None:
    v = _verdict(_pattern(30, range(0)), _pattern(30, range(30)))
    assert v.fires == 0
    assert v.verdict == "dead"


def test_starvation_verdict_reachable_when_any_fire_is_independent() -> None:
    v = _verdict(_pattern(30, range(0, 10)), _pattern(30, range(10, 30)))
    assert v.independent_fires == 10
    assert v.verdict == "reachable"
    assert v.coverage == ()


def test_starvation_verdict_thin_evidence_abstains() -> None:
    v = _verdict(_pattern(30, range(0, 4)), _pattern(30, range(30)))
    assert (v.fires, v.independent_fires) == (4, 0)
    assert v.verdict == "abstained_thin"


def test_starvation_verdict_starved_at_the_evidence_floor() -> None:
    v = _verdict(_pattern(30, range(0, 5)), _pattern(30, range(30)))
    assert (v.fires, v.independent_fires) == (5, 0)
    assert v.verdict == "starved"
    assert v.dominant_index == 0


def test_starvation_verdict_keeps_warmup_prefix_fires_out_of_the_steady_state_window() -> None:
    # Bars where an earlier rule is still warming up are not part of the
    # steady-state denominator — but they are not discarded either: the engine
    # selects the later rule there, so they are counted separately.
    later = ["satisfied"] * 30
    earlier = ["warmup"] * 10 + ["satisfied"] * 20
    v = _verdict(later, earlier)
    assert v.evaluated == 20
    assert (v.fires, v.independent_fires) == (20, 0)
    assert v.warmup_independent_fires == 10
    assert v.verdict == "warmup_only"


def test_warmup_prefix_fires_are_not_independent_when_another_earlier_rule_covers_them() -> None:
    # Two earlier rules: the first is warming up over the prefix, the second is
    # satisfied throughout. The later rule is shadowed on every bar, warmup
    # prefix included, so it is genuinely starved — not warmup_only.
    later = ["satisfied"] * 30
    warming = ["warmup"] * 10 + ["miss"] * 20
    covering = ["satisfied"] * 30
    v = _verdict(later, warming, covering)
    assert v.evaluated == 20
    assert (v.fires, v.independent_fires, v.warmup_independent_fires) == (20, 0, 0)
    assert v.verdict == "starved"


def test_warmup_only_beats_dead_when_every_fire_is_on_the_warmup_prefix() -> None:
    # The rule fires ONLY while the earlier rule warms up. `probe` counts those
    # bars and calls it reachable, so the starvation ladder must not call it
    # dead and fall silent — the shadowing is still worth reporting.
    later = ["satisfied"] * 10 + ["miss"] * 20
    earlier = ["warmup"] * 10 + ["satisfied"] * 20
    v = _verdict(later, earlier)
    assert (v.evaluated, v.fires) == (20, 0)
    assert v.warmup_independent_fires == 10
    assert v.verdict == "warmup_only"
    assert v.coverage == ()


def test_a_steady_state_independent_fire_outranks_warmup_prefix_fires() -> None:
    later = ["satisfied"] * 30
    earlier = ["warmup"] * 10 + ["miss"] + ["satisfied"] * 19
    v = _verdict(later, earlier)
    assert (v.independent_fires, v.warmup_independent_fires) == (1, 10)
    assert v.verdict == "reachable"


def test_starvation_verdict_coverage_is_ordered_by_descending_share() -> None:
    # entry[0] covers 4 of the fires, entry[1] covers 8 — the dominant coverer
    # is named first regardless of listing order.
    later = _pattern(40, range(0, 12))
    first = _pattern(40, range(0, 4))
    second = _pattern(40, range(4, 12))
    v = _verdict(later, first, second)
    assert v.rule_index == 2
    assert v.coverage == ((1, 8), (0, 4))
    assert v.dominant_index == 1
    assert v.verdict == "starved"


def test_starvation_verdicts_empty_for_a_single_rule() -> None:
    from investment_team.strategy_lab.quality_gates.predicate_reachability import (
        _starvation_verdicts,
    )

    assert _starvation_verdicts([["satisfied"] * 30], ["long"]) == []


# ---------------------------------------------------------------------------
# _independence_leg_diagnostic / _coverage_text: rendering helpers.
# ---------------------------------------------------------------------------


def test_independence_leg_diagnostic_co_occur_branch() -> None:
    # Every leg CAN fire independently on its own, but the rule as a whole
    # never does — report the co-occurrence message rather than blaming a leg.
    from investment_team.strategy_lab.quality_gates.predicate_reachability import (
        _independence_leg_diagnostic,
        _PairLegCooccurrence,
    )

    legs = (
        _PairLegCooccurrence("A>B", evaluated=100, fires=40, independent_fires=10),
        _PairLegCooccurrence("C>D", evaluated=100, fires=60, independent_fires=5),
    )
    assert "only co-occur" in _independence_leg_diagnostic(legs, 0)


def test_independence_leg_diagnostic_empty_legs_generic_message() -> None:
    from investment_team.strategy_lab.quality_gates.predicate_reachability import (
        _independence_leg_diagnostic,
    )

    assert (
        _independence_leg_diagnostic((), 0)
        == "The predicate never fires independently of the earlier rule."
    )


def test_coverage_text_names_every_coverer_with_its_share() -> None:
    from investment_team.strategy_lab.quality_gates.predicate_reachability import _coverage_text

    assert _coverage_text(((1, 200), (0, 49))) == "entry[1] covers 200, entry[0] covers 49"
