"""Unit tests for shared.hitl.temporal_signal -- the shared ``submit_answers``
Temporal signal handler + buffer/reject/accept state machine.

Drives ``HitlAnswerSignalMixin`` directly as a plain object (no Temporal
server), the same lightweight pattern
``planning_team/tests/test_temporal_answer_signal.py`` and
``software_engineering_team/tests/test_coding_team_temporal_workflow.py`` use
for the sibling implementations this module was extracted from.

**This is the canonical suite for the mixin's standalone behavioral
contract.** ``software_engineering_team/tests/test_shared_infra_gap_coverage.py``
mirrors these same cases (see that module's docstring) purely because it's
the only one of the two CI actually collects today -- update THIS suite
first when the mixin's contract changes, then mirror the change there.
"""

from __future__ import annotations

import logging
import typing

import pytest
from temporalio.converter import value_to_type

import shared.hitl.temporal_signal as temporal_signal_module
from shared.hitl.temporal_signal import (
    _OWNED_STATE_ATTRS,
    MAX_BUFFERED_SIGNALS,
    SUBMIT_ANSWERS_SIGNAL,
    HitlAnswerSignalMixin,
)


class _Workflow(HitlAnswerSignalMixin):
    """Minimal stand-in for a real ``@workflow.defn`` class mixing this in."""


class _PriorMixinOwningTheSameAttribute:
    """Stand-in for a sibling signal mixin (e.g. PlanningAnswerSignalMixin) that
    also chains super().__init__() and owns one of the same attribute names."""

    def __init__(self) -> None:
        super().__init__()
        self._active_resume_token = None


def test_init_raises_if_a_prior_mixin_already_owns_the_same_state() -> None:
    """Composing HitlAnswerSignalMixin with another mixin that owns the same
    private attribute names (forbidden per the module docstring) must fail
    loudly at construction time -- silently overwriting the sibling's state
    would alias its signal contract onto this one instead."""

    class _Both(HitlAnswerSignalMixin, _PriorMixinOwningTheSameAttribute):
        pass

    with pytest.raises(TypeError, match="_active_resume_token"):
        _Both()


def test_init_assigns_exactly_the_owned_state_attrs() -> None:
    """Pins the guarantee _OWNED_STATE_ATTRS exists for: the attributes __init__
    actually assigns must exactly match the tuple the composition guard checks,
    so a future attribute added to one but not the other can't silently escape
    the cross-mixin conflict check."""
    wf = _Workflow()

    assert set(wf.__dict__.keys()) == set(_OWNED_STATE_ATTRS)


def _answer(question_id: str = "q1", **overrides) -> dict:
    payload = {"question_id": question_id, "selected_option_id": None, "other_text": None}
    payload.update(overrides)
    return payload


# --------------------------------------------------------------------------
# signal name
# --------------------------------------------------------------------------


def test_signal_name_is_submit_answers() -> None:
    """Reused verbatim from CodingTeamWorkflow -- SPEC-024 mandates the same
    name, not a Planning-specific one."""
    assert SUBMIT_ANSWERS_SIGNAL == "submit_answers"


# --------------------------------------------------------------------------
# submit_answers -- malformed payload rejection (fails closed)
# --------------------------------------------------------------------------


def test_submit_answers_ignores_non_dict_payload() -> None:
    """A non-dict payload (str, list, None, ...) must be dropped, never applied
    or buffered -- fail closed on any shape the validator cannot interpret."""
    wf = _Workflow()
    wf._active_resume_token = "tok-1"

    wf.submit_answers("not-a-dict")

    assert wf._submitted_answers is None
    assert wf._buffered_signals == {}


def test_submit_answers_tolerates_zero_argument_delivery() -> None:
    """Temporal invokes a signal handler as handler.fn(*decoded_args) -- a
    zero-arg delivery (e.g. an empty-args signal, or a forwarding shim that
    drops an empty payload) must bind the ``payload: Any = None`` default and
    fall through to the non-dict rejection rather than raising TypeError for
    a missing required argument, which would permanently strand the workflow
    on replay."""
    wf = _Workflow()
    wf._active_resume_token = "tok-1"

    wf.submit_answers()

    assert wf._submitted_answers is None
    assert wf._buffered_signals == {}


