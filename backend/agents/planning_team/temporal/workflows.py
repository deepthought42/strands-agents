"""Temporal workflow for the Planning team.

``PlanningWorkflow`` is the durable orchestrator: it drives the same phase
sequence as ``planning_team.orchestrator.run_workflow`` (intake → discovery →
requirements → optional market research → synthesis → document production →
optional sub-agent provisioning → finalize), but each phase is its own
``@activity.defn`` (see :mod:`.activities`) so Temporal records, times out, and
retries every phase independently instead of one opaque black-box activity.

The workflow body is deterministic: it only threads a JSON-native ``context``
dict from one ``workflow.execute_activity`` call to the next and branches on the
run flags — no I/O, no ``os.getenv``, no time/randomness. Activity/constant
imports are kept under ``workflow.unsafe.imports_passed_through()`` so the
temporalio sandbox reuses the already-imported modules rather than re-executing
them during workflow registration.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any, Dict, Optional

from temporalio import workflow
from temporalio.common import RetryPolicy

with workflow.unsafe.imports_passed_through():
    from planning_team.temporal import activities as _activities
    from planning_team.temporal.constants import (
        RETRYABLE_MAX_ATTEMPTS,
        SINGLE_ATTEMPT,
        TASK_QUEUE,
    )
    from shared.hitl.temporal_signal import HitlAnswerSignalMixin

# --- Per-phase timeouts -----------------------------------------------------
#: Deterministic/cheap phases (intake, synthesis, finalize).
QUICK_TIMEOUT = timedelta(minutes=5)
#: LLM phases (discovery, requirements) + the optional market-research call —
#: a large spec becomes many per-section LLM round-trips.
LLM_TIMEOUT = timedelta(hours=1)
#: Long external-poll phases (document production's PRA wait, sub-agent
#: provisioning's AI-Systems wait). The PRA poll alone can run up to an hour.
EXTERNAL_TIMEOUT = timedelta(hours=2)
#: Heartbeat window for the external-poll phases (their activities emit a
#: background heartbeat every 30s).
HEARTBEAT_TIMEOUT = timedelta(minutes=5)

# --- Per-phase retry policies ----------------------------------------------
#: Idempotent phases are safe to retry: intake/synthesis/finalize (deterministic)
#: and discovery/requirements (pure LLM extraction — they write nothing and submit
#: nothing, so a transient LLM/network blip the llm_service failover doesn't absorb
#: should not fail the whole plan). ``maximum_attempts`` is shared with the
#: activities' final-attempt check via ``RETRYABLE_MAX_ATTEMPTS``.
SAFE_RETRY = RetryPolicy(
    maximum_attempts=RETRYABLE_MAX_ATTEMPTS,
    initial_interval=timedelta(seconds=5),
    maximum_interval=timedelta(minutes=1),
    backoff_coefficient=2.0,
)
#: Non-idempotent phases run once: market research submits a research request, and
#: document production writes files + submits a PRA job, so a workflow-level retry
#: must not re-run them. A failure surfaces as a failed workflow + FAILED job row
#: for explicit resubmission rather than being auto-retried.
NO_RETRY = RetryPolicy(maximum_attempts=SINGLE_ATTEMPT)

# --- Legacy single-activity path (rollout compatibility only) --------------
#: Marker gating the per-phase sequence. New executions record it and take the
#: per-phase path; a PlanningWorkflow history recorded before this patch has no
#: marker, so it replays the legacy single-activity path below and completes.
_PER_PHASE_PATCH = "planning-per-phase-activities"
#: These MUST match byte-for-byte what pre-migration histories recorded for the
#: ``run_planning_activity`` schedule, or replay is non-deterministic.
LEGACY_WORKFLOW_TIMEOUT = timedelta(hours=12)
LEGACY_RETRY_POLICY = RetryPolicy(
    maximum_attempts=3,
    initial_interval=timedelta(seconds=30),
    maximum_interval=timedelta(minutes=2),
    backoff_coefficient=2.0,
)


@workflow.defn(name="PlanningWorkflow")
class PlanningWorkflow(HitlAnswerSignalMixin):
    """Durable, per-phase Planning orchestrator.

    Invariants:
        - Inherits ``HitlAnswerSignalMixin``, registering the ``submit_answers``
          Temporal signal (``shared.hitl.temporal_signal``) and its backing
          buffer/reject/accept state machine. This registration is currently
          unconsumed: no phase below arms a pause or reads
          ``self._submitted_answers`` yet — durably waiting on a pause and
          resuming with the delivered answers is separate, follow-on work.
        - The mixin now also carries the wait half
          (``HitlAnswerSignalMixin.wait_for_answers``), but ``run()`` below
          still never awaits it, so the registration stays dormant end to end:
          arming a pause and resuming with the delivered answers is separate,
          follow-on work.
        - Neither the signal registration nor the inherited wait method needed
          a ``workflow.patched`` gate, despite this class already carrying
          ``_PER_PHASE_PATCH`` for pre-migration histories: the handler only
          mutates in-memory mixin state (``_active_resume_token``/
          ``_submitted_answers``/``_buffered_signals``) that ``run()`` never
          reads, and a method nothing awaits schedules no commands, so
          replaying any open history — with or without a ``submit_answers``
          event in it — produces identical activity-scheduling decisions
          either way.
          A patch gate becomes REQUIRED the moment ``run()`` first awaits
          ``wait_for_answers``: that is when a history recorded before the
          pause existed would start replaying against a different command
          sequence. Add the gate in that change, not before.
    """

    @workflow.run
    async def run(
        self,
        job_id: str,
        repo_path: str,
        client_name: Optional[str],
        initial_brief: Optional[str],
        spec_content: Optional[str],
        use_product_analysis: bool,
        use_market_research: bool,
    ) -> Dict[str, Any]:
        """Run one Planning job phase by phase, each phase a separate activity.

        Preconditions:
            - ``job_id`` refers to a job already created in the job store (the API
              endpoint calls ``create_job`` before dispatch); ``repo_path`` is the
              resolved workspace; at least one of ``initial_brief``/``spec_content``
              is set.

        Postconditions:
            - Threads the ``context`` dict through the per-phase activities in the
              same order and branches as the in-process orchestrator, and returns
              the finalize activity's ``{"success": True, "summary": ...}``. Each
              activity owns its own job-store progress writes and marks the job
              FAILED (then re-raises) on its own error, so a phase failure fails
              this workflow at that specific activity rather than re-running the
              whole plan.
            - A PlanningWorkflow execution started before the per-phase migration
              (its history schedules ``run_planning_activity`` first) replays the
              legacy single-activity path via the ``workflow.patched`` gate, so it
              stays deterministic and completes after a worker rolls forward.
        """
        # TODO: Remove this legacy branch, the `_PER_PHASE_PATCH` gate, and
        # `run_planning_activity` once no pre-migration PlanningWorkflow histories
        # remain open (confirm via the Temporal UI that no open executions predate
        # the deploy that introduced this patch); then deprecate the patch marker
        # with `workflow.deprecate_patch(_PER_PHASE_PATCH)` before deleting it.
        if not workflow.patched(_PER_PHASE_PATCH):
            # Legacy path: reached only when replaying a history recorded before the
            # per-phase migration. Reproduce the old single coarse activity exactly.
            await workflow.execute_activity(
                _activities.run_planning_activity,
                args=[
                    job_id,
                    repo_path,
                    client_name,
                    initial_brief,
                    spec_content,
                    use_product_analysis,
                    use_market_research,
                ],
                task_queue=TASK_QUEUE,
                schedule_to_close_timeout=LEGACY_WORKFLOW_TIMEOUT,
                retry_policy=LEGACY_RETRY_POLICY,
            )
            return {"success": True, "summary": "Planning completed (legacy path)."}

        context = await workflow.execute_activity(
            _activities.intake_activity,
            args=[job_id, repo_path, client_name, initial_brief, spec_content],
            task_queue=TASK_QUEUE,
            start_to_close_timeout=QUICK_TIMEOUT,
            retry_policy=SAFE_RETRY,
        )

        context = await workflow.execute_activity(
            _activities.discovery_activity,
            args=[job_id, context],
            task_queue=TASK_QUEUE,
            start_to_close_timeout=LLM_TIMEOUT,
            retry_policy=SAFE_RETRY,
        )

        context = await workflow.execute_activity(
            _activities.requirements_activity,
            args=[job_id, context],
            task_queue=TASK_QUEUE,
            start_to_close_timeout=LLM_TIMEOUT,
            retry_policy=SAFE_RETRY,
        )

        market_evidence: Optional[Dict[str, Any]] = None
        if use_market_research:
            market_evidence = await workflow.execute_activity(
                _activities.market_research_activity,
                args=[job_id, context],
                task_queue=TASK_QUEUE,
                start_to_close_timeout=LLM_TIMEOUT,
                retry_policy=NO_RETRY,
            )

        context = await workflow.execute_activity(
            _activities.synthesis_activity,
            args=[job_id, context, market_evidence],
            task_queue=TASK_QUEUE,
            start_to_close_timeout=QUICK_TIMEOUT,
            retry_policy=SAFE_RETRY,
        )

        # From here ``context`` is SLIM ``{repo_path}``: document_production persists
        # the (potentially large) handoff to the job store and returns only the
        # repo path, so the handoff never crosses a Temporal boundary. The phases
        # below read the handoff from the job store when they need it — do not add a
        # phase after this that expects the full pre-document-production context.
        context = await workflow.execute_activity(
            _activities.document_production_activity,
            args=[job_id, context, use_product_analysis],
            task_queue=TASK_QUEUE,
            start_to_close_timeout=EXTERNAL_TIMEOUT,
            heartbeat_timeout=HEARTBEAT_TIMEOUT,
            retry_policy=NO_RETRY,
        )

        # capability_gap is not part of the HTTP dispatch surface (the thread path
        # never sets it either), so this phase is a fast no-op skip today; it is
        # still driven so the seam exists for a future gated caller.
        context = await workflow.execute_activity(
            _activities.sub_agent_provisioning_activity,
            args=[job_id, context, None],
            task_queue=TASK_QUEUE,
            start_to_close_timeout=EXTERNAL_TIMEOUT,
            heartbeat_timeout=HEARTBEAT_TIMEOUT,
            retry_policy=NO_RETRY,
        )

        return await workflow.execute_activity(
            _activities.finalize_planning_activity,
            args=[job_id, context],
            task_queue=TASK_QUEUE,
            start_to_close_timeout=QUICK_TIMEOUT,
            retry_policy=SAFE_RETRY,
        )
