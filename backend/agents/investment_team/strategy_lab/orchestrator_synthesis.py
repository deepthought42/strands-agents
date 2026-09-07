"""SynthesisMixin — the pre-synthesis / synthesis-loop / validation-gate /
anomaly-handling cluster extracted from :mod:`orchestrator`.

Pure move (issue #1733, PR 2 of 6 decomposing the StrategyLabOrchestrator
god-class tracking issue): every method below is relocated verbatim from
``orchestrator.py``. No behavior changes, with one narrow exception —
``_cached_run_strategy_code``, ``_run_synthesis_loop``, and
``_evaluate_synthesis_round`` resolve ``run_strategy_code`` /
``MAX_CODE_REFINEMENT_ROUNDS`` / ``compute_metrics`` through a
function-local ``from . import orchestrator as _orchestrator_module``
deferred import instead of a static module-level import. This is required
(not a stylistic choice): several tests monkeypatch these three names
directly on the ``orchestrator`` module (e.g. ``monkeypatch.setattr(
orchestrator_module, "run_strategy_code", ...)`` in ``conftest.py``,
``test_strategy_lab_zero_refinement_rounds.py``'s patch of
``MAX_CODE_REFINEMENT_ROUNDS``, and ``test_acceptance_gate_integration.py`` /
``_walk_forward_test_helpers.py``'s patch of ``compute_metrics``); a static
import here would bind a private reference in this module's globals that
such a patch would never reach. Do not "clean up" these deferred imports
back into static ones — see the identical, pre-existing idiom in
``zero_trade_repair.py`` (its import of ``_maybe_attach_coverage_report``)
for precedent.

``SynthesisMixin`` is mixed into ``StrategyLabOrchestrator`` — see the class
statement in ``orchestrator.py`` for the current base order (more mixins
have since joined it); its methods expect the attributes
``StrategyLabOrchestrator.__init__`` sets on ``self`` (``self.strategy_validator``, ``self.code_safety_checker``,
``self.code_conformance_gate``, ``self.predicate_conformance_gate``,
``self.spec_readiness_gate``, ``self.target_symbol_coverage_gate``,
``self.predicate_reachability_probe``, ``self.zero_trade_repairer``,
``self._backtest_cache``, ``self._last_anomaly_check``), plus the
``self.record_gates`` / ``self.build_orchestrator_gate`` /
``self._build_short_circuit_record`` / ``self._check_anomalies_cached`` /
``self._committed_code_conformance_verdict`` / ``self._refine_or_exhaust`` /
``self._fetch_market_data`` methods — all of which stay on the base class
and resolve via MRO on the final composed instance.

This module must not import anything from ``orchestrator.py`` at module
level (that would be circular: ``orchestrator.py`` imports ``SynthesisMixin``
from here before its own class statement executes) — the two function-local
deferred imports described above are the sole, intentional exception. Pure
helpers shared by both this cluster and code that stays in ``orchestrator.py``
live in ``_orchestrator_helpers.py`` instead.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional, Tuple

from ..market_data_service import OHLCVBar
from ..models import BacktestConfig, StrategyLabRecord, StrategySpec, TradeRecord
from ..trading_service.modes.sandbox_compat import StrategyRunResult
from ._orchestrator_helpers import (
    RefinementStallTracker,
    _attach_execution_diagnostics,
    _critical_failures,
    _DesignAttemptState,
    _DesignPersistContext,
    _DriftCollector,
    _format_execution_diagnostics,
    _has_critical_failures,
    _MarketDataFetch,
    _maybe_attach_coverage_report,
    _round_demoted_conformance,
    _SynthesisLoopOutcome,
)
from .agents._llm_budget import DesignBudgetExhausted, _annotate_budget_exhaustion
from .backtest_cache import BacktestCache
from .coverage_probe import format_coverage_report
from .exceptions import SpecImplementabilityError
from .quality_gates.models import QualityGateResult, join_gate_details
from .quality_gates.universe_injection import inject_universe_and_guard

PhaseCallback = Callable[[str, Dict[str, Any]], None]


@dataclass
class _AnomalyRecoveryOutcome(_DesignAttemptState):
    """Bundle of state returned by ``_handle_critical_anomalies``.

    The synthesis loop's evaluation phase delegates to that helper when
    the backtest produces critical anomaly gates. The helper either
    commits a zero-trade-repair proposal, applies a generic refinement,
    or exhausts the round budget — and the loop body needs to know which
    outcome happened so it can continue or break.

    Invariants on return:
    - ``exhausted=True`` ⇒ caller breaks the synthesis loop with
      ``max_rounds_exhausted=True``; the spec/code/trades/metrics fields
      carry the last failed-round values (callers should not commit them).
    - ``exhausted=False`` ⇒ caller continues to the next round. On the
      zero-trade-repair path, ``spec``/``code``/``trades``/``metrics``/
      ``exec_result`` all carry the freshly executed, mutually consistent
      repair state. On the generic-refinement path, only ``spec``/``code``
      carry the new (not-yet-executed) proposal for the next round;
      ``trades``/``metrics``/``exec_result`` still carry the current
      round's values, since the refined code hasn't run yet.
    """

    exec_result: StrategyRunResult
    exhausted: bool
    # Set only when a zero-trade repair commits new code (which replaces the
    # persisted trades but is not otherwise conformance-gated): the conformance
    # verdict of the committed repair code. ``None`` on the generic-refinement
    # path, which leaves ``trades`` unchanged so the round's existing verdict
    # still applies and must not be overwritten.
    ran_on_non_conforming_code: Optional[bool] = None
    # True iff ``exhausted`` was caused by the refinement-loop stall guard
    # (``RefinementStallTracker``) rather than genuine round-cap exhaustion.
    # Only the generic-refinement path (``_refine_or_exhaust``) can set this;
    # a committed zero-trade repair always returns ``exhausted=False`` and
    # this field's default (``False``) applies.
    stalled: bool = False


@dataclass
class _SynthesisEvaluateResult(_DesignAttemptState):
    """Return envelope for the synthesis loop's backtest-evaluation step.

    ``action`` is one of:
    - ``"success"`` — gates clean, the caller marks ``execution_succeeded`` and
      breaks the loop;
    - ``"continue"`` — a critical anomaly was recovered (refined/repaired) and
      the caller continues to the next round;
    - ``"exhausted"`` — recovery ran the round budget out, the caller marks
      ``max_rounds_exhausted`` and breaks.

    The remaining fields carry the (possibly recovery-mutated) round state back
    to the loop so it can thread them into the final outcome.
    """

    action: str
    exec_result: StrategyRunResult
    ran_on_non_conforming_code: Optional[bool]
    runtime_lookahead_violation: bool
    # True iff ``action="exhausted"`` was caused by the refinement-loop
    # stall guard rather than genuine round-cap exhaustion. Carried from
    # ``_AnomalyRecoveryOutcome.stalled`` on the anomaly-recovery path;
    # ``False`` on the direct "success" return (no recovery ran).
    stalled: bool = False


@dataclass
class _TradeCollectionResult:
    """Return envelope for ``_run_synthesis_trade_collection``.

    Bundles the round's collected trades with the target-symbol coverage
    verdict so the loop can thread ``should_break`` into its own
    ``max_rounds_exhausted`` exit without re-deriving any of the other three
    fields.
    """

    trades: List[TradeRecord]
    ran_on_non_conforming_code: bool
    open_position_entry_reasons: List[str]
    should_break: bool


class SynthesisMixin:
    """Pre-synthesis / synthesis-loop / validation-gate / anomaly-handling cluster."""

    def _run_pre_synthesis_phase(
        self,
        *,
        spec: StrategySpec,
        config: BacktestConfig,
        all_gate_results: List[QualityGateResult],
        code: str,
        original_spec: StrategySpec,
        original_code: str,
        rationale: str,
        refinement_attempts: List[Dict[str, Any]],
        emit: PhaseCallback,
        phase_back_count: int = 0,
        drift_collector: Optional[_DriftCollector] = None,
        design_context: Optional[_DesignPersistContext] = None,
    ) -> Optional[StrategyLabRecord]:
        """Run spec validation before the refinement loop.

        Pre: ``spec`` is a constructed ``StrategySpec`` that has already
        passed the design-phase ``SpecReadinessGate`` check (the design
        loop is the sole caller of that gate with ``phase="design"``);
        ``all_gate_results`` is the orchestrator's running gate list that
        the caller persists.
        Post: returns a short-circuit ``StrategyLabRecord`` when a critical
        gate fires (and ``all_gate_results`` is extended in place with the
        pre-synthesis gates); returns ``None`` to signal the caller can
        continue into the synthesis refinement loop.

        The "strategy_code is missing" critical from StrategySpecValidator
        is deliberately filtered: post-design we always have *some* code
        (the loop's existing safety + regeneration paths repair degenerate
        inputs), so short-circuiting on that critical would regress a
        recoverable case into an outright failure.
        """
        # ``config`` is intentionally not consulted by the readiness gate
        # here — the design loop owns the ``phase="design"`` readiness
        # check, and the round-0 readiness call inside ``_run_synthesis_loop``
        # carries ``phase="synthesis"``. The argument is still threaded
        # into the short-circuit record below so persistence sees the
        # same config the design phase saw.
        pre_spec_gates_raw = self.strategy_validator.validate(spec)
        pre_spec_gates = [
            g
            for g in pre_spec_gates_raw
            if not (g.severity == "critical" and g.details.startswith("strategy_code is missing"))
        ]
        self.record_gates(pre_spec_gates, all_gate_results, refinement_round=-1)

        criticals = _critical_failures(pre_spec_gates)
        if not criticals:
            return None

        emit(
            "coding",
            {
                "sub_phase": "failed",
                "phase": "pre_synthesis",
                "checks_total": len(pre_spec_gates),
                "checks_passed": sum(1 for g in pre_spec_gates if g.passed),
            },
        )
        return self._build_short_circuit_record(
            spec=spec,
            config=config,
            code=code,
            original_spec=original_spec,
            original_code=original_code,
            rationale=rationale,
            all_gate_results=all_gate_results,
            refinement_attempts=refinement_attempts,
            short_circuit_status="failed: spec_validation",
            short_circuit_reason=(
                "Spec validation failed before code synthesis: "
                + join_gate_details(criticals)
            ),
            emit=emit,
            design_context=design_context,
            phase_back_count=phase_back_count,
            drift_collector=drift_collector,
        )

    def _cached_run_strategy_code(
        self,
        code: str,
        market_data: Dict[str, List[OHLCVBar]],
        config: BacktestConfig,
        *,
        strategy: StrategySpec,
    ) -> StrategyRunResult:
        """Run ``code`` through the attempt-scoped :class:`BacktestCache`.

        Routes the module-level ``run_strategy_code`` (so test monkeypatches
        of ``orchestrator.run_strategy_code`` still apply) and memoizes on
        ``(code, market_data, config)``. The cache is created lazily so a
        sub-loop invoked directly in a test — outside ``_run_design_attempt``
        — still works (with a degenerate one-entry cache).

        Pre: ``code`` is non-empty; ``market_data`` is the hoisted per-symbol
        OHLCV dict for the attempt.
        Post: returns the ``StrategyRunResult`` for ``code`` — a fresh run on
        the first call with a given key, the stored result on subsequent ones.
        """
        # Local import — avoids a circular import (orchestrator.py imports
        # SynthesisMixin from this module before its own class statement
        # executes) and keeps test monkeypatches of
        # ``orchestrator.run_strategy_code`` honored, since a static
        # module-level import here would bind a separate reference that a
        # patch on the ``orchestrator`` module's attribute would not reach.
        # Mirrors the existing deferred-import idiom in zero_trade_repair.py.
        from . import orchestrator as _orchestrator_module

        cache = getattr(self, "_backtest_cache", None)
        if cache is None:
            cache = self._backtest_cache = BacktestCache()
        result, _hit = cache.get_or_run(
            code,
            market_data,
            config,
            strategy=strategy,
            runner=_orchestrator_module.run_strategy_code,
        )
        return result

    def _run_synthesis_loop(
        self,
        *,
        spec: StrategySpec,
        code: str,
        config: BacktestConfig,
        all_gate_results: List[QualityGateResult],
        refinement_attempts: List[str],
        zero_trade_attempts: List[str],
        emit: PhaseCallback,
        drift_collector: Optional[_DriftCollector] = None,
    ) -> _SynthesisLoopOutcome:
        """Run up to ``MAX_CODE_REFINEMENT_ROUNDS`` of (validate → fetch →
        execute → trade-collect → evaluate), refining ``spec``/``code``
        between rounds.

        Pre: pre-synthesis spec gating already passed (the caller's
        ``_run_pre_synthesis_phase`` returned ``None``); ``all_gate_results``
        is the running gate list the loop appends to via ``record_gates``;
        ``refinement_attempts`` and ``zero_trade_attempts`` are the running
        change-log lists the loop appends to in-place.
        Post: returns a ``_SynthesisLoopOutcome`` carrying the final
        ``spec``/``code``/``trades``/``metrics`` (plus ``market_data`` and
        the universe audit lists), with ``execution_succeeded=True`` iff
        a round produced a clean run with no critical anomalies, and
        ``max_rounds_exhausted=True`` iff the loop ran the full budget
        without converging. The two flags are mutually exclusive. The
        loop never raises — fatal failures short-circuit by setting flags
        and returning.

        A single ``RefinementStallTracker`` scoped to this invocation tracks
        each failing round's ``(hash(code), hash(failure_details))``
        signature; when it is unchanged for ``_refinement_stall_rounds()``
        consecutive rounds the loop exits early with
        ``max_rounds_exhausted=True`` and ``refinement_stalled=True``,
        mirroring the design ↔ review loop's ``CritiqueLedger`` stall exit.

        State mutations on the caller's lists (``all_gate_results``,
        ``refinement_attempts``, ``zero_trade_attempts``) happen in-place
        and the caller observes them directly; the outcome dataclass
        carries only values the caller cannot read off shared mutable
        state.
        """
        if not isinstance(spec, StrategySpec):
            raise ValueError("spec must be a StrategySpec")
        if not isinstance(code, str):
            raise ValueError("code must be a string")
        if not isinstance(config, BacktestConfig):
            raise ValueError("config must be a BacktestConfig")
        if not isinstance(all_gate_results, list):
            raise ValueError("all_gate_results must be a list")
        if not isinstance(refinement_attempts, list):
            raise ValueError("refinement_attempts must be a list")
        if not isinstance(zero_trade_attempts, list):
            raise ValueError("zero_trade_attempts must be a list")

        # Local import — same deferred-import rationale as
        # ``_cached_run_strategy_code`` above: keeps test monkeypatches of
        # ``orchestrator.compute_metrics`` honored.
        from . import orchestrator as _orchestrator_module

        trades: List[TradeRecord] = []
        open_position_entry_reasons: List[str] = []
        metrics = _orchestrator_module.compute_metrics(
            [], config.initial_capital, config.start_date, config.end_date
        )
        execution_succeeded = False
        market_data: Optional[Dict[str, List[OHLCVBar]]] = None
        requested_symbols: List[str] = []
        fetched_symbols: List[str] = []
        provider_used: Dict[str, str] = {}
        max_rounds_exhausted = False
        # True iff ``max_rounds_exhausted`` was caused by ``stall_tracker``
        # detecting an unchanged ``(code, failure_details)`` signature for
        # consecutive rounds, rather than the loop genuinely running out of
        # rounds. Scoped to this one loop invocation, same as ``stall_tracker``.
        refinement_stalled = False
        stall_tracker = RefinementStallTracker()
        # Tracks whether the LAST executed round (including any
        # ``_handle_critical_anomalies`` recovery) surfaced the harness's
        # runtime ``lookahead_violation`` (``error_type == LOOKAHEAD``).
        # Threaded onto the synthesis outcome so the verification phase
        # can stamp the cause onto ``acceptance_reason`` instead of the
        # generic ``publication_disabled`` message.
        runtime_lookahead_violation = False
        predicate_conformance_attempts = 0
        # Captured at trade-collection time for the round whose backtest is
        # persisted: True when that round ran custom code whose
        # predicate-conformance check was demoted (warning) past the retry
        # budget. A later round that passes conformance but fails before
        # collecting trades does not clear an earlier demoted round's value.
        ran_on_non_conforming_code = False
        # Signature of the inputs the reachability probe depends on (entry rules +
        # code-path). Market data is fetched once and static, so the probe is
        # re-run only when the entry rules change across refinement rounds — this
        # avoids recomputing and recording duplicate reachability gate results
        # every round for an unchanged spec.
        last_reachability_sig: Optional[tuple] = None
        # Pre-bound so the post-loop invariant check below never sees an
        # unbound name if MAX_CODE_REFINEMENT_ROUNDS is ever 0 (loop body
        # never executes).
        round_num = -1

        for round_num in range(_orchestrator_module.MAX_CODE_REFINEMENT_ROUNDS):
            round_gate_results: List[QualityGateResult] = []

            # ── 2a-pre: INJECT UNIVERSE + symbol guard (deterministic) ───
            code = self._run_synthesis_universe_injection(
                spec=spec, code=code, drift_collector=drift_collector
            )

            # ── 2a: VALIDATE (code safety + spec readiness on round 0) ───
            round_gate_results, predicate_conformance_attempts = (
                self._run_synthesis_validation_gates(
                    spec=spec,
                    code=code,
                    config=config,
                    round_num=round_num,
                    predicate_conformance_attempts=predicate_conformance_attempts,
                    all_gate_results=all_gate_results,
                    emit=emit,
                )
            )

            checks_total = len(round_gate_results)
            checks_passed = sum(1 for g in round_gate_results if g.passed)

            critical_failures = _critical_failures(round_gate_results)
            if critical_failures:
                emit(
                    "coding",
                    {
                        "sub_phase": "failed",
                        "refinement_round": round_num,
                        "checks_passed": checks_passed,
                        "checks_total": checks_total,
                    },
                )
                failure_details = "\n".join(
                    f"- [{g.gate_name}{(':' + g.rule_id) if g.rule_id else ''}] {g.details}"
                    for g in critical_failures
                )
                spec, code, exhausted, stalled = self._refine_or_exhaust(
                    spec=spec,
                    code=code,
                    failure_phase="validation",
                    failure_details=failure_details,
                    metrics=None,
                    refinement_attempts=refinement_attempts,
                    round_num=round_num,
                    default_change_label="validation fix",
                    emit=emit,
                    stall_tracker=stall_tracker,
                    drift_collector=drift_collector,
                )
                if exhausted:
                    max_rounds_exhausted = True
                    refinement_stalled = stalled
                    break
                continue

            emit(
                "coding",
                {
                    "sub_phase": "completed",
                    "refinement_round": round_num,
                    "checks_passed": checks_passed,
                    "checks_total": checks_total,
                },
            )

            # ── 2b: FETCH DATA (once, reuse across rounds) ───────────
            if market_data is None:
                fetch = self._fetch_market_data_for_synthesis(
                    spec=spec,
                    config=config,
                    round_num=round_num,
                    all_gate_results=all_gate_results,
                    emit=emit,
                )
                requested_symbols = fetch.requested_symbols
                fetched_symbols = fetch.fetched_symbols
                provider_used = fetch.provider_used
                market_data = fetch.data
                if fetch.should_break:
                    break

            # ── 2b.5: PRE-BACKTEST reachability probe ────────────────
            last_reachability_sig = self._run_synthesis_reachability_probe(
                spec=spec,
                market_data=market_data,
                round_num=round_num,
                last_reachability_sig=last_reachability_sig,
                all_gate_results=all_gate_results,
            )

            # ── 2c: EXECUTE (syntax / runtime correctness) ───────────
            exec_result = self._run_synthesis_execution(
                spec=spec,
                code=code,
                market_data=market_data,
                config=config,
                round_num=round_num,
                all_gate_results=all_gate_results,
                emit=emit,
            )
            runtime_lookahead_violation = exec_result.error_type == "lookahead_violation"

            if not exec_result.success:
                failure_details = (
                    f"Error type: {exec_result.error_type}\nstderr:\n{exec_result.stderr}"
                )
                spec, code, exhausted, stalled = self._refine_or_exhaust(
                    spec=spec,
                    code=code,
                    failure_phase="execution",
                    failure_details=failure_details,
                    metrics=None,
                    refinement_attempts=refinement_attempts,
                    round_num=round_num,
                    default_change_label="execution fix",
                    emit=emit,
                    stall_tracker=stall_tracker,
                    drift_collector=drift_collector,
                )
                if exhausted:
                    max_rounds_exhausted = True
                    refinement_stalled = stalled
                    break
                continue

            # ── 2d: COLLECT TRADES + target-symbol coverage on trades ─
            collection = self._run_synthesis_trade_collection(
                spec=spec,
                exec_result=exec_result,
                round_gate_results=round_gate_results,
                round_num=round_num,
                all_gate_results=all_gate_results,
                emit=emit,
            )
            trades = collection.trades
            # This round's executed code is what produced the persisted trades;
            # attribute the conformance verdict to it (overwriting any earlier
            # round's value) so the flag tracks the backtest that survives.
            ran_on_non_conforming_code = collection.ran_on_non_conforming_code
            open_position_entry_reasons = collection.open_position_entry_reasons
            if collection.should_break:
                max_rounds_exhausted = True
                break

            # ── 2e: BACKTEST EVALUATION (anomaly gates → zero-trade-repair → generic refine) ─
            evaluation = self._evaluate_synthesis_round(
                state=_DesignAttemptState(spec=spec, code=code, trades=trades, metrics=metrics),
                exec_result=exec_result,
                market_data=market_data,
                config=config,
                round_num=round_num,
                ran_on_non_conforming_code=ran_on_non_conforming_code,
                all_gate_results=all_gate_results,
                refinement_attempts=refinement_attempts,
                zero_trade_attempts=zero_trade_attempts,
                emit=emit,
                stall_tracker=stall_tracker,
                drift_collector=drift_collector,
            )
            spec, code = evaluation.spec, evaluation.code
            trades, metrics = evaluation.trades, evaluation.metrics
            ran_on_non_conforming_code = evaluation.ran_on_non_conforming_code
            exec_result = evaluation.exec_result
            runtime_lookahead_violation = evaluation.runtime_lookahead_violation
            if evaluation.action == "exhausted":
                max_rounds_exhausted = True
                refinement_stalled = evaluation.stalled
                break
            if evaluation.action == "continue":
                continue

            # All gates passed — code is clean and backtest is sound
            execution_succeeded = True
            break

        # Post-condition: success and round-exhaustion are mutually exclusive.
        if execution_succeeded and max_rounds_exhausted:
            raise RuntimeError(
                "synthesis loop invariant violated: both execution_succeeded and "
                f"max_rounds_exhausted are True after round {round_num}; this is a bug "
                "in the round-evaluation loop above."
            )
        return _SynthesisLoopOutcome(
            spec=spec,
            code=code,
            trades=trades,
            metrics=metrics,
            market_data=market_data,
            requested_symbols=requested_symbols,
            fetched_symbols=fetched_symbols,
            execution_succeeded=execution_succeeded,
            max_rounds_exhausted=max_rounds_exhausted,
            provider_used=provider_used,
            open_position_entry_reasons=open_position_entry_reasons,
            runtime_lookahead_violation=runtime_lookahead_violation,
            ran_on_non_conforming_code=ran_on_non_conforming_code,
            refinement_stalled=refinement_stalled,
        )

    def _run_synthesis_universe_injection(
        self,
        *,
        spec: StrategySpec,
        code: str,
        drift_collector: Optional[_DriftCollector] = None,
    ) -> str:
        """Deterministically inject/refresh the UNIVERSE + on_bar symbol guard.

        Guarantees the ``UNIVERSE`` constant and the ``on_bar`` symbol guard are
        present and conformant before any gate sees the code, so the
        conformance symbol gate never burns a refinement round on boilerplate
        that is fully determined by ``spec.target_symbols``. Idempotent (strips
        then reinserts), so it is a no-op on already-conformant code and safe
        to apply to both the initial and every refined code variant.

        Pre: ``code`` is the current round's strategy source; ``spec`` is the
        round's ``StrategySpec``.
        Post: returns the (possibly unchanged) injected code. When injection
        changes the code, ``spec.strategy_code`` is updated in lockstep (so
        downstream consumers such as ``_maybe_attach_coverage_report``, which
        re-run probes off ``spec.strategy_code``, do not analyse stale,
        pre-injection source — refinement maintains the same invariant via
        ``_apply_updates``) and, if ``drift_collector`` is given, the change is
        recorded against it. When injection is a no-op, neither
        ``spec.strategy_code`` nor ``drift_collector`` is touched.
        """
        before_inject = code
        code = inject_universe_and_guard(code, spec)
        if code != before_inject:
            spec.strategy_code = code
            if drift_collector is not None:
                drift_collector.record_code_change(
                    phase="synthesis",
                    agent="universe_injector",
                    before_code=before_inject,
                    after_code=code,
                    reason="deterministic UNIVERSE + symbol-guard injection",
                )
        return code

    def _run_synthesis_validation_gates(
        self,
        *,
        spec: StrategySpec,
        code: str,
        config: BacktestConfig,
        round_num: int,
        predicate_conformance_attempts: int,
        all_gate_results: List[QualityGateResult],
        emit: PhaseCallback,
    ) -> Tuple[List[QualityGateResult], int]:
        """Run one round's validation gates and record them.

        Pre: ``code`` has had the deterministic universe/guard injection
        applied; ``all_gate_results`` is the running gate list.
        Post: returns ``(round_gate_results, predicate_conformance_attempts)``.
        ``round_gate_results`` holds this round's gate results — spec readiness
        (round 0 only), code safety, code conformance, and predicate
        conformance — and is recorded onto ``all_gate_results`` in place via
        ``record_gates``. Predicate conformance runs (and extends
        ``round_gate_results``) only when no prior validation gate fired a
        critical, preserving the gate-execution ordering exactly; its attempt
        counter is incremented and returned when it fires a critical.
        """
        emit("coding", {"sub_phase": "started", "refinement_round": round_num})
        round_gate_results: List[QualityGateResult] = []
        if round_num == 0:
            round_gate_results.extend(
                self.spec_readiness_gate.validate(spec, phase="synthesis", backtest_config=config)
            )
        code_gates = self.code_safety_checker.check(code, spec)
        round_gate_results.extend(code_gates)
        conformance_gates = self.code_conformance_gate.check(code, spec)
        round_gate_results.extend(conformance_gates)
        # Predicate conformance only runs when every prior validation gate
        # (spec readiness, code safety, code conformance) is clean. Checking
        # code that an earlier gate already flagged as critical adds noisy
        # rule_id criticals on top of the cleaner upstream critical.
        if not _has_critical_failures(round_gate_results):
            pred_conf_gates = self.predicate_conformance_gate.check(
                code,
                spec,
                attempt=predicate_conformance_attempts,
            )
            round_gate_results.extend(pred_conf_gates)
            if _has_critical_failures(pred_conf_gates):
                predicate_conformance_attempts += 1
        self.record_gates(round_gate_results, all_gate_results, refinement_round=round_num)
        return round_gate_results, predicate_conformance_attempts

    def _fetch_market_data_for_synthesis(
        self,
        *,
        spec: StrategySpec,
        config: BacktestConfig,
        round_num: int,
        all_gate_results: List[QualityGateResult],
        emit: PhaseCallback,
    ) -> _MarketDataFetch:
        """Fetch market data once for the synthesis loop.

        Pre: only called when ``market_data`` has not yet been fetched.
        Post: returns a ``_MarketDataFetch`` carrying the OHLCV payload and
        the symbol/provider audit trail. ``should_break=True`` when no data came
        back (records the ``market_data`` gate) or a critical fetch-coverage
        failure fired (records the coverage gates) — the caller adopts the
        symbol/provider fields regardless and breaks the loop when set. Records
        the relevant gates onto ``all_gate_results`` in place.
        """
        emit("backtesting", {"sub_phase": "fetching_data"})
        fetch = self._fetch_market_data(spec, config)
        requested_symbols = list(fetch.requested_symbols)
        fetched_symbols = list(fetch.fetched_symbols)
        provider_used = dict(fetch.provider_used)
        market_data = fetch.data
        if not market_data:
            all_gate_results.append(
                self.build_orchestrator_gate(
                    "market_data",
                    phase="synthesis",
                    details=f"No market data available for asset class '{spec.asset_class}'.",
                    refinement_round=round_num,
                )
            )
            return _MarketDataFetch(
                data=market_data,
                requested_symbols=requested_symbols,
                fetched_symbols=fetched_symbols,
                provider_used=provider_used,
                should_break=True,
            )
        total_bars = sum(len(bars) for bars in market_data.values())
        emit(
            "backtesting",
            {
                "sub_phase": "data_loaded",
                "symbols_count": len(market_data),
                "bars_count": total_bars,
            },
        )

        fetch_coverage_gates = self.target_symbol_coverage_gate.check_fetch(
            spec, requested_symbols, fetched_symbols
        )
        self.record_gates(fetch_coverage_gates, all_gate_results, refinement_round=round_num)
        should_break = _has_critical_failures(fetch_coverage_gates)
        return _MarketDataFetch(
            data=market_data,
            requested_symbols=requested_symbols,
            fetched_symbols=fetched_symbols,
            provider_used=provider_used,
            should_break=should_break,
        )

    def _run_synthesis_reachability_probe(
        self,
        *,
        spec: StrategySpec,
        market_data: Optional[Dict[str, List[OHLCVBar]]],
        round_num: int,
        last_reachability_sig: Optional[tuple],
        all_gate_results: List[QualityGateResult],
    ) -> Optional[tuple]:
        """Evaluate the authored entry predicates against the REAL fetched bars
        (same evaluator the compiled engine uses) before the backtest runs, so
        data-dependent dead code — legs that never co-occur, a cross that never
        happens — is surfaced early and per-leg instead of only after a doomed
        backtest.

        Pre: ``round_gate_results``' validation gates for this round already
        passed (no critical); ``last_reachability_sig`` is the signature
        returned by the previous round's call (``None`` on round 0).
        Post: returns the signature the probe last ran against. When
        ``market_data`` is not yet available, or the ``(entry_rules,
        requires_custom_code)`` signature is unchanged from
        ``last_reachability_sig``, returns ``last_reachability_sig`` unchanged
        and records nothing — market data is static once fetched, so an
        unchanged spec is not re-probed and its findings are not recorded
        twice. Otherwise runs the probe and records its gate results (critical
        on the compiled path where zero fires ⇒ zero entries; warning on the
        custom path where the executed code may differ) onto
        ``all_gate_results`` in place, plus the distinct structurally-starved
        findings (same severity model; a later rule whose every fire lands on a
        bar some earlier, higher-priority rule already covers, judged against
        the union of all earlier rules and only once enough covered fires have
        been observed to rule out coincidence — an abstention below that floor
        is recorded as an ``info`` rather than dropped, and a rule whose only
        unshadowed fires land on the warmup prefix, where the engine really
        does select it, is a ``warning`` rather than a critical).
        Findings never short-circuit the round — the post-backtest zero-trade
        path still owns routing.
        """
        reachability_sig = (
            tuple(str(getattr(r, "when", r)) for r in (spec.entry_rules or [])),
            bool(spec.requires_custom_code),
        )
        if not market_data or reachability_sig == last_reachability_sig:
            return last_reachability_sig
        reachability = self.predicate_reachability_probe.probe(spec, market_data)
        self.record_gates(
            self.predicate_reachability_probe.to_gate_results(
                reachability, spec, phase="synthesis"
            ),
            all_gate_results,
            refinement_round=round_num,
        )
        starvation = self.predicate_reachability_probe.probe_starvation(spec, market_data)
        self.record_gates(
            self.predicate_reachability_probe.to_starvation_gate_results(
                starvation, spec, phase="synthesis"
            ),
            all_gate_results,
            refinement_round=round_num,
        )
        return reachability_sig

    def _run_synthesis_execution(
        self,
        *,
        spec: StrategySpec,
        code: str,
        market_data: Dict[str, List[OHLCVBar]],
        config: BacktestConfig,
        round_num: int,
        all_gate_results: List[QualityGateResult],
        emit: PhaseCallback,
    ) -> StrategyRunResult:
        """Run the round's code through the attempt-scoped backtest cache.

        Pre: validation gates and the reachability probe for this round
        already ran; ``market_data`` is populated.
        Post: returns the ``StrategyRunResult`` for ``code`` (via
        ``_cached_run_strategy_code``). When ``exec_result.success`` is
        False, appends a ``code_execution`` critical gate describing the
        failure onto ``all_gate_results`` in place; the caller owns deciding
        whether to refine or exhaust the round budget on that failure.
        """
        emit("backtesting", {"sub_phase": "running_code", "refinement_round": round_num})
        exec_result = self._cached_run_strategy_code(code, market_data, config, strategy=spec)
        if not exec_result.success:
            all_gate_results.append(
                self.build_orchestrator_gate(
                    "code_execution",
                    phase="synthesis",
                    details=f"Execution failed ({exec_result.error_type}): {exec_result.stderr}",
                    refinement_round=round_num,
                )
            )
        return exec_result

    def _run_synthesis_trade_collection(
        self,
        *,
        spec: StrategySpec,
        exec_result: StrategyRunResult,
        round_gate_results: List[QualityGateResult],
        round_num: int,
        all_gate_results: List[QualityGateResult],
        emit: PhaseCallback,
    ) -> "_TradeCollectionResult":
        """Collect the round's trades and check target-symbol coverage on them.

        Pre: ``exec_result.success`` is True (the caller only reaches this
        stage after a clean execution); ``round_gate_results`` is this
        round's validation-gate list, used to derive the
        conformance-demotion verdict for the code that produced these
        trades.
        Post: returns a ``_TradeCollectionResult``. ``should_break=True``
        when the coverage check on the collected trades fires a critical —
        the caller sets ``max_rounds_exhausted`` and breaks the loop, and
        the "backtesting"/"completed" event is not emitted (mirroring the
        pre-extraction code, where that emit sat after the break);
        ``should_break=False`` otherwise, after emitting it. Records the
        coverage gates onto ``all_gate_results`` in place.
        """
        trades = exec_result.trades
        ran_on_non_conforming_code = _round_demoted_conformance(round_gate_results)
        open_position_entry_reasons = getattr(exec_result, "open_position_entry_reasons", [])

        trade_coverage_gates = self.target_symbol_coverage_gate.check_trades(spec, trades)
        self.record_gates(trade_coverage_gates, all_gate_results, refinement_round=round_num)
        should_break = _has_critical_failures(trade_coverage_gates)
        if should_break:
            return _TradeCollectionResult(
                trades=trades,
                ran_on_non_conforming_code=ran_on_non_conforming_code,
                open_position_entry_reasons=open_position_entry_reasons,
                should_break=True,
            )

        emit(
            "backtesting",
            {
                "sub_phase": "completed",
                "trades_count": len(trades),
                "execution_time": exec_result.execution_time_seconds,
            },
        )
        return _TradeCollectionResult(
            trades=trades,
            ran_on_non_conforming_code=ran_on_non_conforming_code,
            open_position_entry_reasons=open_position_entry_reasons,
            should_break=False,
        )

    def _evaluate_synthesis_round(
        self,
        *,
        state: _DesignAttemptState,
        exec_result: StrategyRunResult,
        market_data: Dict[str, List[OHLCVBar]],
        config: BacktestConfig,
        round_num: int,
        ran_on_non_conforming_code: bool,
        all_gate_results: List[QualityGateResult],
        refinement_attempts: List[str],
        zero_trade_attempts: List[str],
        emit: PhaseCallback,
        stall_tracker: RefinementStallTracker,
        drift_collector: Optional[_DriftCollector],
    ) -> _SynthesisEvaluateResult:
        """Compute metrics, run the anomaly gates, and route any recovery.

        Pre: this round executed cleanly and collected ``trades`` through the
        coverage gate; ``state`` carries this round's settled ``spec``/``code``/
        ``trades`` (``state.metrics`` is unused — this method computes its own
        metrics from ``state.trades``); ``ran_on_non_conforming_code`` is the
        verdict captured at trade collection.
        Post: returns a ``_SynthesisEvaluateResult``. ``action="success"`` when
        no critical anomaly fired (the caller marks ``execution_succeeded``);
        otherwise ``_handle_critical_anomalies`` runs and the result carries the
        recovered ``spec``/``code``/``trades``/``metrics``/``exec_result`` with
        ``action="continue"`` (retry next round) or ``"exhausted"`` (budget
        spent). ``ran_on_non_conforming_code`` is replaced only when a committed
        repair supplied a fresh verdict (non-``None``). Records the anomaly
        gates onto ``all_gate_results`` in place.
        """
        # Local import — same deferred-import rationale as
        # ``_cached_run_strategy_code`` above: keeps test monkeypatches of
        # ``orchestrator.compute_metrics`` honored.
        from . import orchestrator as _orchestrator_module

        spec, code, trades = state.spec, state.code, state.trades

        metrics = _orchestrator_module.compute_metrics(
            trades, config.initial_capital, config.start_date, config.end_date
        )
        # ``compute_metrics`` builds from the trade ledger alone; carry the
        # engine's exit-rule firing counters and cost-stress sweep rows from
        # this run onto ``metrics`` so the downstream
        # ``ExitRuleConformanceGate`` / ``CostStressRealismGate`` see the
        # payloads ``run_backtest`` produced.
        _attach_execution_diagnostics(metrics=metrics, exec_result=exec_result)

        _maybe_attach_coverage_report(
            metrics=metrics,
            spec=spec,
            market_data=market_data,
            config=config,
            exec_result=exec_result,
        )

        anomaly_gates = self._check_anomalies_cached(
            metrics,
            trades,
            dsr_aware=config.walk_forward_enabled,
            diagnostics=exec_result.execution_diagnostics,
            coverage_report=metrics.coverage_report,
            market_data=market_data,
            phase="synthesis",
        )
        self.record_gates(anomaly_gates, all_gate_results, refinement_round=round_num)

        critical_anomalies = _critical_failures(anomaly_gates)
        if critical_anomalies:
            recovery = self._handle_critical_anomalies(
                state=_DesignAttemptState(spec=spec, code=code, trades=trades, metrics=metrics),
                exec_result=exec_result,
                market_data=market_data,
                config=config,
                critical_anomalies=critical_anomalies,
                all_gate_results=all_gate_results,
                refinement_attempts=refinement_attempts,
                zero_trade_attempts=zero_trade_attempts,
                round_num=round_num,
                emit=emit,
                stall_tracker=stall_tracker,
                drift_collector=drift_collector,
            )
            spec, code = recovery.spec, recovery.code
            trades, metrics = recovery.trades, recovery.metrics
            # A committed zero-trade repair replaced the persisted trades
            # with new code; adopt its conformance verdict. The generic
            # refine path leaves the trades (and so the verdict) unchanged
            # and signals that with ``None``.
            if recovery.ran_on_non_conforming_code is not None:
                ran_on_non_conforming_code = recovery.ran_on_non_conforming_code
            exec_result = recovery.exec_result
            # Even if the code is technically correct, an exhausted cycle
            # leaves ``action="exhausted"`` so the caller keeps
            # ``execution_succeeded=False`` and ``is_winning`` stays False —
            # paper-trading must not fire on a "failed: max_refinement_rounds"
            # record.
            return _SynthesisEvaluateResult(
                action="exhausted" if recovery.exhausted else "continue",
                spec=spec,
                code=code,
                trades=trades,
                metrics=metrics,
                exec_result=exec_result,
                ran_on_non_conforming_code=ran_on_non_conforming_code,
                runtime_lookahead_violation=exec_result.error_type == "lookahead_violation",
                stalled=recovery.stalled,
            )

        return _SynthesisEvaluateResult(
            action="success",
            spec=spec,
            code=code,
            trades=trades,
            metrics=metrics,
            exec_result=exec_result,
            ran_on_non_conforming_code=ran_on_non_conforming_code,
            runtime_lookahead_violation=exec_result.error_type == "lookahead_violation",
        )

    def _handle_critical_anomalies(
        self,
        *,
        state: _DesignAttemptState,
        exec_result: StrategyRunResult,
        market_data: Dict[str, List[OHLCVBar]],
        config: BacktestConfig,
        critical_anomalies: List[QualityGateResult],
        all_gate_results: List[QualityGateResult],
        refinement_attempts: List[str],
        zero_trade_attempts: List[str],
        round_num: int,
        emit: PhaseCallback,
        stall_tracker: RefinementStallTracker,
        drift_collector: Optional[_DriftCollector] = None,
    ) -> _AnomalyRecoveryOutcome:
        """Recover from critical backtest anomalies in the evaluation phase.

        Pre: ``critical_anomalies`` is non-empty; the caller has already
        run the anomaly detector and recorded its gates; ``state`` carries
        this round's settled ``spec``/``code``/``trades``/``metrics``;
        ``all_gate_results``, ``refinement_attempts``, ``zero_trade_attempts``
        are running lists the helper mutates in place.
        Post: returns an ``_AnomalyRecoveryOutcome``. On ``exhausted=False``
        the spec/code/trades/metrics/exec_result fields carry the new
        known-good state (either a committed zero-trade-repair proposal or
        the source the generic refinement loop produced) and the caller
        ``continue``s the synthesis loop. On ``exhausted=True`` the round
        budget is spent and the caller breaks with
        ``max_rounds_exhausted=True``.

        Strategy:
          1. If diagnostics classify the failure as ``ENTRY_WITH_NO_EXIT``
             (entries filled but engine-owned exits never fired), raise
             ``SpecImplementabilityError`` to route the cycle back to
             redesign / spec refinement. This category has no valid
             code-level repair — exits are engine-owned, a manual close is
             rejected by the conformance gate, and ``exit_rules`` spec edits
             are dropped by the ``risk_limits``-only repair whitelist — so
             only the designer (which can rewrite ``exit_rules``) can fix it.
          2. Else if diagnostics carry a ``zero_trade_category`` AND there is
             market data, ask the specialised repair agent first. A
             committed proposal has already cleared safety + fresh
             backtest + anomaly gates, so we use it directly.
          3. Otherwise (or if the repair did not commit), fall through
             to the generic refinement agent via ``_refine_or_exhaust``.
        """
        if not critical_anomalies:
            raise ValueError("_handle_critical_anomalies requires at least one critical")
        if not isinstance(market_data, dict) or not market_data:
            raise ValueError("market_data must be non-empty")

        spec, code, trades, metrics = state.spec, state.code, state.trades, state.metrics

        # ── 1: Build the failure-details prompt block (also used by generic refine) ──
        failure_details = "\n".join(f"- {g.details}" for g in critical_anomalies)
        diagnostics_block = _format_execution_diagnostics(exec_result.execution_diagnostics)
        if diagnostics_block:
            failure_details = f"{failure_details}\n{diagnostics_block}"
        coverage_block = format_coverage_report(metrics.coverage_report)
        if coverage_block:
            failure_details = f"{failure_details}\n{coverage_block}"

        # ── 2: Route a non-firing engine-owned exit to redesign / spec refinement ──
        # ``ENTRY_WITH_NO_EXIT`` (entries filled, exits never fired, positions
        # still open at window end) has no actionable code-level repair: exits
        # are engine-owned, a manual code close is rejected by the conformance
        # gate, and ``exit_rules`` spec edits are dropped by the repair
        # whitelist (``risk_limits`` only). The only real lever is the spec's
        # exit rules, which only the designer can revise — so phase back to
        # design instead of burning refinement rounds on the code-only repair
        # loop, where the agent now correctly proposes nothing.
        diag = exec_result.execution_diagnostics
        if diag is not None and diag.zero_trade_category == "ENTRY_WITH_NO_EXIT":
            emit(
                "coding",
                {
                    "sub_phase": "routed_to_redesign",
                    "refinement_round": round_num,
                    "via": "entry_with_no_exit",
                },
            )
            raise SpecImplementabilityError(
                (
                    "ENTRY_WITH_NO_EXIT: entries filled but engine-owned exits "
                    "never fired in the test window; positions remained open at "
                    "the end. No code-level repair is possible (exits are "
                    "engine-owned and exit_rules spec edits are not honoured by "
                    "the code-repair loop). Revise spec.exit_rules — loosen or "
                    "retune the stop-loss / take-profit / signal-exit rules so "
                    f"exits can fire. Diagnostics:\n{failure_details}"
                ),
                failure_phase="evaluation",
                last_spec=spec,
                last_code=code,
                drift_collector=drift_collector,
                # Deterministic re-check against unchanged code and market
                # data: resuming would reproduce this exact failure every
                # time. Only a design-level exit_rules revision can fix it.
                spec_implicated=True,
            )

        # ── 3: Specialised zero-trade repair (if diagnostics support it) ──
        if diag is not None and diag.zero_trade_category is not None:
            try:
                zt_outcome = self.zero_trade_repairer.try_repair(
                    spec=spec,
                    code=code,
                    exec_result=exec_result,
                    coverage_report=metrics.coverage_report,
                    market_data=market_data,
                    config=config,
                    zero_trade_attempts=zero_trade_attempts,
                    round_num=round_num,
                    emit=emit,
                )
            except DesignBudgetExhausted as exc:
                _annotate_budget_exhaustion(exc, spec, code=code)
                raise
            all_gate_results.extend(zt_outcome.new_gates)
            if zt_outcome.committed:
                if zt_outcome.new_spec is None:
                    raise ValueError("committed ZTR must carry new_spec")
                if zt_outcome.new_metrics is None:
                    raise ValueError("committed ZTR must carry new_metrics")
                if zt_outcome.new_exec_result is None:
                    raise ValueError("committed ZTR must carry new_exec_result")
                refinement_attempts.append(
                    f"zero-trade repair: {zt_outcome.changes_made}"
                    if zt_outcome.changes_made
                    else "zero-trade repair"
                )
                if drift_collector is not None:
                    zt_reason = zt_outcome.changes_made or "zero-trade repair"
                    drift_collector.record_spec_change(
                        phase="verification",
                        agent="ZeroTradeRepairer",
                        before_spec=spec,
                        after_spec=zt_outcome.new_spec,
                        reason=zt_reason,
                    )
                    drift_collector.record_code_change(
                        phase="verification",
                        agent="ZeroTradeRepairer",
                        before_code=code,
                        after_code=zt_outcome.new_code,
                        reason=zt_reason,
                    )
                emit(
                    "coding",
                    {
                        "sub_phase": "refined",
                        "refinement_round": round_num,
                        "changes_made": (zt_outcome.changes_made or "zero-trade repair"),
                        "via": "zero_trade_repair",
                    },
                )
                # The committed repair replaces the persisted trades/code;
                # re-check predicate conformance on it so the non-conforming flag
                # describes the repaired backtest (the repairer does not re-run
                # the gate). The caller adopts this value only on commit; the
                # generic-refine path below leaves it ``None`` (trades unchanged).
                ztr_non_conforming = self._committed_code_conformance_verdict(
                    zt_outcome.new_code,
                    zt_outcome.new_spec,
                    all_gate_results=all_gate_results,
                    refinement_round=round_num,
                    gate_name_prefix="zero_trade_repair_",
                )
                return _AnomalyRecoveryOutcome(
                    spec=zt_outcome.new_spec,
                    code=zt_outcome.new_code,
                    trades=zt_outcome.new_trades,
                    metrics=zt_outcome.new_metrics,
                    exec_result=zt_outcome.new_exec_result,
                    exhausted=False,
                    ran_on_non_conforming_code=ztr_non_conforming,
                )

        # ── 4: Generic refinement (or exhaust the round budget) ──
        new_spec, new_code, exhausted, stalled = self._refine_or_exhaust(
            spec=spec,
            code=code,
            failure_phase="evaluation",
            refine_label="evaluation (backtest anomaly)",
            failure_details=failure_details,
            metrics=metrics,
            refinement_attempts=refinement_attempts,
            round_num=round_num,
            default_change_label="anomaly fix",
            emit=emit,
            stall_tracker=stall_tracker,
            drift_collector=drift_collector,
        )
        return _AnomalyRecoveryOutcome(
            spec=new_spec,
            code=new_code,
            trades=trades,
            metrics=metrics,
            exec_result=exec_result,
            exhausted=exhausted,
            stalled=stalled,
        )