def test_submit_answers_ignores_non_list_answers() -> None:
    """An 'answers' value that is not a list must reject the batch rather than
    be iterated or coerced."""
    wf = _Workflow()
    wf._active_resume_token = "tok-1"

    wf.submit_answers({"resume_token": "tok-1", "answers": "nope"})

    assert wf._submitted_answers is None
    assert wf._buffered_signals == {}


def test_submit_answers_ignores_payload_missing_answers_key() -> None:
    """A payload without the 'answers' key has nothing to validate -- drop it
    rather than treat absence as an empty, acceptable batch."""
    wf = _Workflow()
    wf._active_resume_token = "tok-1"

    wf.submit_answers({"resume_token": "tok-1"})

    assert wf._submitted_answers is None
    assert wf._buffered_signals == {}


def test_submit_answers_rejects_whole_batch_on_one_malformed_answer_entry() -> None:
    """A malformed payload accepted as an answer would resume the workflow with
    fabricated content -- one bad entry must reject the entire batch, not just
    be skipped, so a resume can never proceed with a partially-validated set."""
    wf = _Workflow()
    wf._active_resume_token = "tok-1"

    wf.submit_answers(
        {
            "resume_token": "tok-1",
            "answers": [_answer("q1"), {"selected_option_id": "missing-question-id"}],
        }
    )

    assert wf._submitted_answers is None
    assert wf._buffered_signals == {}


def test_submit_answers_rejects_non_dict_answer_entry() -> None:
    """A non-dict entry inside 'answers' cannot be unpacked into
    AnswerSubmission -- reject the whole batch before any entry is applied."""
    wf = _Workflow()
    wf._active_resume_token = "tok-1"

    wf.submit_answers({"resume_token": "tok-1", "answers": ["not-a-dict"]})

    assert wf._submitted_answers is None
    assert wf._buffered_signals == {}


def test_submit_answers_rejects_malformed_batch_with_no_active_pause() -> None:
    """Payload validation runs before the buffering branch: a malformed batch
    arriving while no pause is active must be dropped, not buffered as
    garbage a later wait_for_planning_answers-style consumer would apply."""
    wf = _Workflow()

    wf.submit_answers({"resume_token": "future-tok", "answers": [{"selected_option_id": "no-question-id"}]})

    assert wf._buffered_signals == {}
    assert wf._submitted_answers is None


def test_submit_answers_rejects_answer_entry_with_non_string_keys() -> None:
    """A dict answer entry with a non-str key would raise TypeError from
    AnswerSubmission(**item) if unpacked directly -- must be rejected before
    that, not let the exception escape the handler."""
    wf = _Workflow()
    wf._active_resume_token = "tok-1"

    wf.submit_answers({"resume_token": "tok-1", "answers": [{1: "x", "question_id": "q1"}]})

    assert wf._submitted_answers is None
    assert wf._buffered_signals == {}


def test_submit_answers_rejects_answer_entry_with_unrecognized_key() -> None:
    """An unrecognized key (e.g. a misspelled field name) must reject the
    whole batch rather than silently succeed with the typo'd content
    dropped -- pydantic's default model_dump() discards unknown fields,
    which would otherwise let a sender's typo pass as a "successful"
    submission missing its actual content."""
    wf = _Workflow()
    wf._active_resume_token = "tok-1"

    wf.submit_answers({"resume_token": "tok-1", "answers": [{"question_id": "q1", "other_txt": "typo"}]})

    assert wf._submitted_answers is None
    assert wf._buffered_signals == {}


def test_submit_answers_rejects_empty_answers_list() -> None:
    """An empty batch has no content to apply -- accepting it would let a
    caller mistake 'submitted, vacuously' for 'not yet submitted' if it ever
    tests _submitted_answers for truthiness."""
    wf = _Workflow()
    wf._active_resume_token = "tok-1"

    wf.submit_answers({"resume_token": "tok-1", "answers": []})

    assert wf._submitted_answers is None
    assert wf._buffered_signals == {}


# --------------------------------------------------------------------------
# submit_answers -- accept path
# --------------------------------------------------------------------------


