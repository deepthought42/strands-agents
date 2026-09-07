"""Shared Temporal signal-handler primitive for durable HITL answer delivery.

Provides the receiving half of the durable human-in-the-loop mechanism: a
``@workflow.signal``-decorated ``submit_answers`` handler plus the buffer/
reject/accept state machine backing it, as a composable mixin any
``@workflow.defn`` class can inherit. This is the signal name and payload
envelope the coding team's ``CodingTeamWorkflow`` already uses in production
(``software_engineering_team/temporal/coding_team_workflow.py``), extracted
here so ``PlanningWorkflow`` can register the identical contract without a
third bespoke copy of its own.

Not yet a full convergence: ``CodingTeamWorkflow`` still carries its own
inline copy of this same state machine, and
``planning_team.temporal.answer_signal.PlanningAnswerSignalMixin`` (signal
name ``submit_planning_answers``, predating this contract, still wired into
``RunTeamWorkflowV2``) remains live and untouched. Migrating
``CodingTeamWorkflow`` onto this mixin (with a ``workflow.patched`` gate
protecting its pre-existing history) and reconciling
``PlanningAnswerSignalMixin`` are deliberately deferred, not implicitly
completed by this module's existence — both are tracked as follow-up work.

**Do not compose this mixin together with ``PlanningAnswerSignalMixin`` on
the same workflow class.** Both use the identical private attribute names
(``_active_resume_token``/``_submitted_answers``/``_buffered_signals``) and
both chain ``super().__init__()``, so a class inheriting both would have the
two signal handlers silently alias one shared set of state — e.g.
``PlanningAnswerSignalMixin.wait_for_planning_answers`` arming
``_active_resume_token`` would make this module's ``submit_answers`` treat
that token as its own active pause, under a different signal name and a
different validation/buffering contract. A workflow needing both gate kinds
must not use both mixins until they converge onto one implementation.

Also provides the waiting half: ``HitlAnswerSignalMixin.wait_for_answers``
arms a pause for a ``resume_token``, drains any signal that arrived before the
wait began, and suspends on ``workflow.wait_condition`` until a validated,
token-matching batch lands. It has no timeout and no default-answer path, so a
workflow nobody answers stays paused rather than resuming with something
fabricated. What is still NOT here is the wiring: nothing in this module reads
a job store, schedules an activity, or knows what a pause means to a given
team — a caller with a durable answer store passes ``verify`` to reconcile
against it. A signal handler
must never raise (Temporal replays workflow history, so an unhandled
exception here would fail identically forever), so every validation failure
here is a non-raising, state-preserving rejection: the payload is dropped,
the workflow's paused state is left exactly as it was, and only a
replay-safe diagnostic is logged (see :func:`_log_signal_diagnostic`).

Preconditions:
    - ``backend/agents`` and ``backend`` are on ``sys.path`` (the ``shared_*``
      convention).
Postconditions:
    - Importing has no side effects beyond class/function definition; no I/O,
      no workflow execution.
"""

from __future__ import annotations

from typing import Any, Awaitable, Callable, Dict, List, Optional

from pydantic import ValidationError
from temporalio import workflow

from shared.hitl.models import AnswerSubmission

__all__ = [
    "SUBMIT_ANSWERS_SIGNAL",
    "MAX_BUFFERED_SIGNALS",
    "HitlAnswerSignalMixin",
]

#: Reused verbatim from ``CodingTeamWorkflow.submit_answers`` — signal names are
#: scoped per workflow type, so there is no collision risk in sharing the name
#: across ``CodingTeamWorkflow``/``PlanningWorkflow``, and a single name keeps one
#: shared vocabulary for any future workflow that hosts both kinds of gate.
SUBMIT_ANSWERS_SIGNAL = "submit_answers"

#: Upper bound on distinct not-yet-armed ``resume_token``s ``_buffered_signals``
#: retains. An unbounded buffer lets an adversarial or misbehaving sender grow
#: durable workflow state without limit merely by sending signals with fresh,
#: never-armed tokens. Small and fixed: a workflow only ever has one pause
#: armed (or about to be armed) at a time, so more than a handful of
#: early-arrived, still-unclaimed batches is already anomalous.
MAX_BUFFERED_SIGNALS = 8


