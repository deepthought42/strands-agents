"""Conversation (chat) service helpers for the branding team API.

Holds the synchronous bodies behind the chat endpoints and the small helpers
they share (mission short-circuit, brand auto-creation, response assembly). The
async route wrappers in ``api.routes.conversations`` dispatch these onto the
bounded pipeline executor.

Collaborators tests monkeypatch (``orchestrator``, ``branding_store``,
``conversation_store``, ``assistant_agent`` via ``_get_assistant_agent``) are
owned by ``main`` and dereferenced through it at call time. The ``import main``
is function-local (not at module scope): ``main`` re-exports names from this
module at its own bottom, so a module-scope hub import would form a load-time
cycle and stop ``conversation`` from being imported independently. The one
exception is ``_phase_caches``: unlike the collaborators above, it is state
this module owns directly (per-conversation, in-memory only) rather than
proxying through ``main``.
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from typing import List, Optional

from fastapi import HTTPException

from branding_team.api.models import (
    ConversationMessage,
    ConversationStateResponse,
    CreateConversationRequest,
    SendMessageRequest,
)
from branding_team.api.state import _mission_has_brand_name, _mission_has_minimal_required_fields
from branding_team.assistant.store import _default_mission, _StoredMessage
from branding_team.models import Brand, BrandingMission, HumanReview, TeamOutput
from branding_team.shared.phase_output_cache import PhaseOutputCache
from branding_team.store import AttachConversationResult
from shared.concurrency import KeyedLazyRegistry

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Per-conversation phase cache (in-memory only; see PhaseOutputCache)
# ---------------------------------------------------------------------------

# Keyed by conversation_id. Unlike ``latest_output`` (persisted to Postgres via
# ``conversation_store``), the phase cache is intentionally process-local: it
# exists to let a future orchestrator.run() call skip re-running unchanged
# pipeline phases within a live process, not to survive a restart. Entries are
# never evicted -- conversations already live forever in their Postgres table
# with no TTL/cleanup precedent anywhere in this codebase, so an unbounded
# registry matches that existing tradeoff rather than introducing a new one.
_phase_caches: KeyedLazyRegistry[str, PhaseOutputCache] = KeyedLazyRegistry()


def _get_or_create_phase_cache(conversation_id: str) -> PhaseOutputCache:
    """Return this conversation's in-memory phase cache, creating one on first use.

    Preconditions:
        ``conversation_id`` is a non-empty string identifying an existing or
        about-to-exist conversation.
    Postconditions:
        Returns the same ``PhaseOutputCache`` instance for a given
        ``conversation_id`` on every call within this process, initialized
        empty (no entries) the first time a given id is seen -- so mutations
        made to the returned cache (e.g. via ``PhaseOutputCache.put``) are
        visible to every later call with the same ``conversation_id``. Never
        persisted to Postgres or across process restarts. Thread-safe:
        first-use construction is delegated to
        ``KeyedLazyRegistry.get_or_create``, which serializes concurrent first
        calls for the same new ``conversation_id`` (the chat endpoints run on a
        bounded pipeline executor threadpool) under that key's own lock -- a
        slow construction for one conversation never delays another
        conversation's first cache.
    """
    return _phase_caches.get_or_create(conversation_id, PhaseOutputCache)


def _run_orchestrator_if_ready(
    mission: BrandingMission,
    previous_mission: Optional[BrandingMission] = None,
    previous_output: Optional[TeamOutput] = None,
    phase_cache: Optional[PhaseOutputCache] = None,
) -> Optional[TeamOutput]:
    """Run the pipeline for *mission*, or reuse a cached result.

    Returns None when the mission lacks the minimal required fields. The
    pipeline output is a pure function of the mission, so when the mission is
    unchanged since the previous run we return ``previous_output`` instead of
    re-running ~40 agents — the common case on the chat path, where most turns
    don't change the mission. Equality is a structural Pydantic compare; no
    serialization needed. This whole-mission short-circuit is checked before
    ``phase_cache`` is ever consulted, so it remains the outermost fast path
    regardless of whether a cache was supplied.

    When a mission edit does slip past the short-circuit, ``phase_cache`` (the
    caller's per-conversation ``PhaseOutputCache``, when supplied) is forwarded
    to ``orchestrator.run``, which runs each phase in isolation and reuses any
    phase whose input hash is already cached — so only the earliest phase whose
    input actually changed, and everything downstream of it, gets recomputed.
    ``phase_cache`` mutates in place (it's a view over a shared, process-wide
    backing store — see ``PhaseOutputCache``), so the caller's retained
    reference already reflects this run's writes; there is nothing separate to
    write back.
    """
    from branding_team.api import main as _main

    if not _mission_has_minimal_required_fields(mission):
        return None
    # NOTE: the short-circuit relies on BrandingMission being treated as
    # immutable — missions are replaced (model_copy/new instance), never mutated
    # in place. If that ever changes, this structural equality could match a
    # mutated-but-same-identity mission and serve stale output; compare a version
    # or content hash instead.
    if previous_output is not None and previous_mission == mission:
        return previous_output
    return _main.orchestrator.run(
        mission=mission,
        human_review=HumanReview(approved=False, feedback="Building brand from conversation."),
        phase_cache=phase_cache,
    )


def _brand_exists(brand_id: str) -> bool:
    from branding_team.api import main as _main

    return _main.branding_store.brand_exists(brand_id)


def link_conversation_to_brand(
    client_id: str,
    brand_id: str,
    conversation_id: str,
    mission: Optional[BrandingMission] = None,
) -> Brand:
    """Atomically link *conversation_id* to *brand_id*, raising on any failure.

    Single choke point for the three call sites that establish a brand<->
    conversation link — ``create_brand`` (``api.routes.brands``),
    ``attach_conversation_to_brand`` (``api.routes.conversations``), and
    ``_auto_create_brand_from_conversation`` below. All three now share the
    same atomicity guarantee via ``BrandingStore.attach_conversation`` (a
    single transaction that checks the uniqueness invariant and writes both
    rows together) instead of each maintaining its own consistency story —
    two of which used to call ``ConversationStore.set_brand`` directly and
    leave the counterpart row unpatched on failure.

    Preconditions:
        ``client_id``, ``brand_id`` identify an existing (client, brand) pair;
        ``conversation_id`` identifies an existing conversation; ``mission``,
        when provided, is a validated ``BrandingMission``.
    Postconditions:
        On success returns the updated ``Brand`` — the conversation now points
        at ``brand_id`` and the brand's ``conversation_id`` is set. When
        *mission* is omitted, the conversation's stored mission is left as-is
        (read inside the attach transaction's own lock, not a pre-lock
        snapshot the caller took beforehand — see
        ``BrandingStore.attach_conversation``); pass it explicitly only when
        the caller is itself the source of truth for that mission (e.g. the
        mission that just drove brand creation). Raises ``HTTPException``: 404
        when the conversation or brand is missing, 409 when the conversation
        is already attached to a *different* brand, 500 for any unrecognized
        result. Never returns ``None`` — every non-OK outcome raises instead.
    """
    from branding_team.api import main as _main

    result, brand = _main.branding_store.attach_conversation(
        client_id, brand_id, conversation_id, mission
    )
    if result is AttachConversationResult.OK:
        return brand
    if result is AttachConversationResult.CONVERSATION_NOT_FOUND:
        raise HTTPException(status_code=404, detail="Conversation not found")
    if result is AttachConversationResult.ALREADY_ATTACHED:
        raise HTTPException(
            status_code=409,
            detail="Conversation is already attached to another brand",
        )
    if result is AttachConversationResult.BRAND_NOT_FOUND:
        raise HTTPException(status_code=404, detail="Brand not found")
    # Defensive: every known AttachConversationResult member is handled
    # above, so this only fires if the enum ever grows a new member.
    raise HTTPException(status_code=500, detail="Unexpected attach result")


def _local_message(role: str, content: str) -> _StoredMessage:
    """Build an in-memory message mirroring what ``append_message`` just wrote,
    so a turn's response can be assembled without re-reading the row.

    Preconditions:
        ``role`` is ``"user"`` or ``"assistant"``; ``content`` is the message
        text that was just persisted for this conversation.
    Postconditions:
        Returns a ``_StoredMessage`` with the given role/content and an
        ISO-8601 UTC timestamp captured now (within sub-millisecond of the
        persisted row's timestamp, which is also app-clock generated).
    """
    return _StoredMessage(
        role=role,
        content=content,
        timestamp=datetime.now(tz=timezone.utc).isoformat(),
    )


def _conversation_to_response(
    conversation_id: str,
    brand_id: Optional[str],
    messages: list,
    mission: BrandingMission,
    latest_output: Optional[TeamOutput],
    suggested_questions: List[str],
    degraded: bool = False,
) -> ConversationStateResponse:
    """Assemble the ``ConversationStateResponse`` API model from in-memory state.

    Preconditions:
        ``messages`` is a list of ``_StoredMessage``-like objects (each with
        ``role``/``content``/``timestamp``); the rest are already-validated
        conversation fields.
    Postconditions:
        Returns a ``ConversationStateResponse`` with ``messages`` mapped 1:1 to
        ``ConversationMessage``, ``suggested_questions`` defaulted to ``[]``,
        and ``degraded`` reflecting whether this turn's mission extraction was
        lost (see ``BrandingAssistantAgent.respond``).
    """
    msg_list = [
        ConversationMessage(role=m.role, content=m.content, timestamp=m.timestamp) for m in messages
    ]
    return ConversationStateResponse(
        conversation_id=conversation_id,
        brand_id=brand_id,
        messages=msg_list,
        mission=mission,
        latest_output=latest_output,
        suggested_questions=suggested_questions or [],
        degraded=degraded,
    )


def _create_branding_conversation_impl(
    req: CreateConversationRequest,
) -> ConversationStateResponse:
    """Synchronous body of :func:`create_branding_conversation` (see its docstring).

    Preconditions:
        ``req`` is a validated ``CreateConversationRequest``.
    Postconditions:
        Same as the endpoint; runs entirely with blocking calls. The route
        dispatches this via ``_bg._run_in_pipeline_executor`` when
        ``initial_message`` is present (assistant/pipeline work), and via
        ``asyncio.to_thread`` for the no-initial-message greeting path.
    """
    from branding_team.api import main as _main

    conversation_store = _main.conversation_store
    brand_id = (req.brand_id or "").strip() or None
    if brand_id:
        if not _brand_exists(brand_id):
            raise HTTPException(status_code=404, detail="Brand not found")

    # Conversations are created unattached. If an initial message is provided
    # and the mission ends up with enough info, the auto-create-brand step
    # below attaches this conversation to a new brand before the response is
    # returned; otherwise, send_message will handle auto-creation on a later turn.
    conversation_id = conversation_store.create(brand_id=brand_id)
    # Seed this conversation's phase-cache slot now; threaded into
    # orchestrator.run below via _run_orchestrator_if_ready.
    phase_cache = _get_or_create_phase_cache(conversation_id)
    initial_message = (req.initial_message or "").strip()
    suggested_questions: List[str] = []
    # Track the response messages in memory (a fresh conversation has none yet)
    # so we don't re-read the row we just wrote.
    messages: List[_StoredMessage] = []
    mission: BrandingMission = _default_mission()
    latest_output: Optional[TeamOutput] = None
    degraded = False

    if initial_message:
        # Freshly created conversation: no prior history, mission is the default.
        # conversation_id was just minted above in this same synchronous call, so
        # append failing here (conversation vanished) isn't reachable in practice;
        # checked anyway for consistency with send_branding_conversation_message.
        if not conversation_store.append_message(conversation_id, "user", initial_message):
            raise HTTPException(status_code=404, detail="Conversation not found")
        messages.append(_local_message("user", initial_message))
        reply, updated_mission, suggested_questions, degraded = (
            _main._get_assistant_agent().respond([], _default_mission(), initial_message)
        )
        if not conversation_store.update_mission(conversation_id, updated_mission):
            raise HTTPException(status_code=404, detail="Conversation not found")
        if not conversation_store.append_message(conversation_id, "assistant", reply):
            logger.warning("Assistant reply not persisted for conversation %s", conversation_id)
        messages.append(_local_message("assistant", reply))
        # Call the hub's re-exported binding, not the module-local name — main
        # re-exports _run_orchestrator_if_ready specifically so tests/callers
        # can intercept it (patch main._run_orchestrator_if_ready); the local
        # name would make that patch a silent no-op, same reasoning as
        # background._run_branding_background calling _main._run_branding_core.
        output = _main._run_orchestrator_if_ready(updated_mission, phase_cache=phase_cache)
        if output is not None:
            if not conversation_store.update_output(conversation_id, output):
                logger.warning("Pipeline output not persisted for conversation %s", conversation_id)
                output = None
        mission, latest_output = updated_mission, output

        # Auto-create a brand when the user provided enough info in the initial message.
        if not brand_id and not req.skip_save and _mission_has_brand_name(updated_mission):
            brand_id = _auto_create_brand_from_conversation(
                conversation_id, updated_mission, output
            )
    else:
        reply = (
            "Hi! I'm your branding lead. I'll guide you through our 5-phase brand development framework — "
            "starting with your Strategic Core. Let's begin: what's your company or product name?"
        )
        if not conversation_store.append_message(conversation_id, "assistant", reply):
            logger.warning("Greeting not persisted for conversation %s", conversation_id)
        messages.append(_local_message("assistant", reply))
        suggested_questions = [
            "What's your company name?",
            "Who is your target audience?",
            "What does your company do?",
        ]

    return _conversation_to_response(
        conversation_id, brand_id, messages, mission, latest_output, suggested_questions, degraded
    )


def _ensure_default_client() -> str:
    """Find or create a default workspace client; return client_id.

    The default client name is configurable via ``BRANDING_DEFAULT_CLIENT_NAME``
    (default ``"My brands"``) for multi-tenant deployments.

    Note:
        Find-or-create is not atomic: two concurrent first-time requests could
        each create a default client. This is benign for the single-user
        assistant flow (subsequent calls return ``list_clients(limit=1)[0]``)
        and client names are intentionally non-unique (a workspace can have
        several clients), so a unique constraint isn't the right fix. A
        dedicated default-workspace flag or app-level lock is a follow-up.
    """
    from branding_team.api import main as _main

    branding_store = _main.branding_store
    clients = branding_store.list_clients(limit=1)
    if clients:
        return clients[0].id
    name = os.environ.get("BRANDING_DEFAULT_CLIENT_NAME", "My brands")
    client = branding_store.create_client(name=name)
    return client.id


def _auto_create_brand_from_conversation(
    conversation_id: str,
    mission: BrandingMission,
    output: Optional[TeamOutput],
) -> Optional[str]:
    """Create a brand from an unattached conversation and link the two.

    Preconditions:
        ``conversation_id`` refers to an existing conversation that is not yet
        attached to a brand, and ``mission`` carries a real (non-placeholder)
        company name.
    Postconditions:
        On success the conversation is attached to the new brand, the brand
        records the conversation id, and any ``output`` is appended as the
        first version. Returns the new brand id, or None if creation failed.

    Note:
        The conversation<->brand link itself is atomic (``link_conversation_to_brand``,
        wrapping ``BrandingStore.attach_conversation``). Appending the first
        version is still a separate statement, so the sequence as a whole is
        NOT fully atomic: if that final step raises, the brand exists and is
        already linked to the conversation, just without its first version.
        Acceptable for the single-user assistant flow today; folding the
        version append into the same transaction is tracked as a follow-up.
    """
    from branding_team.api import main as _main

    branding_store = _main.branding_store
    client_id = _ensure_default_client()
    brand = branding_store.create_brand(
        client_id=client_id,
        mission=mission,
        name=mission.company_name,
    )
    if not brand:
        return None
    # The brand now exists. If any linkage step below fails, the brand is
    # orphaned (created but not attached, or attached but missing its first
    # version). Log a warning that names the brand so the inconsistency is
    # recoverable, then re-raise — surface the failure rather than hide it.
    try:
        link_conversation_to_brand(client_id, brand.id, conversation_id, mission)
        if output and branding_store.append_brand_version(client_id, brand.id, output) is None:
            raise RuntimeError(
                f"brand {brand.id} vanished before appending first version from "
                f"conversation {conversation_id}"
            )
    except Exception:
        logger.warning(
            "Brand %s was created but linking it to conversation %s failed; "
            "the brand may be orphaned",
            brand.id,
            conversation_id,
            exc_info=True,
        )
        raise
    logger.info("Auto-created brand %s from conversation %s", brand.id, conversation_id)
    return brand.id


def _send_branding_conversation_message_impl(
    conversation_id: str, payload: SendMessageRequest
) -> ConversationStateResponse:
    """Synchronous body of :func:`send_branding_conversation_message`.

    Preconditions:
        ``conversation_id`` is a string; ``payload`` is a validated
        ``SendMessageRequest``.
    Postconditions:
        Same as the endpoint; runs entirely with blocking calls. The route
        always dispatches this via ``_bg._run_in_pipeline_executor`` because
        the assistant/pipeline work is blocking.
    """
    from branding_team.api import main as _main

    conversation_store = _main.conversation_store
    state = conversation_store.get_state(conversation_id)
    if state is None:
        raise HTTPException(status_code=404, detail="Conversation not found")
    # Retrieve this conversation's retained phase-cache slot from prior turns;
    # threaded into orchestrator.run below via _run_orchestrator_if_ready.
    phase_cache = _get_or_create_phase_cache(conversation_id)
    brand_id = state.brand_id
    # If the write does not land (conversation no longer exists), don't go on to
    # build an in-memory response that claims the message was persisted.
    if not conversation_store.append_message(conversation_id, "user", payload.message):
        raise HTTPException(status_code=404, detail="Conversation not found")
    history_pairs = [(m.role, m.content) for m in state.messages]
    reply, updated_mission, suggested_questions, degraded = _main._get_assistant_agent().respond(
        history_pairs, state.mission, payload.message
    )
    if not conversation_store.update_mission(conversation_id, updated_mission):
        raise HTTPException(status_code=404, detail="Conversation not found")
    # The reply is already computed and returned to the caller; if this write
    # doesn't land (conversation vanished mid-turn) log it rather than fail the
    # response, so the inconsistency is at least visible in the logs.
    if not conversation_store.append_message(conversation_id, "assistant", reply):
        logger.warning("Assistant reply not persisted for conversation %s", conversation_id)
    # Reuse the prior output when the mission is unchanged this turn; the
    # short-circuit returns the same object, so identity tells us whether a
    # fresh run happened and thus whether a write is needed. Call the hub's
    # re-exported binding, not the module-local name — see the comment in
    # _create_branding_conversation_impl above.
    output = _main._run_orchestrator_if_ready(
        updated_mission, state.mission, state.latest_output, phase_cache=phase_cache
    )
    if output is not None and output is not state.latest_output:
        if not conversation_store.update_output(conversation_id, output):
            logger.warning("Pipeline output not persisted for conversation %s", conversation_id)
            output = state.latest_output

    # Auto-create a brand when the user has provided at least a company name and conversation is unattached.
    if not brand_id and not payload.skip_save and _mission_has_brand_name(updated_mission):
        brand_id = _auto_create_brand_from_conversation(conversation_id, updated_mission, output)

    # Assemble the response from known state instead of re-reading the row.
    messages = list(state.messages) + [
        _local_message("user", payload.message),
        _local_message("assistant", reply),
    ]
    latest_output = output if output is not None else state.latest_output
    return _conversation_to_response(
        conversation_id,
        brand_id,
        messages,
        updated_mission,
        latest_output,
        suggested_questions,
        degraded,
    )