def test_submit_answers_sets_state_when_pause_active() -> None:
    """A token-matching submission while a pause is active is applied directly
    to _submitted_answers, not buffered -- the accept and buffer paths are
    mutually exclusive."""
    wf = _Workflow()
    wf._active_resume_token = "tok-1"

    wf.submit_answers({"resume_token": "tok-1", "answers": [_answer("q1", selected_option_id="yes")]})

    assert wf._submitted_answers == [_answer("q1", selected_option_id="yes")]
    assert wf._buffered_signals == {}


def test_submit_answers_normalizes_answer_shape_through_schema() -> None:
    """A minimal, schema-valid answer (only question_id) is normalized to the
    full AnswerSubmission field set."""
    wf = _Workflow()
    wf._active_resume_token = "tok-1"

    wf.submit_answers({"resume_token": "tok-1", "answers": [{"question_id": "q1"}]})

    assert wf._submitted_answers == [_answer("q1")]
    assert wf._buffered_signals == {}


# --------------------------------------------------------------------------
# submit_answers -- out-of-order rejection
# --------------------------------------------------------------------------


def test_submit_answers_ignores_mismatched_resume_token() -> None:
    """A submission for a pause that is not the one currently pending must not
    be applied to it -- token validation defends against a retried/duplicate
    signal resolving the wrong pause."""
    wf = _Workflow()
    wf._active_resume_token = "current-token"

    wf.submit_answers({"resume_token": "stale-token", "answers": [_answer("q1")]})

    assert wf._submitted_answers is None


def test_submit_answers_does_not_buffer_mismatched_token_while_a_pause_is_active() -> None:
    """A signal for a different pause must be discarded outright while one is
    active -- buffering it would let a stale token resolve a future pause it
    was never meant for."""
    wf = _Workflow()
    wf._active_resume_token = "current-token"

    wf.submit_answers({"resume_token": "other-token", "answers": [_answer("q1")]})

    assert wf._buffered_signals == {}
    assert wf._submitted_answers is None


def test_submit_answers_ignores_second_submission_for_same_token() -> None:
    """A double-submit (or two clients racing to answer the same pause) must not
    overwrite the first accepted batch -- first submission per token wins."""
    wf = _Workflow()
    wf._active_resume_token = "tok-1"
    first = [_answer("q1", selected_option_id="yes")]
    wf.submit_answers({"resume_token": "tok-1", "answers": first})

    wf.submit_answers({"resume_token": "tok-1", "answers": [_answer("q1", selected_option_id="no")]})

    assert wf._submitted_answers == first
    assert wf._buffered_signals == {}


# --------------------------------------------------------------------------
# submit_answers -- early-arrival buffering
# --------------------------------------------------------------------------


def test_submit_answers_buffers_signal_with_no_active_pause() -> None:
    """A well-formed signal that arrives before any pause is armed has nothing
    to apply against yet -- buffer it by resume_token so a pause armed later
    for that same token can consume it immediately."""
    wf = _Workflow()

    wf.submit_answers({"resume_token": "future-tok", "answers": [_answer("q1")]})

    assert wf._submitted_answers is None
    assert wf._buffered_signals == {"future-tok": [_answer("q1")]}


def test_submit_answers_drops_early_signal_with_no_usable_resume_token() -> None:
    """An early-arriving signal with no non-empty resume_token has nothing to
    key a buffer entry on and must be dropped, not buffered under a
    placeholder key that could never be claimed by a future pause."""
    wf = _Workflow()

    wf.submit_answers({"resume_token": "", "answers": [_answer("q1")]})
    wf.submit_answers({"answers": [_answer("q1")]})

    assert wf._buffered_signals == {}


def test_submit_answers_early_buffering_first_submission_per_token_wins() -> None:
    """Two early-arriving signals for the same not-yet-armed token are a
    double-submit/race in the buffering phase too -- the first one buffered
    must not be overwritten by a later one for the same token."""
    wf = _Workflow()
    first = [_answer("q1")]

    wf.submit_answers({"resume_token": "tok-1", "answers": first})
    wf.submit_answers({"resume_token": "tok-1", "answers": [_answer("q2")]})

    assert wf._buffered_signals == {"tok-1": first}