def _log_signal_diagnostic(msg: str, *args: Any) -> None:
    """Log a ``submit_answers`` diagnostic via the replay-aware workflow logger.

    Covers both rejection notices and the buffer-cap eviction notice (not
    itself a rejection — the incoming signal is buffered right after); both
    need the same outside-a-workflow-safe guard, so there is no value in two
    near-identical helpers.

    Preconditions:
        - None.
    Postconditions:
        - No-op (never raises) when called outside a running workflow, e.g.
          from a unit test driving ``HitlAnswerSignalMixin`` as a bare object
          (the established pattern this module's own test suite, and its
          ``CodingTeamWorkflow``/``PlanningAnswerSignalMixin`` siblings, all
          use to test signal-handler logic without a Temporal test server).
          ``workflow.logger`` itself requires an active workflow event loop
          and raises ``_NotInWorkflowEventLoopError`` otherwise, so this
          checks ``workflow.in_workflow()`` first.
        - Inside a running workflow, logs at ``WARNING`` via
          ``workflow.logger``. The SDK's workflow logger only suppresses
          replayed log calls *below* ``WARNING``, so this diagnostic is
          re-emitted on every replay (worker restart, a ``__stack_trace``
          query, etc.) — that duplication is harmless, since logging is
          local-only and never affects workflow determinism, and each
          replay's re-emission reflects the same rejection or eviction
          decision, not a new one.
    """
    if workflow.in_workflow():
        workflow.logger.warning(msg, *args)


def _bounded_repr(value: Any, max_chars: int = 64) -> str:
    """Return ``repr(value)`` truncated to ``max_chars``, for safe logging of
    unvalidated, client-supplied values (e.g. a ``resume_token``) whose length
    this module never bounds -- a sender able to deliver a signal could
    otherwise flood operator logs with an arbitrarily large token on every
    rejected attempt.

    Preconditions:
        - None.
    Postconditions:
        - Returns ``repr(value)`` unchanged if it is at most ``max_chars``
          long; otherwise returns the first ``max_chars`` characters of that
          repr followed by a truncation marker. Never raises, even if
          ``value.__repr__`` itself raises (``value`` is arbitrary,
          unvalidated signal-payload data of type ``Any``).
    """
    try:
        rendered = repr(value)
    except Exception:
        return "<unrepresentable>"
    if len(rendered) <= max_chars:
        return rendered
    return rendered[:max_chars] + "...(truncated)"


#: The private attribute names ``HitlAnswerSignalMixin`` owns. Single source of
#: truth for both the ``__init__`` composition guard and the attributes it sets
#: -- keeping one list means a future added attribute can't be added to the
#: assignments while the guard's coverage silently stays stale.
_OWNED_STATE_ATTRS = ("_active_resume_token", "_submitted_answers", "_buffered_signals")


def _validate_answer_batch(raw: Any) -> Optional[List[Dict[str, Any]]]:
    """Validate a signal payload's ``answers`` value against ``AnswerSubmission``.

    Preconditions:
        - None — ``raw`` is untrusted, signal-delivered data of arbitrary shape.
    Postconditions:
        - Returns ``None`` (never raises, for any input) if ``raw`` is not a
          non-empty list, or any element is not a dict with all-``str`` keys
          all of which are recognized ``AnswerSubmission`` fields, or any
          element fails ``AnswerSubmission`` validation — the whole batch is
          rejected on a single bad entry rather than silently dropping just
          that entry, so a resume can never proceed with a
          partially-validated answer set. An empty list is rejected too:
          there is no content to apply, and accepting it would let a caller
          mistake "submitted, vacuously" for "not yet submitted" if it ever
          tests ``_submitted_answers`` for truthiness instead of ``is not
          None``. An unrecognized key is rejected rather than silently
          dropped: pydantic's default ``model_dump()`` behavior discards
          extra fields, so without this check a sender's typo (e.g.
          ``other_txt`` instead of ``other_text``) would "successfully"
          submit an answer with its actual content quietly stripped —
          exactly the partial-content failure mode whole-batch rejection
          exists to prevent, just triggered by a misspelling instead of a
          missing field.
        - Otherwise returns a new, non-empty list of plain dicts, one per
          input element, each normalized through ``AnswerSubmission`` (so
          every dict carries the schema's full field set, e.g. an omitted
          ``other_text`` becomes an explicit ``None``).
    """
    if not isinstance(raw, list) or not raw:
        return None
    validated: List[Dict[str, Any]] = []
    for item in raw:
        if not isinstance(item, dict) or not all(isinstance(key, str) for key in item):
            return None
        if not set(item).issubset(AnswerSubmission.model_fields):
            return None
        try:
            answer = AnswerSubmission(**item)
        except ValidationError:
            return None
        validated.append(answer.model_dump())
    return validated


