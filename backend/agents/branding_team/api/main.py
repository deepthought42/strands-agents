"""FastAPI application for the branding strategy team.

This module is the thin app-assembly hub. Responsibility-focused sub-modules hold
the actual logic:

* ``models`` — Pydantic request/response schemas.
* ``state`` — the interactive-review session store + pure mission/question helpers.
* ``lifecycle`` — the ASGI shutdown hook.
* ``background`` — the run/job executor + Temporal-dispatch machinery.
* ``conversation`` — the chat-endpoint bodies and their helpers.
* ``api.routes.*`` — one ``APIRouter`` per concern, mounted below.

This module remains the single owning namespace for the collaborators the test
suite monkeypatches (``orchestrator``, ``assistant_agent``, ``branding_store``,
``_run_executor``, ``_job_manager``, ``_stale_monitor_stop``,
``_job_heartbeat_interval_s``), so ``from …api.main import X`` and
``monkeypatch.setattr(main, "X", …)`` keep working unchanged for those names; the
route, background, and conversation modules dereference the collaborators through
``main`` at call time.

Beyond those globals, this module re-exports only the handful of names a test or
another module actually reaches *through* ``main``: ``orchestrator``,
``JOB_STATUS_RUNNING``/``JOB_STATUS_FAILED``, and — because tests intercept the
run/conversation flow at the hub — ``_run_branding_core``,
``_run_branding_background``, ``_signal_branding_cancel``, and
``_run_orchestrator_if_ready``. Everything else moved out of the old monolith
(DTOs, the session store, mission helpers, the remaining conversation/background
helpers, and the route *handler* functions themselves) is imported directly from
its owning module by the code that needs it — ``api.models``, ``api.state``,
``api.conversation``, ``api.background``, or the mounted ``api.routes.*``
``APIRouter`` — never re-bound here, matching the split-router convention in
``software_engineering_team/api``.
"""

from __future__ import annotations

import concurrent.futures
import contextlib
import logging
import threading
from typing import Any, ContextManager, Optional

from fastapi import HTTPException

# --- Public contract / re-exports (keep import + monkeypatch surface stable) ---
from branding_team.api.lifecycle import _branding_service_shutdown
from branding_team.assistant import get_conversation_store
from branding_team.assistant.agent import BrandingAssistantAgent
from branding_team.orchestrator import (
    orchestrator,  # noqa: F401  (re-export: patched via main.orchestrator)
)
from branding_team.postgres import SCHEMA as BRANDING_POSTGRES_SCHEMA
from branding_team.shared.job_store import (  # noqa: F401
    JOB_STATUS_FAILED,  # re-exported; tests reference main_mod.JOB_STATUS_FAILED
    JOB_STATUS_RUNNING,
)
from branding_team.shared.phase_output_cache import PhaseOutputCache
from branding_team.store import get_default_store
from job_service_client import JobServiceClient, start_stale_job_monitor
from shared.app import create_team_app
from shared.concurrency import BackgroundHeartbeat, KeyedLazyRegistry, LazySingleton
from shared.env_config import env_float, env_int

logger = logging.getLogger(__name__)


def _max_concurrent_runs() -> int:
    """Worker cap for the branding-run executor (env-tunable, clamped to >= 1)."""
    return env_int("BRANDING_MAX_CONCURRENT_RUNS", 4, floor=1)


# Branding runs are submitted to a bounded pool instead of spawning an
# unbounded daemon thread per request. The fixed worker count gives the
# pipeline backpressure (extra submissions queue rather than fan out into
# thousands of concurrent LLM pipelines) while the job row stays PENDING
# until a worker picks it up.
_run_executor = concurrent.futures.ThreadPoolExecutor(
    max_workers=_max_concurrent_runs(),
    thread_name_prefix="branding-run",
)


def _job_heartbeat_interval_s() -> float:
    """Heartbeat cadence for a running branding job (env-tunable, clamped to >= 1.0s)."""
    return env_float("BRANDING_JOB_HEARTBEAT_INTERVAL_S", 30.0, floor=1.0)


# Periodic sweep that fails jobs whose heartbeat has gone stale (e.g. a worker
# crashed mid-run). Degrades gracefully: a job-service outage at import time
# leaves both globals None instead of crashing the whole app.
#
# A branding pipeline can run for several minutes, and its bounded executor can
# leave extra submissions queued as PENDING. While a job is RUNNING its heartbeat
# is kept fresh by ``_job_heartbeat`` (see ``background._run_branding_core``), so
# the sweep never fails a live run regardless of length. The 900s window only has
# to cover the worst-case PENDING queue wait before a worker picks the job up — a
# job stuck PENDING that long is genuinely wedged and should be swept.
try:
    _job_manager = JobServiceClient(team="branding_team")
    _stale_monitor_stop: Optional[threading.Event] = start_stale_job_monitor(
        _job_manager,
        interval_seconds=15.0,
        stale_after_seconds=900.0,
        reason="Job heartbeat stale while pending/running",
    )
except Exception as _init_err:
    logger.warning("Branding job manager init failed: %s", _init_err)
    _job_manager = None
    _stale_monitor_stop = None