def test_submit_answers_buffer_evicts_oldest_token_past_cap() -> None:
    """Durable workflow state cannot grow without bound: buffering past
    MAX_BUFFERED_SIGNALS distinct tokens evicts the oldest one first."""
    wf = _Workflow()

    for i in range(MAX_BUFFERED_SIGNALS):
        wf.submit_answers({"resume_token": f"tok-{i}", "answers": [_answer("q1")]})
    assert len(wf._buffered_signals) == MAX_BUFFERED_SIGNALS
    assert "tok-0" in wf._buffered_signals

    wf.submit_answers({"resume_token": "tok-overflow", "answers": [_answer("q1")]})

    assert len(wf._buffered_signals) == MAX_BUFFERED_SIGNALS
    assert "tok-0" not in wf._buffered_signals
    assert "tok-overflow" in wf._buffered_signals
    assert "tok-1" in wf._buffered_signals


def test_submit_answers_buffering_an_already_present_token_does_not_evict() -> None:
    """Re-signaling an already-buffered token is a no-op (first-writer-wins) and
    must not itself count toward/trigger cap eviction."""
    wf = _Workflow()
    for i in range(MAX_BUFFERED_SIGNALS):
        wf.submit_answers({"resume_token": f"tok-{i}", "answers": [_answer("q1")]})

    wf.submit_answers({"resume_token": "tok-0", "answers": [_answer("q2")]})

    assert len(wf._buffered_signals) == MAX_BUFFERED_SIGNALS
    assert wf._buffered_signals["tok-0"] == [_answer("q1")]


def test_submit_answers_does_not_raise_if_max_buffered_signals_is_ever_zero(monkeypatch) -> None:
    """Defensive: if MAX_BUFFERED_SIGNALS were ever 0 (or negative), the eviction
    guard's len(...) >= MAX_BUFFERED_SIGNALS check would be true against an
    empty buffer -- next(iter(self._buffered_signals)) must not then raise
    StopIteration, which would violate the handler's never-raise contract.
    Unreachable with today's positive constant; pins the hardening."""
    monkeypatch.setattr(temporal_signal_module, "MAX_BUFFERED_SIGNALS", 0)
    wf = _Workflow()

    wf.submit_answers({"resume_token": "tok-1", "answers": [_answer("q1")]})

    assert wf._buffered_signals == {"tok-1": [_answer("q1")]}


# --------------------------------------------------------------------------
# replay-safety: payload: Any annotation
# --------------------------------------------------------------------------


def test_submit_answers_payload_annotation_survives_temporal_type_conversion() -> None:
    """The ``payload`` parameter must stay annotated ``Any`` -- Temporal's data
    converter type-checks a signal argument against its annotation *before*
    the handler body runs. A ``Dict``-shaped annotation would make
    ``value_to_type`` raise ``TypeError`` for a non-dict wire payload (e.g. a
    bare string), which fails the workflow task outright and, since Temporal
    replays history, would fail identically on every future replay --
    permanently stranding the workflow, defeating this handler's own
    isinstance-based fail-closed design. This drives the real Temporal
    converter (not a fake) against the handler's live type hint to prove a
    non-dict payload converts cleanly instead of raising."""
    hints = typing.get_type_hints(HitlAnswerSignalMixin.submit_answers)
    payload_hint = hints["payload"]

    # Must not raise -- a Dict[str, Any] annotation would raise TypeError here.
    assert value_to_type(payload_hint, "not-a-dict") == "not-a-dict"
    assert value_to_type(payload_hint, {"resume_token": "tok-1", "answers": []}) == {
        "resume_token": "tok-1",
        "answers": [],
    }


# --------------------------------------------------------------------------
# _log_signal_diagnostic -- in-workflow branch
# --------------------------------------------------------------------------


class _FakeWorkflowLogger:
    def __init__(self) -> None:
        self.warnings: list[tuple[str, tuple]] = []

    def warning(self, msg: str, *args) -> None:
        self.warnings.append((msg, args))


