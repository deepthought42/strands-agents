"""Replay-safety coverage for ``HitlAnswerSignalMixin.wait_for_answers``.

``test_temporal_signal.py`` proves the wait's SHAPE against a monkeypatched
``workflow.wait_condition``. This file proves the property that shape exists
for: the wait is durable, so a worker that dies while a workflow is parked can
be replaced by a fresh worker that rebuilds the pause purely from replayed
history and resumes correctly.

The two ``WorkflowEnvironment`` tests are ``@pytest.mark.integration``, so
``backend/conftest.py`` skips them unless pytest is invoked with
``-m integration`` -- the same status every other ``WorkflowEnvironment`` test
in this repo has. They additionally ``pytest.skip`` when the ephemeral Temporal
test-server binary cannot be downloaded (see
``shared.temporal.testing.workflow_environment``). The structural test at the
bottom is NOT integration-marked: it runs in the ordinary suite and pins that
the probe workflow is a well-formed ``@workflow.defn`` composing the mixin, so
a broken probe surfaces even where the server is unreachable.
"""

from __future__ import annotations

import asyncio
import contextlib

import pytest

from shared.hitl.temporal_signal import SUBMIT_ANSWERS_SIGNAL
from shared.hitl.tests._wait_probe_workflow import WAIT_PROBE_TASK_QUEUE, HitlWaitProbeWorkflow

RESUME_TOKEN = "probe-job-1:abc123"
ANSWERS = [{"question_id": "q1", "selected_option_id": "yes", "other_text": None}]


@contextlib.asynccontextmanager
async def _probe_worker(env):
    """Run a probe worker against an already-started ``env``.

    Preconditions:
        - ``env`` is a live ``WorkflowEnvironment``.
    Postconditions:
        - Yields once the worker is polling ``WAIT_PROBE_TASK_QUEUE``. Exiting
          stops that worker WITHOUT shutting down ``env``, so a later call can
          start a replacement worker against the same environment -- which is
          how the worker-restart test below simulates a worker dying mid-pause.
        - ``max_cached_workflows=0`` disables the sticky cache, so a replacement
          worker rebuilds workflow state by replaying history from event 1
          rather than resuming from an in-memory snapshot. That is what makes
          this a replay test and not merely a reconnect test.
    """
    from temporalio.worker import Worker

    async with Worker(
        env.client,
        task_queue=WAIT_PROBE_TASK_QUEUE,
        workflows=[HitlWaitProbeWorkflow],
        max_cached_workflows=0,
    ) as worker:
        yield worker


async def _wait_until_parked(handle, *, timeout_s: float = 10.0) -> None:
    """Block until history shows the workflow has processed a task and parked.

    Preconditions:
        - ``handle`` is a live workflow handle; ``timeout_s`` is positive.
    Postconditions:
        - Returns once history contains a ``WORKFLOW_TASK_COMPLETED`` event (the
          task in which ``run`` reached ``wait_condition``). Raises
          ``TimeoutError`` naming the observed event types otherwise.
    """
    assert timeout_s > 0, "timeout_s must be positive"

    from temporalio.api.enums.v1 import EventType

    deadline = asyncio.get_running_loop().time() + timeout_s
    while True:
        events = list((await handle.fetch_history()).events)
        if any(e.event_type == EventType.EVENT_TYPE_WORKFLOW_TASK_COMPLETED for e in events):
            return
        if asyncio.get_running_loop().time() >= deadline:
            raise TimeoutError(
                f"timed out waiting for the probe to park; history event_type ints={[int(e.event_type) for e in events]}"
            )
        await asyncio.sleep(0.05)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_wait_survives_a_worker_restart_and_replays_deterministically() -> None:
    """The acceptance criterion this story exists for: kill the worker while the
    workflow is parked, deliver the answer with NO worker running at all, then
    start a fresh worker. It must rebuild the pause from replayed history and
    resume with the real answers -- not restart, not default, not hang."""
    from temporalio.worker import Replayer

    from shared.temporal.testing import workflow_environment

    async with workflow_environment() as env:
        async with _probe_worker(env):
            handle = await env.client.start_workflow(
                HitlWaitProbeWorkflow.run,
                RESUME_TOKEN,
                id="hitl-wait-probe-worker-restart",
                task_queue=WAIT_PROBE_TASK_QUEUE,
            )
            await _wait_until_parked(handle)
        # Worker A is gone. The signal is durable server-side, so it is recorded
        # into history with nothing running to observe it -- the buffered/armed
        # distinction now has to survive purely as replayed state.
        await handle.signal(SUBMIT_ANSWERS_SIGNAL, {"resume_token": RESUME_TOKEN, "answers": ANSWERS})

        async with _probe_worker(env):
            # Auto time-skipping would race past an unbounded wait_condition to
            # the run timeout before the replacement worker finishes replaying.
            with env.auto_time_skipping_disabled():
                result = await asyncio.wait_for(handle.result(), timeout=30)
            history = await handle.fetch_history()

    assert result == ANSWERS
    await Replayer(workflows=[HitlWaitProbeWorkflow]).replay_workflow(history)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_a_signal_that_beats_the_wait_is_not_lost() -> None:
    """The signal-before-wait race against a real server: signal-with-start
    delivers the answer in the same history event batch that starts the run, so
    the handler sees it with no pause armed and buffers it. The wait must drain
    that buffer rather than parking for a signal that already came and went."""
    from temporalio.worker import Replayer

    from shared.temporal.testing import workflow_environment

    async with workflow_environment() as env:
        async with _probe_worker(env):
            handle = await env.client.start_workflow(
                HitlWaitProbeWorkflow.run,
                RESUME_TOKEN,
                id="hitl-wait-probe-early-signal",
                task_queue=WAIT_PROBE_TASK_QUEUE,
                start_signal=SUBMIT_ANSWERS_SIGNAL,
                start_signal_args=[{"resume_token": RESUME_TOKEN, "answers": ANSWERS}],
            )
            with env.auto_time_skipping_disabled():
                result = await asyncio.wait_for(handle.result(), timeout=30)
            history = await handle.fetch_history()

    assert result == ANSWERS
    await Replayer(workflows=[HitlWaitProbeWorkflow]).replay_workflow(history)


def test_probe_workflow_is_a_well_formed_defn_composing_the_mixin() -> None:
    """Not integration-marked on purpose: the two tests above cannot run without
    the ephemeral test-server binary, so without this the probe class would be
    unexercised anywhere the download is blocked. Pins that it is a real
    ``@workflow.defn``, registers ``submit_answers`` through the mixin, and
    initializes the mixin's state (it defines no ``__init__`` of its own)."""
    from shared.hitl.testing import assert_workflow_registers_submit_answers, get_workflow_definition

    assert_workflow_registers_submit_answers(HitlWaitProbeWorkflow)

    defn = get_workflow_definition(HitlWaitProbeWorkflow)
    assert defn.name == "HitlWaitProbeWorkflow"
    assert defn.run_fn.__name__ == "run"

    wf = HitlWaitProbeWorkflow()
    assert wf._active_resume_token is None
    assert wf._submitted_answers is None
    assert wf._buffered_signals == {}
    assert callable(wf.wait_for_answers)