def _job_heartbeat(job_id: str) -> ContextManager[Any]:
    """Keep ``job_id``'s job-service heartbeat fresh while the pipeline runs.

    Preconditions:
        ``job_id`` refers to a job already created in the job store.
    Postconditions:
        Returns a context manager that, while active, pings the job service every
        ``_job_heartbeat_interval_s()`` seconds so the stale-job monitor never marks
        a valid long-running branding run as failed. A no-op context when the job
        manager is unavailable; a beat error is logged and never interrupts the run.
    """
    if _job_manager is None:
        return contextlib.nullcontext()
    return BackgroundHeartbeat(
        lambda: _job_manager.heartbeat(job_id),
        _job_heartbeat_interval_s(),
        name=f"branding-job-heartbeat-{job_id}",
        on_error=lambda exc: logger.warning("branding job %s heartbeat error: %s", job_id, exc),
    )


app = create_team_app(
    service_name="branding-team",
    team_key="branding",
    title="Branding Team API",
    version="2.0.0",
    postgres_schema=BRANDING_POSTGRES_SCHEMA,
    on_shutdown=_branding_service_shutdown,
)

branding_store = get_default_store()
conversation_store = get_conversation_store()

# Public name so tests can patch 'branding_team.api.main.assistant_agent'.
assistant_agent: Optional[BrandingAssistantAgent] = None
_assistant_agent_singleton: LazySingleton[BrandingAssistantAgent] = LazySingleton()


def _build_assistant_agent() -> BrandingAssistantAgent:
    try:
        return BrandingAssistantAgent()
    except Exception:
        raise HTTPException(
            status_code=503,
            detail="Branding assistant is temporarily unavailable. LLM service may not be configured.",
        )


def _get_assistant_agent() -> BrandingAssistantAgent:
    """Lazy-init the branding assistant so the app mounts even if llm_service is unavailable.

    Thread-safe: the chat endpoints run in worker threads (via
    ``background._run_in_pipeline_executor``), so first-use initialization is
    delegated to ``LazySingleton.get_or_create``, which serializes concurrent
    first requests under its own internal lock instead of constructing several
    ``BrandingAssistantAgent`` instances.

    Postconditions:
        Returns the same ``BrandingAssistantAgent`` for the process lifetime
        once construction succeeds. If ``BrandingAssistantAgent()`` raises,
        ``_build_assistant_agent`` converts it to ``HTTPException(503)``,
        which propagates to the caller without marking the singleton
        constructed — matching the prior hand-rolled behavior, the next call
        retries construction. Also honors direct test monkeypatching of the
        module-level ``assistant_agent`` name above: if it is already
        non-``None`` when this is called, that value is returned without
        touching the singleton.
    """
    global assistant_agent
    if assistant_agent is None:
        assistant_agent = _assistant_agent_singleton.get_or_create(_build_assistant_agent)
    return assistant_agent


# Per-brand phase cache (in-memory only; see PhaseOutputCache). Keyed by brand_id,
# mirroring conversation._phase_caches: process-local, never evicted (same no-TTL
# precedent), so a REST run for a given brand can skip unchanged pipeline phases on
# a later run within this process.
_brand_phase_caches: KeyedLazyRegistry[str, PhaseOutputCache] = KeyedLazyRegistry()


def _get_brand_cache(brand_id: str) -> PhaseOutputCache:
    """Return this brand's in-memory phase cache, creating one on first use.

    Preconditions:
        ``brand_id`` is a non-empty string identifying an existing or
        about-to-exist brand.
    Postconditions:
        Returns the same ``PhaseOutputCache`` instance for a given ``brand_id``
        on every call within this process. Thread-safe: first-use construction
        is delegated to ``KeyedLazyRegistry.get_or_create``, which serializes
        concurrent first calls for the same new ``brand_id`` (REST run
        submissions execute on the bounded run executor) under that key's own
        lock — a slow construction for one ``brand_id`` never delays another
        brand's first cache.
    """
    return _brand_phase_caches.get_or_create(brand_id, PhaseOutputCache)


# --- Re-export the load-bearing subset (import + monkeypatch surface). Imported
# after the globals + app above so each module's ``from …api import main as
# _main`` binds a fully-populated hub. Only names actually reached *through*
# main (by a test or another module) are re-exported here — everything else
# moved out of the old monolith is imported directly from its owning module by
# the code that needs it. ---
from branding_team.api.background import (  # noqa: E402,F401
    _run_branding_background,
    _run_branding_core,
    _signal_branding_cancel,
)
from branding_team.api.conversation import _run_orchestrator_if_ready  # noqa: E402,F401

# Mount the concern-grouped routers last, so the route modules'
# ``from …api import main as _main`` binds a fully-populated hub.
from branding_team.api.routes import (  # noqa: E402
    brands,
    clients,
    conversations,
    health,
    integrations,
    runs,
    sessions,
)

app.include_router(clients.router)
app.include_router(brands.router)
app.include_router(runs.router)
app.include_router(integrations.router)
app.include_router(sessions.router)
app.include_router(conversations.router)
app.include_router(health.router)