def test_log_signal_diagnostic_logs_via_workflow_logger_inside_a_workflow(monkeypatch) -> None:
    """The in-workflow branch of _log_signal_diagnostic (guarded by
    workflow.in_workflow()) is only reachable inside a real Temporal workflow
    sandbox -- monkeypatch workflow.in_workflow/workflow.logger to exercise it
    without one, proving the operator diagnostic trail this module's
    postconditions promise is actually emitted, not just documented."""
    fake_logger = _FakeWorkflowLogger()
    monkeypatch.setattr(temporal_signal_module.workflow, "in_workflow", lambda: True)
    monkeypatch.setattr(temporal_signal_module.workflow, "logger", fake_logger)

    temporal_signal_module._log_signal_diagnostic("submit_answers rejected: %r", "reason")

    assert fake_logger.warnings == [("submit_answers rejected: %r", ("reason",))]


def test_log_signal_diagnostic_is_a_silent_no_op_outside_a_workflow(monkeypatch, caplog) -> None:
    """The documented contract is a no-op (not a stdlib-logging fallback) when
    workflow.in_workflow() is False -- e.g. every other test in this suite,
    which drives HitlAnswerSignalMixin as a bare object with no Temporal
    context. Asserts both that the workflow logger is never touched AND that
    nothing lands on the stdlib logging chain either, pinning "no-op" rather
    than "logs somewhere else" as the actual behavior."""
    fake_logger = _FakeWorkflowLogger()
    monkeypatch.setattr(temporal_signal_module.workflow, "in_workflow", lambda: False)
    monkeypatch.setattr(temporal_signal_module.workflow, "logger", fake_logger)

    with caplog.at_level(logging.DEBUG):
        temporal_signal_module._log_signal_diagnostic("submit_answers rejected: %r", "reason")

    assert fake_logger.warnings == []
    assert not caplog.records


# --------------------------------------------------------------------------
# wait_for_answers -- the durable wait
# --------------------------------------------------------------------------


class _FakeWaitCondition:
    """Stand-in for ``workflow.wait_condition``, driving the wait without a server.

    Records, per await, whether the predicate ALREADY held at call time (the real
    SDK returns immediately in that case, which is how a signal that beat the wait
    gets consumed) and exactly what kwargs it was handed (so a test can pin that no
    ``timeout=`` is ever passed). When the predicate does not hold, it runs the next
    queued deliverer to simulate a signal landing while the workflow is parked, and
    fails loudly if there is nothing left to deliver -- which in production would be
    a wait that hangs forever.
    """

    def __init__(self, *deliverers) -> None:
        self._deliverers = list(deliverers)
        self.predicates: list = []
        self.satisfied_at_call: list[bool] = []
        self.kwargs: list[dict] = []

    async def __call__(self, predicate, **kwargs) -> None:
        # Evaluated ONCE per call, like the real wait_condition: a fake that
        # polled the predicate twice would silently tolerate a side-effecting
        # predicate the SDK would not.
        satisfied = bool(predicate())
        self.predicates.append(predicate)
        self.satisfied_at_call.append(satisfied)
        self.kwargs.append(kwargs)
        if satisfied:
            return
        assert self._deliverers, "wait_condition parked with nothing left to deliver -- this wait would hang"
        self._deliverers.pop(0)()
        assert predicate(), "the scripted deliverer did not satisfy the wait predicate"


class _Verifier:
    """Stand-in for the caller's durable-store reconciliation, scripted per call."""

    def __init__(self, *results: bool) -> None:
        self._results = list(results)
        self.calls = 0

    async def __call__(self) -> bool:
        self.calls += 1
        assert self._results, "verify was awaited more times than the test scripted"
        return self._results.pop(0)


def _install_wait(monkeypatch, fake: _FakeWaitCondition) -> _FakeWaitCondition:
    monkeypatch.setattr(temporal_signal_module.workflow, "wait_condition", fake)
    return fake


@pytest.mark.asyncio
async def test_wait_for_answers_returns_the_delivered_batch_and_resets_state(monkeypatch) -> None:
    """The core round trip: park, a token-matching signal lands, the batch is
    returned and both pause attributes are reset so the next round starts clean."""
    wf = _Workflow()
    batch = [_answer("q1", selected_option_id="yes")]
    fake = _install_wait(
        monkeypatch,
        _FakeWaitCondition(lambda: wf.submit_answers({"resume_token": "tok-1", "answers": batch})),
    )

    result = await wf.wait_for_answers("tok-1")

    assert result == batch
    assert fake.satisfied_at_call == [False]
    assert wf._active_resume_token is None
    assert wf._submitted_answers is None
    assert wf._buffered_signals == {}


