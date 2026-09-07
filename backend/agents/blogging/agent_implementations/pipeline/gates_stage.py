"""Gates stage: validators, fact-check, compliance, rewrite loop, and finalize."""

import logging
from functools import partial
from pathlib import Path

from agents.blogging.blog_copy_editor_agent.models import FeedbackItem
from agents.blogging.blog_publication_agent.models import PublishingPack
from agents.blogging.blog_writer_agent import ReviseWriterInput
from agents.blogging.shared.artifacts import load_allowed_claims_for_brief, write_artifact
from agents.blogging.shared.content_profile import build_draft_length_instruction
from agents.blogging.shared.errors import BloggingError, ComplianceError, DraftError, FactCheckError
from agents.blogging.shared.models import BlogPhase
from agents.blogging.shared.run_pipeline_job import _is_external_cancellation
from temporalio.exceptions import CancelledError

from llm_service.interface import LLMRateLimitError, LLMTemporaryError
from shared.concurrency import parallel_map

from ._common import _load_required_guidelines, _make_update
from .constants import BRAND_SPEC_PROMPT_PATH
from .context import PipelineContext, PipelineStatus

logger = logging.getLogger(__name__)


def run_gates_stage(ctx: "PipelineContext") -> None:
    """Gates stage: validators, fact-check, compliance, rewrite loop, and finalize.

    Args:
        ctx: The shared ``PipelineContext``. Reads ``brief``, ``work_dir``,
            ``llm_client``, ``length_policy``, ``job_updater``,
            ``max_rewrite_iterations``, ``run_gates``, ``plan``,
            ``elicited_stories_text``, ``selected_title``, and ``draft_result``;
            writes the final ``draft_result`` and ``status``.
    Preconditions:
        - The draft stage populated ``ctx.draft_result``/``ctx.plan``/
          ``ctx.elicited_stories_text``. ``ctx.selected_title`` is also read — it is
          produced by the planning stage's title-selection round — so a gate-driven
          rewrite preserves the author's chosen title and the finalized publishing
          pack reflects it.
    Postconditions:
        - Sets ``ctx.draft_result`` (final) and ``ctx.status`` (PASS or
          NEEDS_HUMAN_REVIEW). Always returns None (no early aborts).
        - When ``run_gates`` is True but ``work_dir`` is None the gates cannot run
          (they persist artifacts under ``work_dir``): they are skipped with a
          ``logger.info`` and ``ctx.status`` stays PASS — a "gates requested but not
          executable" result rather than "gates passed". Callers that require gates
          to actually run must supply a ``work_dir``.
        - Runs no human-interaction round of its own: title selection happens once,
          in the planning stage. When the all-gates-pass branch is taken,
          ``publishing_pack.json`` is written with
          ``title_options = [ctx.selected_title]`` when set, else the existing
          ``[tc.title for tc in plan.title_candidates[:5]]`` fallback. The
          gates-skipped branch writes no publishing pack, matching today's
          behavior.
    Raises:
        DraftError: when gates are enabled but the guideline files required for
            gate-driven rewrites cannot be loaded, or when a rewrite iteration
            fails (phase="gates"/"draft").
        FactCheckError: when the fact-check gate fails unrecoverably.
        ComplianceError: when the compliance gate fails unrecoverably.
        BloggingError: any other blogging-domain gate failure (base class of the
            above) propagates unchanged.
        CancelledError: a Temporal-native cancellation propagates for the worker
            to observe (never swallowed here).
        LLMRateLimitError / LLMTemporaryError: a transient LLM-transport failure
            propagates unwrapped so the Temporal activity funnel can retry the
            stage instead of masking it as a domain gate failure.

    Note:
        The fact-check and compliance gates are independent given the draft and the
        deterministic validator report, so they run concurrently via ``parallel_map``
        (which copies the caller's LLM attribution/request-id contextvars into each
        worker). Validators run first because the compliance gate consumes their report.
    """
    # Deferred import: see agents.blogging.agent_implementations.pipeline._common's
    # module docstring — keeps monkeypatch.setattr(shim, "BlogComplianceAgent", ...) /
    # ("BlogFactCheckAgent", ...) / ("BlogWriterAgent", ...) /
    # ("load_brand_spec_prompt", ...) / ("run_validators_from_work_dir", ...)
    # effective now that this code lives outside the shim.
    from agents.blogging.agent_implementations.blog_writing_process_v2 import (
        BlogComplianceAgent,
        BlogFactCheckAgent,
        BlogWriterAgent,
        load_brand_spec_prompt,
        run_validators_from_work_dir,
    )

    assert ctx.draft_result is not None, (
        "run_gates_stage requires ctx.draft_result (set by the draft stage)"
    )
    brief = ctx.brief
    work_dir = ctx.work_dir
    llm_client = ctx.llm_client
    length_policy = ctx.length_policy
    job_updater = ctx.job_updater
    max_rewrite_iterations = ctx.max_rewrite_iterations
    run_gates = ctx.run_gates
    plan = ctx.plan
    elicited_stories_text = ctx.elicited_stories_text
    selected_title = ctx.selected_title
    draft_result = ctx.draft_result
    _update = _make_update(job_updater)

    status: PipelineStatus = "PASS"
    allowed_claims = None
    if work_dir is not None:
        write_artifact(work_dir, "final.md", draft_result.draft)
        logger.info("Persisted final.md")

        # Load allowed_claims.json (if present, and belonging to the current
        # brief) so the fact-check gate evaluates the draft against the same
        # list the writer was given, and so a gate-driven rewrite can keep
        # [CLAIM:id] tags valid; a missing/non-dict artifact, or a topic
        # mismatch (a stale artifact from a reused work_dir), is a no-op
        # (matches validators' handling of the same artifact). Guarded by the
        # same work_dir check as the write above: there's nothing on disk to
        # read when there's no work_dir.
        allowed_claims = load_allowed_claims_for_brief(work_dir, brief.brief)

    # Gates require a work_dir: they persist validator/fact-check/compliance
    # artifacts and drive the closed-loop rewrite off them. When gates are
    # requested without a work_dir (e.g. an in-memory run), skip them but say so
    # rather than finalizing silently as PASS.
    if run_gates and work_dir is None:
        logger.info(
            "Blog gates requested (run_gates=True) but skipped: no work_dir to "
            "persist gate artifacts. Provide work_dir to enable quality gates."
        )

    if work_dir is not None and run_gates:
        brand_spec_prompt_text = load_brand_spec_prompt(BRAND_SPEC_PROMPT_PATH)
        compliance_agent = BlogComplianceAgent(llm_client=llm_client)
        fact_check_agent = BlogFactCheckAgent(llm_client=llm_client)
        require_disclaimer_for = ["medical", "legal", "financial"]

        # Reconstruct the draft agent for gate-driven rewrites. Guideline edits made
        # during the draft stage are persisted to STYLE_GUIDE_PATH, so re-loading here
        # picks them up — this also makes the gates stage self-contained when it runs
        # as its own Temporal activity (a fresh process with no in-memory draft agent).
        writing_style_content, brand_spec_content = _load_required_guidelines(
            "run gate-driven rewrites", phase="gates"
        )
        draft_agent = BlogWriterAgent(
            llm_client=llm_client,
            writing_style_guide_content=writing_style_content,
            brand_spec_content=brand_spec_content,
        )

        # The fact-check and compliance gates are independent given the draft (and,
        # for compliance, the deterministic validator report), so they run
        # concurrently below. Each returns ``(report, error)`` — CAPTURING (not
        # raising) any failure it would otherwise raise — so that parallel_map runs
        # BOTH gates to completion before the stage propagates a failure. That drain
        # matters because both gates persist artifacts (fact_check_report.json /
        # compliance_report.json) into the same work_dir: if one raised while the
        # other was still running, parallel_map's fast-fail would abandon the running
        # worker, which could later overwrite the report from a subsequent
        # retry/rewrite. The captured error is Temporal cancellation, BloggingError,
        # or a transient LLM-transport error (propagated unwrapped so the Temporal
        # activity funnel can retry the stage — see temporal.activities._run_stage),
        # or any other failure mapped to the gate's domain error type.
        #
        # These are nested (not module-level) deliberately: they take only the
        # per-iteration draft/validator report as parameters — so they never close
        # over the `rewrite_iter` loop variable — and intentionally close over the
        # loop-INVARIANT collaborators built once above (the agents,
        # require_disclaimer_for, work_dir, brand_spec_prompt_text, _update). That
        # closure is accepted for conciseness; the gates' behavior is covered
        # end-to-end via run_pipeline in test_run_pipeline_gates.py.
        def _fact_check_gate(draft: str):
            """Run the fact-check gate, capturing (not raising) its outcome.

            Preconditions:
                - ``draft`` is the current draft text to check.
            Postconditions:
                - Returns ``(FactCheckReport, None)`` on success, or ``(None, error)`` on
                  failure — CAPTURING every failure so ``parallel_map`` runs the sibling
                  gate to completion instead of fast-failing (see the block comment above).
                - The captured ``error`` preserves its class: ``BloggingError``,
                  ``CancelledError``, and transient ``LLMRateLimitError``/``LLMTemporaryError``
                  pass through unwrapped (for cancellation/Temporal-retry handling); an
                  external cancellation surfacing as another type is passed through too;
                  any other exception is wrapped in ``FactCheckError``.
            """
            # Both gates report progress under BlogPhase.FACT_CHECK — the umbrella phase
            # for this concurrent step — so the two callbacks don't flip the UI phase
            # back and forth between FACT_CHECK and COMPLIANCE while they run together.
            try:
                report = fact_check_agent.run(
                    draft,
                    allowed_claims=allowed_claims,
                    require_disclaimer_for=require_disclaimer_for,
                    work_dir=work_dir,
                    on_llm_request=lambda msg: _update(BlogPhase.FACT_CHECK, status_text=msg),
                )
                return report, None
            except (BloggingError, CancelledError, LLMRateLimitError, LLMTemporaryError) as e:
                return None, e
            except Exception as e:
                if _is_external_cancellation(e):
                    return None, e
                return None, FactCheckError(f"Fact check failed: {e}", cause=e)

        def _compliance_gate(draft: str, validator_report):
            """Run the compliance gate, capturing (not raising) its outcome.

            Preconditions:
                - ``draft`` is the current draft text; ``validator_report`` is the
                  deterministic validator result (a Pydantic model, or a stand-in that
                  the ``model_dump`` guard tolerates) that compliance consumes.
            Postconditions:
                - Returns ``(ComplianceReport, None)`` on success, or ``(None, error)`` on
                  failure — capturing every failure (same rationale as ``_fact_check_gate``).
                - The captured ``error`` preserves its class: ``BloggingError``,
                  ``CancelledError``, and transient LLM errors pass through unwrapped, an
                  external cancellation surfacing as another type is passed through, and
                  any other exception is wrapped in ``ComplianceError``.
            """
            # Reports progress under BlogPhase.FACT_CHECK too — see _fact_check_gate; the
            # umbrella phase keeps the concurrent gates from flip-flopping the UI phase.
            try:
                report = compliance_agent.run(
                    draft,
                    brand_spec_prompt=brand_spec_prompt_text,
                    # validator_report is normally a Pydantic model, but the
                    # hasattr guard tolerates plain-object stand-ins from test
                    # doubles / legacy validator paths (passes None if absent).
                    validator_report=validator_report.model_dump()
                    if hasattr(validator_report, "model_dump")
                    else None,
                    work_dir=work_dir,
                    on_llm_request=lambda msg: _update(BlogPhase.FACT_CHECK, status_text=msg),
                )
                return report, None
            except (BloggingError, CancelledError, LLMRateLimitError, LLMTemporaryError) as e:
                return None, e
            except Exception as e:
                if _is_external_cancellation(e):
                    return None, e
                return None, ComplianceError(f"Compliance check failed: {e}", cause=e)

        for rewrite_iter in range(max_rewrite_iterations):
            _update(
                BlogPhase.FACT_CHECK,
                sub_progress=rewrite_iter / max_rewrite_iterations,
                status_text=f"Running fact-check + compliance (iteration {rewrite_iter + 1})...",
                rewrite_iterations=rewrite_iter,
            )

            # Deterministic validators run first (non-LLM, and the compliance gate
            # consumes their report). Their failures map to FactCheckError as before.
            # Pass the already topic-matched allowed_claims through explicitly so
            # validators' claims-policy check agrees with the fact-check gate above
            # instead of independently re-reading (and possibly disagreeing with)
            # work_dir/allowed_claims.json.
            try:
                validator_report = run_validators_from_work_dir(
                    work_dir, allowed_claims=allowed_claims
                )
            except BloggingError:
                raise
            except CancelledError:
                raise
            except Exception as e:
                if _is_external_cancellation(e):
                    raise
                raise FactCheckError(f"Fact check failed: {e}", cause=e) from e

            # Fan the two independent LLM gates out concurrently. parallel_map copies
            # this thread's context into each worker so the LLM attribution /
            # request-id contextvars propagate (a raw ThreadPoolExecutor would not;
            # see llm_service.attribution). partial() binds the current draft /
            # validator report eagerly; preserve_order keeps [fact, compliance]
            # positional; skip_none=False because each gate always returns a
            # (report, error) tuple. Because the gates capture rather than raise,
            # parallel_map never fast-fails — both run to completion before we
            # propagate any failure (no abandoned worker; see the gate comment above).
            (fact_report, fact_error), (compliance_report, compliance_error) = parallel_map(
                [
                    partial(_fact_check_gate, draft_result.draft),
                    partial(_compliance_gate, draft_result.draft, validator_report),
                ],
                lambda gate: gate(),
                max_workers=2,
                preserve_order=True,
                skip_none=False,
            )

            # Both gates have finished; propagate a failure (if any) with a fixed
            # precedence: cancellation first, then a transient LLM-transport error
            # (prefer a Temporal stage retry over a terminal domain failure), then the
            # fact-check domain error, then the compliance one (input order).
            #
            # The three passes are deliberate: each pass scans BOTH gates for a
            # higher-priority error class before falling through, so cancellation from
            # *either* gate wins over a transient from the other, which in turn wins over
            # any domain error — a single positional pass could not express that ordering.
            gate_errors = [e for e in (fact_error, compliance_error) if e is not None]
            # Only one error is raised (by the precedence below), so when BOTH gates
            # failed, log every error first — otherwise the lower-precedence failure
            # would be silently discarded and never reach the logs.
            if len(gate_errors) > 1:
                logger.error(
                    "Both gates failed on rewrite iteration %s; raising by precedence, "
                    "all gate errors: %s",
                    rewrite_iter + 1,
                    [f"{type(e).__name__}: {e}" for e in gate_errors],
                )
            for gate_error in gate_errors:
                if isinstance(gate_error, CancelledError) or _is_external_cancellation(gate_error):
                    raise gate_error
            for gate_error in gate_errors:
                if isinstance(gate_error, (LLMRateLimitError, LLMTemporaryError)):
                    raise gate_error
            if gate_errors:
                raise gate_errors[0]

            all_pass = (
                validator_report.status == "PASS"
                and fact_report.claims_status == "PASS"
                and fact_report.risk_status == "PASS"
                and compliance_report.status == "PASS"
            )
            if all_pass:
                status = "PASS"
                logger.info("All gates PASS on rewrite iteration %s", rewrite_iter + 1)

                _update(
                    BlogPhase.FINALIZE,
                    sub_progress=0.5,
                    status_text="Finalizing...",
                )

                # Title selection now runs once, in the planning stage, before the
                # draft is written; this only sources the pack from that choice.
                title_options = (
                    [selected_title]
                    if selected_title
                    else [tc.title for tc in plan.title_candidates[:5]]
                )
                pack = PublishingPack(
                    title_options=title_options,
                    meta_description=draft_result.draft[:155].strip() or None,
                    tags=[],
                )
                write_artifact(work_dir, "publishing_pack.json", pack.model_dump())
                logger.info("Wrote publishing_pack.json")

                _update(
                    BlogPhase.FINALIZE,
                    sub_progress=1.0,
                    status_text="Pipeline complete - all checks passed",
                )
                break

            if rewrite_iter >= max_rewrite_iterations - 1:
                status = "NEEDS_HUMAN_REVIEW"
                logger.warning(
                    "Max rewrite iterations (%s) reached; status=NEEDS_HUMAN_REVIEW",
                    max_rewrite_iterations,
                )
                _update(
                    BlogPhase.FINALIZE,
                    sub_progress=1.0,
                    status_text=f"Needs human review after {max_rewrite_iterations} rewrite attempts",
                )
                break

            # Rewrite loop
            _update(
                BlogPhase.REWRITE_LOOP,
                sub_progress=(rewrite_iter + 1) / max_rewrite_iterations,
                status_text=f"Rewriting to address issues (iteration {rewrite_iter + 1}/{max_rewrite_iterations})...",
                rewrite_iterations=rewrite_iter + 1,
            )

            # --- Build feedback from ALL gates ---
            feedback_items: list[FeedbackItem] = []

            # 1. Validator failed checks
            if validator_report.status == "FAIL":
                for check in validator_report.checks:
                    if check.status == "FAIL":
                        details_str = ""
                        if check.details:
                            if "matches" in check.details:
                                details_str = (
                                    f" Found: {', '.join(str(m) for m in check.details['matches'])}"
                                )
                            elif "violations" in check.details:
                                details_str = f" Violations: {', '.join(str(v) for v in check.details['violations'])}"
                            elif "fk_grade" in check.details:
                                details_str = f" FK grade: {check.details['fk_grade']}"
                        feedback_items.append(
                            FeedbackItem(
                                category="validator",
                                severity="must_fix",
                                location=None,
                                issue=f"Validator check '{check.name}' failed.{details_str}",
                                suggestion=f"Fix the '{check.name}' violation identified by the deterministic validator.",
                            )
                        )

            # 2. Fact-check failures
            if fact_report.claims_status == "FAIL" or fact_report.risk_status == "FAIL":
                for flag in fact_report.risk_flags:
                    feedback_items.append(
                        FeedbackItem(
                            category="fact_check",
                            severity="must_fix",
                            location=None,
                            issue=f"Risk flag: {flag}",
                            suggestion=f"Address risk flag: {flag}",
                        )
                    )
                for disclaimer in fact_report.required_disclaimers:
                    feedback_items.append(
                        FeedbackItem(
                            category="fact_check",
                            severity="must_fix",
                            location=None,
                            issue=f"Missing required disclaimer: {disclaimer}",
                            suggestion=f"Add disclaimer: {disclaimer}",
                        )
                    )

            # 3. Compliance fixes
            for fix in compliance_report.required_fixes:
                feedback_items.append(
                    FeedbackItem(
                        category="compliance",
                        severity="must_fix",
                        location=None,
                        issue=fix,
                        suggestion=fix,
                    )
                )

            if not feedback_items:
                feedback_items = [
                    FeedbackItem(
                        category="compliance",
                        severity="must_fix",
                        location=None,
                        issue="Validator, fact-check, or compliance check failed; see reports for details.",
                        suggestion="Address all violations from validator_report.json, fact_check_report.json, and compliance_report.json.",
                    )
                ]

            # Build a summary reflecting all gate failures
            gate_failures = []
            if validator_report.status == "FAIL":
                failed_checks = [c.name for c in validator_report.checks if c.status == "FAIL"]
                gate_failures.append(f"Validator FAIL ({', '.join(failed_checks)})")
            if fact_report.claims_status == "FAIL" or fact_report.risk_status == "FAIL":
                gate_failures.append(
                    f"Fact-check FAIL (claims={fact_report.claims_status}, risk={fact_report.risk_status})"
                )
            if compliance_report.status == "FAIL":
                gate_failures.append(
                    f"Compliance FAIL ({len(compliance_report.violations)} violations)"
                )
            feedback_summary = "; ".join(gate_failures) if gate_failures else "Gates failed"

            try:
                revise_input = ReviseWriterInput(
                    draft=draft_result.draft,
                    feedback_items=feedback_items,
                    feedback_summary=feedback_summary,
                    content_plan=plan,
                    audience=brief.audience,
                    tone_or_purpose=brief.tone_or_purpose,
                    target_word_count=length_policy.target_word_count,
                    length_guidance=build_draft_length_instruction(length_policy),
                    selected_title=selected_title,
                    elicited_stories=elicited_stories_text or None,
                    allowed_claims=allowed_claims,
                )
                draft_output_path = Path(work_dir) / f"draft_rewrite_{rewrite_iter + 1}.md"
                draft_result = draft_agent.revise(
                    revise_input,
                    on_llm_request=lambda msg: _update(BlogPhase.REWRITE_LOOP, status_text=msg),
                    draft_output_path=draft_output_path,
                    work_dir=work_dir,
                    iteration=rewrite_iter + 1,
                )
            except (BloggingError, CancelledError, LLMRateLimitError, LLMTemporaryError):
                # Transient LLM-transport errors propagate unwrapped for Temporal retry.
                raise
            except Exception as e:
                if _is_external_cancellation(e):
                    raise
                raise DraftError(
                    f"Rewrite revision failed: {e}", iteration=rewrite_iter + 1, cause=e
                ) from e

            write_artifact(work_dir, "final.md", draft_result.draft)
            logger.info("Rewrite iteration %s: applied fixes, re-running gates", rewrite_iter + 1)
    else:
        # Gates skipped — title selection already ran in the planning stage.
        _update(
            BlogPhase.FINALIZE,
            sub_progress=1.0,
            status_text="Pipeline complete (gates skipped)",
        )

    ctx.draft_result = draft_result
    ctx.status = status
    return None