class HitlAnswerSignalMixin:
    """Mixin giving a Temporal workflow class the ``submit_answers`` signal and
    its backing buffer/reject/accept state machine.

    Invariants:
        - ``self._active_resume_token`` is non-``None`` only while a pause is
          armed for that token — between :meth:`wait_for_answers` arming it
          and that same call returning (or a subclass driving the three
          attributes itself, as ``CodingTeamWorkflow`` does inline) — so
          ``submit_answers`` can tell a fresh submission for the CURRENT pause
          apart from a stale one for an already-resolved pause.
        - ``self._submitted_answers`` is non-``None`` only in the narrow
          window between a validated, token-matching ``submit_answers``
          signal being delivered and a waiter consuming it (which resets it to
          ``None`` before returning) — so a stale answer batch from one pause
          round can never be mistaken for a fresh one in the next.
        - ``self._buffered_signals`` holds at most one early-arrived, validated
          answer batch per not-yet-armed ``resume_token``, and never more than
          ``MAX_BUFFERED_SIGNALS`` distinct tokens at once — the oldest entry
          (by arrival order) is evicted before a new one is buffered past the
          cap, so durable workflow state cannot grow without bound.

    Requirements on adopters:
        - A workflow class using this mixin must ensure
          ``HitlAnswerSignalMixin.__init__`` runs — define no ``__init__`` of
          its own, or chain ``super().__init__()`` if it does — so the buffer
          state attributes exist before any signal is delivered. Skipping
          this makes the first delivered signal raise ``AttributeError``
          inside the handler, the permanent-strand failure mode this module
          exists to prevent.
        - Any consumer waiting on this state — :meth:`wait_for_answers`
          below, or a subclass driving its own ``workflow.wait_condition``
          predicate as ``CodingTeamWorkflow`` still does — MUST test
          ``self._submitted_answers is not None``, never truthiness: that is
          the only reliable "has a signal landed" test. It happens to be
          moot today only because ``submit_answers`` never stores an empty
          list (an empty ``"answers"`` batch is rejected as malformed, see
          :func:`_validate_answer_batch`) — but ``is not None`` remains the
          contractually correct test, not an accident of today's validation
          choice.
        - ``__init__``'s composition guard (below) calls ``super().__init__()``
          *before* checking for existing state, so a conflicting sibling
          later in the MRO runs *inside* that ``super().__init__()`` call and
          already has its attributes set by the time the guard executes —
          the guard therefore raises regardless of whether
          ``HitlAnswerSignalMixin`` is listed first or after such a sibling
          in a class's bases, as long as that sibling assigns its state
          within its own ``__init__``. The one residual gap: a sibling listed
          *before* this mixin whose ``__init__`` assigns its state *after*
          its own ``super().__init__()`` call returns — those assignments
          land after this guard has already passed. The module docstring's
          blanket "do not compose with ``PlanningAnswerSignalMixin``" rule
          remains the authoritative constraint; the guard is a loud backstop
          for the far more common ordering, not a substitute for it.
    """

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        # Guards the module docstring's "do not compose with PlanningAnswerSignalMixin"
        # warning: that mixin owns the identical attribute names and also chains
        # super().__init__(), so a conflicting sibling anywhere in the MRO that ran
        # inside the super().__init__() call above (i.e. assigned its state within
        # its own __init__) already has its attributes set by now; silently
        # overwriting them here would alias its signal contract onto this one.
        # Failing loudly at construction time turns that into a deterministic,
        # replay-safe error instead of runtime state corruption. See the class
        # docstring's Invariants for the one base-order combination this misses.
        for _attr in _OWNED_STATE_ATTRS:
            if hasattr(self, _attr):
                raise TypeError(
                    f"{type(self).__name__} already defines {_attr!r}; composing "
                    "HitlAnswerSignalMixin with another signal mixin that owns the "
                    "same state (e.g. PlanningAnswerSignalMixin) is unsupported -- "
                    "see this module's docstring."
                )
        # Kept as explicit, individually-annotated assignments (rather than a loop
        # over _OWNED_STATE_ATTRS) so each attribute keeps its own type annotation;
        # the names themselves must be kept identical to that shared tuple above.
        self._active_resume_token: Optional[str] = None
        self._submitted_answers: Optional[List[Dict[str, Any]]] = None
        self._buffered_signals: Dict[str, List[Dict[str, Any]]] = {}
        # Pins _OWNED_STATE_ATTRS to the three names the assignments above set,
        # so guard-side drift (a name added to the tuple with no matching
        # assignment) fails loudly here instead of as an AttributeError on the
        # first delivered signal. This does NOT observe the assignment
        # statements themselves: a future `self._new_attr = ...` added above
        # without updating both this literal and the tuple would pass this
        # check and silently escape the composition guard (assignment-side
        # drift) -- when adding an owned attribute, update the assignments,
        # _OWNED_STATE_ATTRS, and this literal together.
        # Deterministic and replay-safe; not gated on -O since a stripped assert
        # would silently drop this exact protection.
        if set(_OWNED_STATE_ATTRS) != {
            "_active_resume_token",
            "_submitted_answers",
            "_buffered_signals",
        }:
            raise RuntimeError(
                "_OWNED_STATE_ATTRS no longer matches HitlAnswerSignalMixin.__init__'s "
                "explicit assignments -- update both together."
            )

    @workflow.signal(name=SUBMIT_ANSWERS_SIGNAL)
    def submit_answers(self, payload: Any = None) -> None:
        """Deliver a human answer batch for the current (or a not-yet-armed) pause.

        Preconditions:
            - None enforced — ``payload`` arrives from outside the workflow, so
              this handler validates its shape defensively rather than trusting
              a precondition an external, unvalidated signal cannot guarantee.
              A well-formed payload is a dict shaped
              ``{"resume_token": str, "answers": list}``, each ``answers``
              element ``AnswerSubmission``-shaped. The parameter is typed
              ``Any``, not ``Dict[str, Any]``, deliberately: Temporal's data
              converter type-checks a signal argument against its annotation
              *before* the handler body runs, so a ``Dict`` annotation would
              raise ``TypeError`` for a non-dict wire payload during argument
              conversion — never reaching the checks below — and an unhandled
              exception here fails the workflow task and, since Temporal
              replays history, would fail identically on every future replay,
              permanently stranding the workflow. It defaults to ``None`` for
              the same reason: Temporal invokes a signal handler as
              ``handler.fn(*decoded_args)``, so a zero-argument delivery (an
              empty-args signal, or a forwarding shim that drops an empty
              payload) would otherwise raise ``TypeError: missing 1 required
              positional argument`` before any validation runs — the same
              permanent-strand failure mode, just one step earlier. With the
              default, a zero-arg delivery binds ``payload=None`` and falls
              through to the ``not isinstance(payload, dict)`` rejection below
              like any other malformed payload.
        Postconditions:
            - A payload that is not a dict, or whose ``"answers"`` value fails
              :func:`_validate_answer_batch` (missing, empty, not a list, or
              any element malformed), is ignored: returns without mutating any
              workflow state — the only action taken is the operator
              diagnostic log described at the end of this docstring — leaving
              the workflow's paused state exactly as it was (fails closed
              rather than resuming with partial content).
            - When no pause is currently active
              (``self._active_resume_token is None``), a well-formed payload is
              treated as an early arrival for a pause not yet armed: a
              non-empty string ``resume_token`` is buffered in
              ``self._buffered_signals``, keyed by that token (first
              submission per token wins — an already-buffered token is left
              alone; buffering past ``MAX_BUFFERED_SIGNALS`` evicts the oldest
              entry first). A payload with no usable ``resume_token`` while no
              pause is active has nothing to key a buffer entry on and is
              dropped.
            - Otherwise, validates ``payload.get("resume_token")`` against
              ``self._active_resume_token``: a mismatch is ignored, not
              applied — this is the out-of-order rejection: a signal that
              arrives for a pause that is not the one currently pending is
              never applied to it. Once a batch is accepted for the current
              token, a second matching-token signal (a double-submit, or two
              clients racing) is ignored too, for as long as the first batch
              remains unconsumed (``self._submitted_answers is not None``) —
              first submission per token wins *within one pause round*. Once
              a caller consumes it (resets ``self._submitted_answers`` to
              ``None`` while the same token stays active), that dedup window
              closes: a further matching signal is accepted and overwrites.
              Deduplicating across the remainder of a pause once consumed is
              the caller's responsibility, not this handler's. Only a
              token-matching first submission with a valid ``"answers"``
              batch sets ``self._submitted_answers``.

        Every rejection branch below logs via :func:`_log_signal_diagnostic` before
        returning — never raises (Temporal's ``workflow.logger`` is
        replay-aware, so this adds an operator diagnostic trail without
        affecting determinism or violating the never-raise contract; see
        :func:`_log_signal_diagnostic` for why this is not simply
        ``workflow.logger.warning`` called directly).
        """
        if not isinstance(payload, dict):
            _log_signal_diagnostic("submit_answers rejected: payload is not a dict (%r)", type(payload))
            return
        raw_answers = payload.get("answers")
        answers = _validate_answer_batch(raw_answers)
        if answers is None:
            # %r of the raw value itself is deliberately omitted -- it is
            # unvalidated client input and could be arbitrarily large; the type
            # alone is enough to distinguish "missing", "wrong shape", and
            # "empty list" from this log line, matching the diagnostic detail
            # the sibling rejection branches below already carry.
            _log_signal_diagnostic(
                "submit_answers rejected: malformed or empty answers batch (raw type=%r)",
                type(raw_answers),
            )
            return
        resume_token = payload.get("resume_token")
        if self._active_resume_token is None:
            if isinstance(resume_token, str) and resume_token:
                # The `and self._buffered_signals` guard keeps `next(iter(...))` below
                # unreachable on an empty dict even if MAX_BUFFERED_SIGNALS were ever
                # 0 or negative -- StopIteration here would violate the never-raise
                # contract. Unreachable with today's positive constant; defensive.
                if (
                    resume_token not in self._buffered_signals
                    and self._buffered_signals
                    and len(self._buffered_signals) >= MAX_BUFFERED_SIGNALS
                ):
                    oldest_token = next(iter(self._buffered_signals))
                    del self._buffered_signals[oldest_token]
                    _log_signal_diagnostic(
                        "submit_answers: buffer cap reached, evicted oldest buffered resume_token=%s",
                        _bounded_repr(oldest_token),
                    )
                self._buffered_signals.setdefault(resume_token, answers)
            else:
                _log_signal_diagnostic(
                    "submit_answers dropped: no pause active and no usable resume_token to buffer against"
                )
            return
        if resume_token != self._active_resume_token:
            _log_signal_diagnostic(
                "submit_answers rejected: resume_token mismatch (received=%s, active=%s)",
                _bounded_repr(resume_token),
                _bounded_repr(self._active_resume_token),
            )
            return
        if self._submitted_answers is not None:
            _log_signal_diagnostic(
                "submit_answers rejected: duplicate submission for resume_token=%s", _bounded_repr(resume_token)
            )
            return
        self._submitted_answers = answers

    async def wait_for_answers(
        self,
        resume_token: str,
        *,
        verify: Optional[Callable[[], Awaitable[bool]]] = None,
    ) -> Optional[List[Dict[str, Any]]]:
        """Durably suspend this workflow until a matching ``submit_answers`` signal
        lands -- or, with ``verify``, until a durable answer batch is confirmed for
        ``resume_token`` -- then return the delivered answers.

        The waiting half of the mechanism whose receiving half is
        :meth:`submit_answers`. The arm/drain/clear/consume sequence is the one
        ``CodingTeamWorkflow.run`` already runs inline
        (``software_engineering_team/temporal/coding_team_workflow.py``) and
        ``planning_team.temporal.answer_signal.PlanningAnswerSignalMixin.wait_for_planning_answers``
        already packages -- deliberately the same underlying approach rather than a
        divergent one.

        The optional ``verify`` predicate exists because a signal is a wake-up HINT,
        not the answer itself: an early-arrived batch can be evicted from
        ``_buffered_signals`` once past ``MAX_BUFFERED_SIGNALS``, and a sender can
        signal without having durably persisted anything. When the caller has a
        durable answer store, ``verify`` is what makes the wait trust that store
        rather than the signal. It is a caller-supplied predicate rather than a
        store read performed here so this mixin stays free of job-store knowledge
        and usable by any team.

        Preconditions:
            - ``resume_token`` is a non-empty ``str`` uniquely identifying this pause
              round -- the same token the paused activity's result carried, and the
              one a submitter must echo back in its signal payload.
            - Called only from inside ``@workflow.defn`` code: ``workflow.wait_condition``
              is valid nowhere else.
            - ``verify``, when given, is a zero-argument callable returning an
              awaitable ``bool``, and is itself REPLAY-SAFE -- it must await an
              activity (or otherwise be deterministic under replay), never read a
              clock, generate randomness, or perform direct I/O from workflow code.
              It is the one non-determinism this method cannot check on the caller's
              behalf.
            - No other coroutine in this workflow is inside ``wait_for_answers``
              concurrently. The three mixin attributes describe a single pause, so
              two overlapping waits would arm over each other's token.

        Postconditions:
            - Arms the pause (``self._active_resume_token = resume_token``), applies
              any batch already buffered under that token, and clears
              ``self._buffered_signals`` ENTIRELY. This is what makes a signal that
              arrived BEFORE the wait began a hit rather than a loss -- the buffered
              flag is checked, not only awaited -- and it stops a stale entry for
              some other token from leaking into a later pause round.
            - With ``verify`` omitted: suspends on
              ``workflow.wait_condition(lambda: self._submitted_answers is not None)``
              -- ``is not None``, never truthiness, per this class's "Requirements on
              adopters" -- and returns that list. Never returns ``None``.
            - With ``verify`` supplied: the wait is released only once ``verify()``
              answers ``True``. A signal whose batch ``verify()`` cannot confirm
              re-arms the latch and the wait resumes, so an unbacked signal cannot
              resume the workflow. The return value is then the in-memory batch when
              one was accepted, or ``None`` when the durable check alone released the
              wait -- in which case the caller reads the batch back from wherever
              ``verify`` confirmed it.
            - Resets ``self._active_resume_token`` and ``self._submitted_answers`` to
              ``None`` before returning, so the next pause round starts clean.
            - THERE IS NO TIMEOUT, NO DEFAULT, AND NO AUTO-ANSWER PATH. Nothing in
              this method can release the wait except a validated, token-matching
              signal (or a ``verify``-confirmed durable batch). A workflow nobody
              answers stays paused indefinitely -- that is the guarantee, not a gap
              in it.

        Replay safety: the body touches only in-memory workflow state and awaits
        ``workflow.wait_condition``, a deterministic SDK primitive. No
        ``workflow.now()``, no ``datetime``, no ``random``/``uuid``, no
        ``os.getenv``, no I/O, and no ``asyncio`` primitive of its own -- so a worker
        restart mid-wait replays history to exactly this suspension point and
        resumes from it. ``_buffered_signals`` is touched only via ``pop``/``clear``,
        whose effects depend on insertion order, which is itself derived from
        replayed signal-event order.

        Known limitation, inherited verbatim from ``CodingTeamWorkflow``'s own wait:
        the ``wait_condition`` is unbounded, so an out-of-band job cancellation
        recorded outside this workflow does not interrupt it, and a
        ``CancelledError`` raised during the wait unwinds with the pause still armed.
        Reconciling cancellation with the wait is deliberately unsolved here --
        adding a timeout to solve it would be exactly the default-answer path this
        method must not have.
        """
        assert isinstance(resume_token, str) and resume_token, "wait_for_answers requires a non-empty resume_token"
        # Checked here rather than at the first await site: a non-callable (e.g. a
        # token string passed positionally by mistake) would otherwise stay silent
        # until the wait actually re-checks, and surface as a TypeError from inside
        # a parked workflow -- the one place a clear message is hardest to come by.
        assert verify is None or callable(verify), "wait_for_answers requires a zero-argument callable verify, or None"
        self._active_resume_token = resume_token
        self._submitted_answers = self._buffered_signals.pop(resume_token, None)
        self._buffered_signals.clear()

        if verify is None:
            await workflow.wait_condition(lambda: self._submitted_answers is not None)
        else:
            while True:
                if self._submitted_answers is None:
                    # Checked BEFORE parking: the durable batch may already be there
                    # (its signal evicted from the buffer, or never sent at all), in
                    # which case parking would wait for a signal that never comes.
                    if await verify():
                        break
                    await workflow.wait_condition(lambda: self._submitted_answers is not None)
                if await verify():
                    break
                # Rogue/unbacked signal: the latch fired but the durable store holds
                # no batch for this token. Re-arm and loop -- the top of the loop
                # re-checks once more before parking again, which catches a
                # submission that landed while this check was in flight.
                self._submitted_answers = None

        answers = self._submitted_answers
        self._submitted_answers = None
        self._active_resume_token = None
        # Not merely defensive: with no verify, wait_condition's predicate IS
        # "_submitted_answers is not None", so a None here would mean the SDK
        # released the wait on an unsatisfied predicate.
        if verify is None:
            assert answers is not None
        return answers