@pytest.mark.asyncio
async def test_wait_for_answers_arms_the_token_before_parking(monkeypatch) -> None:
    """The token must be armed BEFORE the wait suspends, or a signal that lands
    while parked would hit the no-active-pause branch and be buffered instead of
    applied -- leaving the workflow paused on an answer it already received."""
    wf = _Workflow()
    observed: list = []

    def _deliver() -> None:
        observed.append(wf._active_resume_token)
        wf.submit_answers({"resume_token": "tok-1", "answers": [_answer("q1")]})

    _install_wait(monkeypatch, _FakeWaitCondition(_deliver))

    await wf.wait_for_answers("tok-1")

    assert observed == ["tok-1"]


@pytest.mark.asyncio
async def test_wait_for_answers_consumes_a_signal_that_arrived_before_the_wait(monkeypatch) -> None:
    """The signal-before-wait race: a batch buffered while no pause was active is
    applied when the wait arms, so the predicate already holds at call time and the
    wait returns without ever parking. The flag is CHECKED, not only awaited."""
    wf = _Workflow()
    batch = [_answer("q1", selected_option_id="yes")]
    wf.submit_answers({"resume_token": "tok-1", "answers": batch})
    assert wf._buffered_signals == {"tok-1": batch}
    fake = _install_wait(monkeypatch, _FakeWaitCondition())

    result = await wf.wait_for_answers("tok-1")

    assert result == batch
    assert fake.satisfied_at_call == [True]
    assert wf._buffered_signals == {}


@pytest.mark.asyncio
async def test_wait_for_answers_discards_buffered_entries_for_other_tokens(monkeypatch) -> None:
    """Arming a pause clears the WHOLE buffer, not just the matching entry, so a
    stale batch for a token this workflow never arms cannot survive into a later
    round and be mistaken for that round's answer."""
    wf = _Workflow()
    wf.submit_answers({"resume_token": "stale", "answers": [_answer("q0")]})
    batch = [_answer("q1")]
    wf.submit_answers({"resume_token": "tok-1", "answers": batch})
    assert set(wf._buffered_signals) == {"stale", "tok-1"}
    _install_wait(monkeypatch, _FakeWaitCondition())

    result = await wf.wait_for_answers("tok-1")

    assert result == batch
    assert wf._buffered_signals == {}


@pytest.mark.asyncio
async def test_wait_for_answers_predicate_tests_is_not_none_not_truthiness(monkeypatch) -> None:
    """Pins the class's 'Requirements on adopters' contract: the predicate must be
    ``_submitted_answers is not None``. An empty list is falsy but not None, so a
    truthiness predicate would keep waiting on a batch that had already landed.
    Unreachable through submit_answers today (it rejects an empty batch) -- which
    is exactly why the predicate itself, not the handler, is asserted here."""
    wf = _Workflow()
    fake = _install_wait(
        monkeypatch,
        _FakeWaitCondition(lambda: wf.submit_answers({"resume_token": "tok-1", "answers": [_answer("q1")]})),
    )

    await wf.wait_for_answers("tok-1")

    predicate = fake.predicates[0]
    wf._submitted_answers = None
    assert predicate() is False
    wf._submitted_answers = []
    assert predicate() is True


@pytest.mark.asyncio
async def test_wait_for_answers_never_passes_a_timeout(monkeypatch) -> None:
    """The no-default guarantee at its source: a ``timeout=`` on wait_condition
    would release the wait without a real answer, which is the fabricated-resume
    failure this whole mechanism exists to prevent."""
    wf = _Workflow()
    fake = _install_wait(
        monkeypatch,
        _FakeWaitCondition(lambda: wf.submit_answers({"resume_token": "tok-1", "answers": [_answer("q1")]})),
    )

    await wf.wait_for_answers("tok-1")

    assert fake.kwargs == [{}]


