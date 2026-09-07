"""Probe workflow for the durable-wait integration tests in
``test_temporal_signal_replay.py``.

Kept in its own module rather than inside the test file so the temporalio
workflow sandbox re-imports a plain module instead of a pytest test module when
it loads the workflow class. The module name has no ``test_`` prefix, so pytest
does not collect it.

Preconditions:
    - ``backend/agents`` and ``backend`` are on ``sys.path`` (the ``shared_*``
      convention).
Postconditions:
    - Importing has no side effects beyond class/constant definition.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from temporalio import workflow

with workflow.unsafe.imports_passed_through():
    from shared.hitl.temporal_signal import HitlAnswerSignalMixin

#: Dedicated queue so a probe worker never picks up a real team's tasks.
WAIT_PROBE_TASK_QUEUE = "shared-hitl-wait-probe"


@workflow.defn(name="HitlWaitProbeWorkflow")
class HitlWaitProbeWorkflow(HitlAnswerSignalMixin):
    """The smallest workflow that exercises ``wait_for_answers`` end to end.

    Invariants:
        - Composes ``HitlAnswerSignalMixin`` and nothing else, so a test failure
          here is attributable to the mixin rather than to surrounding workflow
          logic. It defines no ``__init__``, so the mixin's runs.
        - Schedules no activities: the only history this workflow produces is
          start, workflow tasks, the signal, and completion — which is exactly
          the history a replay-determinism check should be reading.
    """

    @workflow.run
    async def run(self, resume_token: str) -> Optional[List[Dict[str, Any]]]:
        """Park on ``resume_token`` and return whatever answers resume the wait.

        Preconditions:
            - ``resume_token`` is a non-empty ``str`` (enforced downstream by
              ``wait_for_answers``).
        Postconditions:
            - Returns only after a validated, token-matching ``submit_answers``
              signal lands; returns that batch. Never completes otherwise —
              there is no timeout and no default.
        """
        return await self.wait_for_answers(resume_token)