@pytest.mark.asyncio
@pytest.mark.parametrize("bad_token", ["", None, 0, b"tok-1"])
async def test_wait_for_answers_rejects_a_non_string_or_empty_resume_token(monkeypatch, bad_token) -> None:
    """A falsy or non-str token can never equal a submitter's echoed token, so the
    predicate would be permanently unsatisfiable -- fail at the call site instead
    of stranding the workflow in a wait nothing can release."""
    wf = _Workflow()
    _install_wait(monkeypatch, _FakeWaitCondition())

    with pytest.raises(AssertionError, match="non-empty resume_token"):
        await wf.wait_for_answers(bad_token)


@pytest.mark.asyncio
async def test_wait_for_answers_rejects_a_non_callable_verify(monkeypatch) -> None:
    """Checked up front rather than at the first await: a non-callable would
    otherwise surface as a TypeError from inside an already-parked workflow."""
    wf = _Workflow()
    _install_wait(monkeypatch, _FakeWaitCondition())

    with pytest.raises(AssertionError, match="zero-argument callable"):
        await wf.wait_for_answers("tok-1", verify="tok-1")


@pytest.mark.asyncio
async def test_wait_for_answers_verify_releases_the_wait_with_no_signal_at_all(monkeypatch) -> None:
    """A durable batch can exist with no signal to match it -- the early-arrived
    signal was evicted past MAX_BUFFERED_SIGNALS, or never sent. verify is checked
    BEFORE parking so the wait doesn't hang for a signal that will never come; the
    None return tells the caller to read the batch back from its own store."""
    wf = _Workflow()
    verifier = _Verifier(True)
    fake = _install_wait(monkeypatch, _FakeWaitCondition())

    result = await wf.wait_for_answers("tok-1", verify=verifier)

    assert result is None
    assert verifier.calls == 1
    assert fake.satisfied_at_call == []
    assert wf._active_resume_token is None


@pytest.mark.asyncio
async def test_wait_for_answers_verify_rejects_an_unbacked_signal_and_keeps_waiting(monkeypatch) -> None:
    """A signal is a wake-up hint, never the answer itself. A signal whose batch the
    durable store cannot confirm re-arms the latch rather than resuming the
    workflow, so a sender that signals without persisting cannot fabricate a
    resume."""
    wf = _Workflow()
    verifier = _Verifier(False, False, True)
    fake = _install_wait(
        monkeypatch,
        _FakeWaitCondition(lambda: wf.submit_answers({"resume_token": "tok-1", "answers": [_answer("q1")]})),
    )

    result = await wf.wait_for_answers("tok-1", verify=verifier)

    assert result is None
    assert verifier.calls == 3
    assert fake.satisfied_at_call == [False]
    assert wf._submitted_answers is None
    assert wf._active_resume_token is None


@pytest.mark.asyncio
async def test_wait_for_answers_verify_returns_the_batch_once_confirmed(monkeypatch) -> None:
    """The ordinary verify path: a signal lands, the durable store confirms it, and
    the in-memory batch is returned rather than forcing a redundant read-back."""
    wf = _Workflow()
    batch = [_answer("q1", selected_option_id="yes")]
    verifier = _Verifier(False, True)
    _install_wait(
        monkeypatch,
        _FakeWaitCondition(lambda: wf.submit_answers({"resume_token": "tok-1", "answers": batch})),
    )

    result = await wf.wait_for_answers("tok-1", verify=verifier)

    assert result == batch
    assert verifier.calls == 2


@pytest.mark.asyncio
async def test_wait_for_answers_stays_parked_through_a_mismatched_token_signal(monkeypatch) -> None:
    """End to end across both halves: a signal for a different pause round is
    rejected by the handler and leaves the predicate unsatisfied, so the wait keeps
    waiting for the token it actually armed."""
    wf = _Workflow()
    batch = [_answer("q1", selected_option_id="yes")]

    def _deliver() -> None:
        wf.submit_answers({"resume_token": "wrong-token", "answers": [_answer("q9")]})
        assert wf._submitted_answers is None
        wf.submit_answers({"resume_token": "tok-1", "answers": batch})

    _install_wait(monkeypatch, _FakeWaitCondition(_deliver))

    assert await wf.wait_for_answers("tok-1") == batch
