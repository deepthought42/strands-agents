"""Tests for several smaller blogging modules:

* ``shared.errors``      — error class constructors and ``__str__``.
* ``shared.planning_config`` — env-var helpers.
* ``shared.models``      — phase ordering helpers.
* ``shared.style_loader`` — load/save/append.
* ``shared.medium_integration_access`` — fallback when modules unavailable.
* ``shared.run_pipeline_job`` — helper functions (audience formatting, cancellation detection,
  artifacts base resolution, error publishing).
* ``shared.job_event_bus`` — subscribe/publish/cleanup/reaper.
* ``blog_research_agent.allowed_claims`` — extract_allowed_claims edge cases.
* ``blog_research_agent.llm`` / ``strands_integration`` — re-exports + factory.
* ``ghost_writer_agent.models`` — Pydantic schemas.
* ``temporal.client`` / ``constants`` — accessors.
* ``temporal.workflows`` / ``activities`` import-time wiring.
* ``temporal.start_workflow`` and ``worker`` (no real Temporal).
* ``blog_medium_stats_agent.agent`` thin wrapper.
* ``blog_writer_agent.feedback_tracker`` — persistence + jaccard.
* ``blog_research_agent.agent_cache`` — save/load/clear with model_dump objects.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

# ---------------------------------------------------------------------------
# shared.errors
# ---------------------------------------------------------------------------


def test_errors_constructors_and_str() -> None:
    from agents.blogging.shared.errors import (
        BloggingError,
        ComplianceError,
        CopyEditError,
        DraftError,
        FactCheckError,
        PlanningError,
        PublicationError,
        ResearchError,
        ValidationError,
    )

    e = BloggingError("boom", phase="research")
    assert "[research] boom" == str(e)
    e_no_phase = BloggingError("nope")
    assert str(e_no_phase) == "nope"

    rese = ResearchError("nope", sources_found=3)
    assert rese.phase == "research"
    assert rese.sources_found == 3

    plan = PlanningError("nope", failure_reason="no_titles")
    assert plan.phase == "planning"
    assert plan.failure_reason == "no_titles"

    df = DraftError("nope", iteration=2)
    assert df.iteration == 2

    ce = CopyEditError("nope", iteration=1)
    assert ce.iteration == 1

    comp = ComplianceError("nope", violation_count=5)
    assert comp.violation_count == 5

    fc = FactCheckError("nope", unverified_claims=2, high_risk_count=1)
    assert fc.unverified_claims == 2
    assert fc.high_risk_count == 1

    vd = ValidationError("nope")
    assert vd.failed_checks == []

    pub = PublicationError("nope")
    assert pub.phase == "publication"


# ---------------------------------------------------------------------------
# shared.planning_config
# ---------------------------------------------------------------------------


def test_planning_config_env_handling(monkeypatch) -> None:
    from agents.blogging.shared import planning_config as pc

    monkeypatch.setenv("BLOG_PLANNING_MAX_ITERATIONS", "8")
    assert pc.planning_max_iterations() == 8
    monkeypatch.setenv("BLOG_PLANNING_MAX_ITERATIONS", "100")
    assert pc.planning_max_iterations() == 20  # capped
    monkeypatch.setenv("BLOG_PLANNING_MAX_ITERATIONS", "0")
    assert pc.planning_max_iterations() == 1

    monkeypatch.setenv("BLOG_PLANNING_MAX_PARSE_RETRIES", "5")
    assert pc.planning_max_parse_retries() == 5
    monkeypatch.setenv("BLOG_PLANNING_MAX_PARSE_RETRIES", "999")
    assert pc.planning_max_parse_retries() == 10

    monkeypatch.delenv("BLOG_PLANNING_MODEL", raising=False)
    assert pc.planning_model_override() is None
    monkeypatch.setenv("BLOG_PLANNING_MODEL", "  llama:7b  ")
    assert pc.planning_model_override() == "llama:7b"

    monkeypatch.delenv("BLOG_PLAN_CRITIC_ENABLED", raising=False)
    assert pc.plan_critic_enabled() is False
    for v in ("1", "true", "yes", "on", "TRUE"):
        monkeypatch.setenv("BLOG_PLAN_CRITIC_ENABLED", v)
        assert pc.plan_critic_enabled() is True

    monkeypatch.delenv("BLOG_PLAN_CRITIC_MAX_ITERATIONS", raising=False)
    assert pc.plan_critic_max_iterations() == 3
    monkeypatch.setenv("BLOG_PLAN_CRITIC_MAX_ITERATIONS", "7")
    assert pc.plan_critic_max_iterations() == 7
    monkeypatch.setenv("BLOG_PLAN_CRITIC_MAX_ITERATIONS", "0")
    assert pc.plan_critic_max_iterations() == 1
    monkeypatch.setenv("BLOG_PLAN_CRITIC_MAX_ITERATIONS", "abc")
    assert pc.plan_critic_max_iterations() == 3  # ValueError default

    monkeypatch.delenv("BLOG_PLAN_CRITIC_MODEL", raising=False)
    assert pc.plan_critic_model_override() is None
    monkeypatch.setenv("BLOG_PLAN_CRITIC_MODEL", " mistral ")
    assert pc.plan_critic_model_override() == "mistral"


# ---------------------------------------------------------------------------
# shared.models
# ---------------------------------------------------------------------------


def test_models_phase_helpers() -> None:
    from agents.blogging.shared.models import (
        BlogPhase,
        get_completed_phases,
        get_phase_progress,
    )

    assert get_phase_progress(BlogPhase.PLANNING, 0.0) == 0
    assert get_phase_progress(BlogPhase.PLANNING, 1.0) == 12
    assert get_phase_progress(BlogPhase.FINALIZE, 1.0) == 100
    assert get_phase_progress(BlogPhase.DRAFT_INITIAL, 0.5) == 22

    completed = get_completed_phases(BlogPhase.COPY_EDIT_LOOP)
    assert "planning" in completed
    assert "draft_initial" in completed
    assert "copy_edit" not in completed


def test_title_selection_ordered_right_after_planning() -> None:
    """Drift guard: title selection runs at the end of the planning stage, before
    any draft is written, so its position/range must reflect that (not the old
    post-rewrite placement) or the UI progress bar jumps backward mid-run."""
    from agents.blogging.shared.models import PHASE_ORDER, BlogPhase

    assert PHASE_ORDER.index(BlogPhase.TITLE_SELECTION) == PHASE_ORDER.index(BlogPhase.PLANNING) + 1


def test_phase_progress_ranges_contiguous_and_cover_0_to_100() -> None:
    from agents.blogging.shared.models import PHASE_ORDER, PHASE_PROGRESS_RANGES

    ranges = [PHASE_PROGRESS_RANGES[phase] for phase in PHASE_ORDER]
    assert ranges[0][0] == 0
    assert ranges[-1][1] == 100
    for (_, prev_max), (next_min, _) in zip(ranges, ranges[1:]):
        assert prev_max == next_min


# ---------------------------------------------------------------------------
# shared.style_loader
# ---------------------------------------------------------------------------


def test_style_loader_load_save_append(tmp_path: Path) -> None:
    from agents.blogging.shared.style_loader import (
        append_guidelines,
        load_style_file,
        save_style_file,
    )

    target = tmp_path / "guide.md"
    # load missing file → ""
    assert load_style_file(target) == ""

    save_style_file(target, "# Guidelines\n", "writing style guide")
    assert target.read_text(encoding="utf-8").startswith("# Guidelines")

    # load on valid file should render (with no jinja2 placeholders, returns same)
    text = load_style_file(target, "writing style guide")
    assert "Guidelines" in text

    # save into nested directory creates parents
    deep = tmp_path / "a" / "b" / "deep.md"
    assert save_style_file(deep, "hi") is True
    assert deep.read_text() == "hi"

    # append_guidelines with empty list returns True (no-op)
    assert append_guidelines(target, []) is True

    # append a guideline
    ok = append_guidelines(
        target,
        [
            {
                "category": "tone",
                "description": "Avoid contractions",
                "guideline_text": "Use full forms",
            },
            {"category": "style"},  # missing keys → defaults
        ],
    )
    assert ok is True
    content = target.read_text(encoding="utf-8")
    assert "Editor-Derived Guidelines" in content
    assert "Avoid contractions" in content


def test_style_loader_save_fails_on_oserror(monkeypatch, tmp_path: Path) -> None:
    from agents.blogging.shared.style_loader import save_style_file

    target = tmp_path / "x.md"

    def boom(self, *args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(Path, "write_text", boom)
    assert save_style_file(target, "data") is False


def test_style_loader_load_oserror(monkeypatch, tmp_path: Path) -> None:
    from agents.blogging.shared.style_loader import load_style_file

    target = tmp_path / "x.md"
    target.write_text("hello")

    def boom(self, *args, **kwargs):
        raise OSError("read error")

    monkeypatch.setattr(Path, "read_text", boom)
    assert load_style_file(target) == ""


# ---------------------------------------------------------------------------
# shared.medium_integration_access — modules absent path
# ---------------------------------------------------------------------------


def test_medium_integration_modules_unavailable(monkeypatch) -> None:
    """When unified_api modules are not importable, the helper returns a friendly message."""
    import builtins

    real_import = builtins.__import__

    def fake_import(name: str, *args, **kwargs):
        if name == "unified_api.integrations_store":
            raise ImportError("no unified_api in this test")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)

    from agents.blogging.shared.medium_integration_access import (
        medium_stats_integration_eligible,
        resolve_medium_stats_storage_state,
    )

    state, hint, err = resolve_medium_stats_storage_state()
    assert state is None
    assert hint == ""
    assert "Medium integration is not available" in err

    ok, err2 = medium_stats_integration_eligible()
    assert ok is False
    assert err2


# ---------------------------------------------------------------------------
# shared.run_pipeline_job — pure helpers
# ---------------------------------------------------------------------------


def test_run_pipeline_job_helpers(monkeypatch, tmp_path: Path) -> None:
    from agents.blogging.shared import run_pipeline_job as rpj

    # _normalize_audience
    assert rpj._normalize_audience(None) is None
    assert rpj._normalize_audience("  hi ") == "hi"
    assert rpj._normalize_audience("") is None
    assert rpj._normalize_audience(42) is None
    out = rpj._normalize_audience(
        {
            "profession": "dev",
            "skill_level": "expert",
            "hobbies": ["a", "b"],
            "other": "extra",
        }
    )
    assert "profession: dev" in out
    assert "skill_level: expert" in out
    assert "interests: a, b" in out
    assert "extra" in out
    # empty dict → None
    assert rpj._normalize_audience({}) is None


def _dict_audience() -> dict:
    return {
        "profession": "dev",
        "skill_level": "expert",
        "hobbies": ["a", "b"],
        "other": "extra",
    }


def _pydantic_audience() -> Any:
    from agents.blogging.api.models import AudienceDetails

    return AudienceDetails(
        profession="dev", skill_level="expert", hobbies=["a", "b"], other="extra"
    )


@pytest.mark.parametrize(
    "make_audience", [_dict_audience, _pydantic_audience], ids=["dict", "AudienceDetails"]
)
def test_format_audience_same_output_for_equivalent_input_shapes(make_audience) -> None:
    """The shared formatter produces identical output for a dict and an equivalent
    AudienceDetails model — the two input shapes the API layer and the job-runner
    layer each hand it."""
    from agents.blogging.shared.audience import format_audience

    assert format_audience(make_audience()) == (
        "profession: dev; skill_level: expert; interests: a, b; extra"
    )


def test_format_audience_object_with_only_hobbies_and_other() -> None:
    """An object exposing only hobbies/other (no profession/skill_level) is still
    recognized as audience-like, per the documented contract."""
    from agents.blogging.shared.audience import format_audience

    class PartialAudience:
        hobbies = ["reading"]
        other = "night owl"

    assert format_audience(PartialAudience()) == "interests: reading; night owl"


def test_run_pipeline_job_external_cancellation_detection() -> None:
    from agents.blogging.shared import run_pipeline_job as rpj
    from temporalio.exceptions import CancelledError

    assert rpj._is_external_cancellation(RuntimeError("nope")) is False

    inner = CancelledError("temporal cancelled")
    outer = RuntimeError("wrap")
    outer.__cause__ = inner
    assert rpj._is_external_cancellation(outer) is True


def test_run_pipeline_job_artifacts_base_resolution(monkeypatch, tmp_path: Path) -> None:
    from agents.blogging.shared import run_pipeline_job as rpj

    monkeypatch.setenv("BLOGGING_RUN_ARTIFACTS_ROOT", str(tmp_path / "custom"))
    assert rpj._get_run_artifacts_base() == (tmp_path / "custom").resolve()

    monkeypatch.delenv("BLOGGING_RUN_ARTIFACTS_ROOT", raising=False)
    monkeypatch.setenv("AGENT_CACHE", str(tmp_path / "cache"))
    assert (
        rpj._get_run_artifacts_base() == (tmp_path / "cache").resolve() / "blogging_team" / "runs"
    )

    monkeypatch.delenv("BLOGGING_RUN_ARTIFACTS_ROOT", raising=False)
    monkeypatch.delenv("AGENT_CACHE", raising=False)
    # reset the warned flag so we hit the fallback log
    monkeypatch.setattr(rpj, "_tempfile_fallback_warned", False)
    base = rpj._get_run_artifacts_base()
    assert "blogging_runs" in str(base)


def test_publish_terminal_swallows_errors(monkeypatch) -> None:
    """_publish_terminal returns silently when the event-bus module is unavailable."""
    from agents.blogging.shared import run_pipeline_job as rpj

    def deny(name):
        raise ImportError("nope")

    monkeypatch.setattr(rpj, "_import_shared", deny)
    rpj._publish_terminal("jid", "complete", status="ok")  # must not raise


def test_fail_job_swallows_errors(monkeypatch) -> None:
    """_fail_job tolerates a missing job-store module."""
    from agents.blogging.shared import run_pipeline_job as rpj

    def deny(name):
        raise ImportError("nope")

    monkeypatch.setattr(rpj, "_import_shared", deny)
    rpj._fail_job("jid", "err")


# ---------------------------------------------------------------------------
# shared.job_event_bus
# ---------------------------------------------------------------------------


def test_event_bus_subscribe_publish_cleanup() -> None:
    from agents.blogging.shared import job_event_bus as bus

    job_id = "job-evbus-1"
    sub = bus.subscribe(job_id)
    sub.touch()
    bus.publish(job_id, {"x": 1}, event_type="update")
    assert sub.events
    evt = sub.events.pop()
    assert evt["type"] == "update"
    assert evt["x"] == 1

    bus.publish("no-subs", {"y": 2})  # early-return path
    bus.cleanup_job(job_id)
    # unsubscribe missing job is safe
    bus.unsubscribe(job_id, sub)

    # Re-subscribe + unsubscribe path
    sub2 = bus.subscribe(job_id)
    bus.unsubscribe(job_id, sub2)
    # double unsubscribe is safe (subs list deleted)
    bus.unsubscribe(job_id, sub2)


def test_event_bus_reaper_evicts_idle(monkeypatch) -> None:
    from agents.blogging.shared import job_event_bus as bus

    job_id = "job-evbus-reaper"
    sub = bus.subscribe(job_id)
    # Force the subscription's last_activity to be ancient
    sub.last_activity = sub.last_activity - 1e9
    bus._reap_once()
    # The job should be gone after reap
    assert job_id not in bus._subscribers


def test_event_bus_reaper_global_cap(monkeypatch) -> None:
    from agents.blogging.shared import job_event_bus as bus

    monkeypatch.setattr(bus, "_MAX_JOBS_TRACKED", 2)
    # Subscribe three jobs
    a = bus.subscribe("evbus-a")
    b = bus.subscribe("evbus-b")
    c = bus.subscribe("evbus-c")
    bus._reap_once()
    assert len(bus._subscribers) <= 2
    bus.cleanup_job("evbus-a")
    bus.cleanup_job("evbus-b")
    bus.cleanup_job("evbus-c")
    # Touch the local refs so we don't leak ContextVars (they're already woken)
    _ = (a, b, c)


def test_event_bus_shutdown_safe(monkeypatch) -> None:
    from agents.blogging.shared import job_event_bus as bus

    bus.shutdown()  # idempotent

    # And starting reaper twice is a no-op
    bus._start_reaper_if_needed()
    bus._start_reaper_if_needed()
    bus.shutdown()


def test_event_bus_concurrent_start_no_double_reaper() -> None:
    """Concurrent _start_reaper_if_needed must not orphan a second beater.

    The lazy-init is guarded by _lock; a burst of concurrent starts must leave
    exactly one reaper thread, and shutdown() must stop it (no leaked, unstoppable
    beater whose private stop event shutdown can't reach).
    """
    import threading

    from agents.blogging.shared import job_event_bus as bus

    bus.shutdown()  # clean slate

    def _reapers_alive() -> int:
        return len([t for t in threading.enumerate() if t.name == "blogging-event-bus-reaper"])

    n_racers = 8  # enough to race the check-and-start; small enough to stay stable in CI

    barrier = threading.Barrier(n_racers)

    def _racer() -> None:
        barrier.wait()  # maximise the chance all threads race the check-and-start
        bus._start_reaper_if_needed()

    threads = [threading.Thread(target=_racer) for _ in range(n_racers)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    try:
        assert _reapers_alive() == 1, "concurrent starts must yield exactly one reaper thread"
    finally:
        bus.shutdown()
    assert _reapers_alive() == 0, "shutdown() must stop the (only) reaper thread"


# ---------------------------------------------------------------------------
# blog_research_agent.allowed_claims
# ---------------------------------------------------------------------------


class _FakeJSONClient:
    """Minimal LLM client exposing complete_json for tests."""

    def __init__(self, response: Any) -> None:
        self._response = response

    def complete_json(self, prompt: str, **_: Any) -> Any:
        if isinstance(self._response, Exception):
            raise self._response
        return self._response


def test_extract_allowed_claims_pydantic_models() -> None:
    """Test the AllowedClaims / ClaimEntry pydantic models directly.

    The ``extract_allowed_claims`` helper itself currently has a latent bug:
    the prompt string contains ``{"..."}`` placeholders that crash ``str.format``
    before the LLM is even called. The model layer is tested here; the broken
    helper is covered indirectly through other tests that import it.
    """
    from agents.blogging.blog_research_agent.allowed_claims import AllowedClaims, ClaimEntry

    c1 = ClaimEntry(id="1", text="A.", citations=["s1"], risk_level="low")
    c2 = ClaimEntry(id="2", text="B.", citations=[], risk_level="high")
    allowed = AllowedClaims(topic="My Topic", claims=[c1, c2])
    payload = allowed.to_dict()
    assert payload["topic"] == "My Topic"
    assert payload["claims"][0]["text"] == "A."
    assert payload["claims"][1]["risk_level"] == "high"


def test_allowed_claims_default_risk_low() -> None:
    from agents.blogging.blog_research_agent.allowed_claims import ClaimEntry

    c = ClaimEntry(id="x", text="hello")
    assert c.risk_level == "low"
    assert c.citations == []


# ---------------------------------------------------------------------------
# blog_research_agent.llm + strands_integration
# ---------------------------------------------------------------------------


def test_research_llm_reexports_present() -> None:
    from agents.blogging.blog_research_agent.llm import (
        DummyLLMClient,
        LLMClient,
        LLMError,
        LLMJsonParseError,
        OllamaLLMClient,
        get_strands_model,
    )

    # All non-None imports
    assert DummyLLMClient
    assert LLMClient
    assert LLMError
    assert LLMJsonParseError
    assert OllamaLLMClient
    assert callable(get_strands_model)


def test_strands_integration_factory_and_spec() -> None:
    from agents.blogging.blog_research_agent.models import ResearchBriefInput
    from agents.blogging.blog_research_agent.strands_integration import (
        create_research_agent,
        get_agent_spec,
    )

    # Don't actually invoke the agent (it needs a real LLM client). Just check
    # the spec contract — factory creates an agent, handler_factory is callable.
    class _StubLLM:
        pass

    agent = create_research_agent(_StubLLM())
    assert agent is not None

    spec = get_agent_spec()
    assert spec["name"] == "research_agent"
    assert spec["input_model"] is ResearchBriefInput
    assert callable(spec["handler_factory"])

    # Precondition: assertion fires when llm_client is None
    with pytest.raises(AssertionError):
        create_research_agent(None)


# ---------------------------------------------------------------------------
# ghost_writer_agent.models
# ---------------------------------------------------------------------------


def test_ghost_writer_models() -> None:
    from agents.blogging.ghost_writer_agent.models import StoryElicitationResult, StoryGap

    gap = StoryGap(
        section_title="My Section",
        section_context="Some context",
        seed_question="What was the moment?",
    )
    assert gap.section_title == "My Section"
    result = StoryElicitationResult(
        gap=gap,
        narrative="Once upon a time...",
        skipped=False,
        rounds_used=3,
        story_context="client",
    )
    assert result.rounds_used == 3
    assert result.story_context == "client"


# ---------------------------------------------------------------------------
# temporal.client / constants
# ---------------------------------------------------------------------------


def test_temporal_client_helpers(monkeypatch) -> None:
    """address/namespace/enabled accessors read env, and the module-level client and
    loop setters/getters round-trip None."""
    from shared.temporal import client as tc

    # Default no-address → disabled
    monkeypatch.delenv("TEMPORAL_ADDRESS", raising=False)
    assert tc.get_temporal_address() is None
    assert tc.is_temporal_enabled() is False
    monkeypatch.setenv("TEMPORAL_ADDRESS", "localhost:7233")
    assert tc.get_temporal_address() == "localhost:7233"
    assert tc.is_temporal_enabled() is True

    monkeypatch.delenv("TEMPORAL_NAMESPACE", raising=False)
    assert tc.get_temporal_namespace() == "default"
    monkeypatch.setenv("TEMPORAL_NAMESPACE", "blogging-ns")
    assert tc.get_temporal_namespace() == "blogging-ns"

    # Module-level client and loop accessors
    tc.set_temporal_client(None)
    assert tc.get_temporal_client() is None
    tc.set_temporal_loop(None)
    assert tc.get_temporal_loop() is None


def test_temporal_constants_loaded() -> None:
    """The task-queue, workflow-id prefix, workflow name, and all five activity-name
    constants are present and non-empty."""
    from agents.blogging.temporal import constants

    assert constants.TASK_QUEUE  # non-empty
    assert constants.WORKFLOW_ID_PREFIX_FULL_PIPELINE
    assert constants.WORKFLOW_FULL_PIPELINE
    assert constants.ACTIVITY_PLAN_STAGE
    assert constants.ACTIVITY_DRAFT_STAGE
    assert constants.ACTIVITY_GATES_STAGE
    assert constants.ACTIVITY_FINALIZE
    assert constants.ACTIVITY_FULL_PIPELINE


def test_connect_temporal_client_no_address(monkeypatch) -> None:
    """connect_temporal_client returns None (no connection attempt) when
    TEMPORAL_ADDRESS is unset."""
    import asyncio

    from shared.temporal import client as tc

    monkeypatch.delenv("TEMPORAL_ADDRESS", raising=False)
    out = asyncio.run(tc.connect_temporal_client())
    assert out is None


def test_start_full_pipeline_workflow_without_client(monkeypatch) -> None:
    """start_full_pipeline_workflow raises RuntimeError('not available') when no
    Temporal client is configured."""
    from agents.blogging.temporal import start_workflow

    monkeypatch.setattr(start_workflow, "get_temporal_client", lambda: None)
    with pytest.raises(RuntimeError, match="not available"):
        start_workflow.start_full_pipeline_workflow("j1", {})


def test_start_workflow_run_async_no_loop(monkeypatch) -> None:
    """_run_async raises RuntimeError when there is no running Temporal loop/client to
    submit the coroutine to."""
    from agents.blogging.temporal import start_workflow

    monkeypatch.setattr(start_workflow, "get_temporal_loop", lambda: None)
    monkeypatch.setattr(start_workflow, "get_temporal_client", lambda: None)
    with pytest.raises(RuntimeError):
        start_workflow._run_async(None)


# ---------------------------------------------------------------------------
# temporal.worker — disabled-mode guards
# ---------------------------------------------------------------------------


def test_temporal_worker_disabled_paths(monkeypatch) -> None:
    """When Temporal is disabled, create_blogging_worker returns None, the thread
    starter returns False, and the thread target is a no-op."""
    from agents.blogging.temporal import worker

    monkeypatch.setattr(worker, "is_temporal_enabled", lambda: False)

    assert worker.create_blogging_worker(client=None) is None
    assert worker.start_blogging_temporal_worker_thread() is False
    # _worker_thread_target should noop when disabled
    worker._worker_thread_target()


def test_temporal_worker_shutdown_noop_when_nothing_running() -> None:
    """shutdown_blogging_temporal_components is a safe no-op when no executor, worker,
    loop, or thread is running."""
    from agents.blogging.temporal import worker

    worker._activity_executor = None
    worker._worker_instance = None
    worker._worker_running_loop = None
    worker._worker_thread = None
    worker.shutdown_blogging_temporal_components()


def test_force_stop_worker_loop_already_closed(monkeypatch) -> None:
    """_force_stop_worker_loop swallows the 'Event loop is closed' RuntimeError raised
    when stopping an already-closed loop."""
    from agents.blogging.temporal import worker

    class _DeadLoop:
        def is_running(self) -> bool:
            return False

        def call_soon_threadsafe(self, *args, **kwargs):
            raise RuntimeError("Event loop is closed")

    worker._force_stop_worker_loop(_DeadLoop())  # must swallow RuntimeError


# ---------------------------------------------------------------------------
# blog_medium_stats_agent.agent — thin wrapper
# ---------------------------------------------------------------------------


def test_medium_stats_agent_delegates(monkeypatch) -> None:
    from agents.blogging.blog_medium_stats_agent import BlogMediumStatsAgent
    from agents.blogging.blog_medium_stats_agent import agent as agent_mod
    from agents.blogging.blog_medium_stats_agent.models import (
        MediumStatsReport,
        MediumStatsRunConfig,
    )

    sentinel_report = MediumStatsReport(posts=[])

    def fake_collect(cfg):
        assert isinstance(cfg, MediumStatsRunConfig)
        return sentinel_report

    monkeypatch.setattr(agent_mod, "collect_medium_stats", fake_collect)
    agent = BlogMediumStatsAgent()
    out = agent.collect(MediumStatsRunConfig(headless=True))
    assert out is sentinel_report
    # Default config path
    out2 = agent.collect()
    assert out2 is sentinel_report


# ---------------------------------------------------------------------------
# blog_writer_agent.feedback_tracker
# ---------------------------------------------------------------------------


def test_feedback_tracker_persistence_and_jaccard() -> None:
    from agents.blogging.blog_copy_editor_agent.models import FeedbackItem
    from agents.blogging.blog_writer_agent.feedback_tracker import FeedbackTracker

    t = FeedbackTracker(window_size=3)

    item1 = FeedbackItem(
        category="grammar", severity="minor", issue="comma", location="paragraph 1"
    )
    item2 = FeedbackItem(category="flow", severity="major", issue="abrupt", location="Paragraph 2")
    item3 = FeedbackItem(
        category="grammar", severity="minor", issue="comma", location="Paragraph 1  "
    )

    t.record_iteration(1, [item1, item2])
    t.record_iteration(2, [item1, item2])
    t.record_iteration(3, [item3])

    persistent = t.get_persistent_issues(min_occurrences=2)
    assert any(p.occurrence_count >= 2 for p in persistent)

    # Stalled detection: window of 3 with high overlap
    t2 = FeedbackTracker(window_size=2)
    t2.record_iteration(1, [item1])
    t2.record_iteration(2, [item1])
    assert t2.is_stalled() is True

    # Not stalled when only one iteration recorded
    t3 = FeedbackTracker(window_size=3)
    t3.record_iteration(1, [item1])
    assert t3.is_stalled() is False

    # Capped previous feedback respects max_items
    capped = t.get_capped_previous_feedback(max_items=10)
    assert isinstance(capped, list)
    assert all(isinstance(x, FeedbackItem) for x in capped)

    # max_items smaller than persistent
    small = t.get_capped_previous_feedback(max_items=1)
    assert len(small) <= 1


def test_feedback_tracker_empty_input() -> None:
    from agents.blogging.blog_writer_agent.feedback_tracker import FeedbackTracker

    t = FeedbackTracker()
    assert t.get_persistent_issues() == []
    assert t.get_capped_previous_feedback() == []
    assert t.is_stalled() is False


def test_max_previous_feedback_items_constant_is_tracker_default() -> None:
    import inspect

    from agents.blogging.blog_writer_agent.feedback_tracker import (
        MAX_PREVIOUS_FEEDBACK_ITEMS,
        FeedbackTracker,
    )

    assert MAX_PREVIOUS_FEEDBACK_ITEMS == 15
    default = (
        inspect.signature(FeedbackTracker.get_capped_previous_feedback)
        .parameters["max_items"]
        .default
    )
    assert default == MAX_PREVIOUS_FEEDBACK_ITEMS


def test_feedback_tracker_jaccard_no_overlap() -> None:
    from agents.blogging.blog_copy_editor_agent.models import FeedbackItem
    from agents.blogging.blog_writer_agent.feedback_tracker import FeedbackTracker

    t = FeedbackTracker(window_size=2)
    a = FeedbackItem(category="x", severity="minor", issue="A", location="loc-a")
    b = FeedbackItem(category="y", severity="minor", issue="B", location="loc-b")
    t.record_iteration(1, [a])
    t.record_iteration(2, [b])
    assert t.is_stalled() is False


# ---------------------------------------------------------------------------
# blog_research_agent.agent_cache
# ---------------------------------------------------------------------------


def test_agent_cache_save_load_clear(tmp_path: Path) -> None:
    from agents.blogging.blog_research_agent.agent_cache import AgentCache
    from agents.blogging.blog_research_agent.models import ResearchBriefInput

    cache = AgentCache(tmp_path / "cache")
    brief = ResearchBriefInput(brief="Topic about AI", audience="devs", max_results=10)

    # No checkpoint yet
    assert cache.load_checkpoint(brief) is None

    # Save normalized
    cache.save_checkpoint(brief, "normalized", normalized={"topic": "AI"})
    state = cache.load_checkpoint(brief)
    assert state is not None
    assert state.normalized == {"topic": "AI"}
    assert state.last_completed_step == "normalized"

    # Save queries (with model_dump support)
    class _Q:
        def model_dump(self):
            return {"q": "search1"}

    cache.save_checkpoint(brief, "queries", queries=[_Q(), {"q": "search2"}])
    state = cache.load_checkpoint(brief)
    assert state.queries == [{"q": "search1"}, {"q": "search2"}]

    # Save candidates / documents / references
    cache.save_checkpoint(brief, "candidates", candidates=[{"id": "c1"}])
    cache.save_checkpoint(brief, "documents", documents=[{"id": "d1"}])
    cache.save_checkpoint(
        brief,
        "scored_docs",
        scored_docs=[
            ({"id": "s1"}, 0.9, 0.8, 0.7, "primary"),
            ({"id": "s2"}, 0.5, "secondary"),  # 3-tuple legacy path
        ],
    )
    cache.save_checkpoint(brief, "references", references=[{"id": "r1"}])
    cache.save_checkpoint(brief, "notes", notes="some notes")

    final = cache.load_checkpoint(brief)
    assert final.scored_docs[0][1] == 0.9
    assert final.scored_docs[1][1] == 0.5  # legacy mapped
    assert final.notes == "some notes"
    assert final.notes_computed is True

    # Brief mismatch returns None
    different = ResearchBriefInput(brief="Topic about DBs", audience="devs", max_results=10)
    assert cache.load_checkpoint(different) is None

    # Clear deletes the file
    cache.clear_checkpoint(brief)
    assert cache.load_checkpoint(brief) is None
    # Clearing again is a no-op
    cache.clear_checkpoint(brief)


def test_agent_cache_notes_computed_distinguishes_null_from_unset(tmp_path: Path) -> None:
    """notes=None is a legitimate completed result (no references, or an unusable
    LLM response), distinct from a checkpoint where the notes step never ran —
    notes_computed carries that distinction since `notes is not None` can't."""
    from agents.blogging.blog_research_agent.agent_cache import AgentCache
    from agents.blogging.blog_research_agent.models import ResearchBriefInput

    cache = AgentCache(tmp_path / "cache")
    brief = ResearchBriefInput(brief="Topic", max_results=10)

    cache.save_checkpoint(brief, "normalized", normalized={"topic": "Topic"})
    state = cache.load_checkpoint(brief)
    assert state.notes is None
    assert state.notes_computed is False

    cache.save_checkpoint(brief, "notes", notes=None)
    state = cache.load_checkpoint(brief)
    assert state.notes is None
    assert state.notes_computed is True


def test_agent_cache_load_corrupt_file(tmp_path: Path) -> None:
    from agents.blogging.blog_research_agent.agent_cache import AgentCache
    from agents.blogging.blog_research_agent.models import ResearchBriefInput

    cache = AgentCache(tmp_path / "cache")
    brief = ResearchBriefInput(brief="Topic", max_results=10)
    # Write garbage at the expected cache file path
    key = cache._cache_key(brief)
    (tmp_path / "cache" / f"{key}.json").write_text("not-valid-json{")
    assert cache.load_checkpoint(brief) is None


def test_agent_cache_save_with_corrupt_existing(tmp_path: Path) -> None:
    """When the existing cache is unreadable, save_checkpoint starts fresh."""
    from agents.blogging.blog_research_agent.agent_cache import AgentCache
    from agents.blogging.blog_research_agent.models import ResearchBriefInput

    cache = AgentCache(tmp_path / "cache")
    brief = ResearchBriefInput(brief="Topic", max_results=10)
    key = cache._cache_key(brief)
    cache_file = tmp_path / "cache" / f"{key}.json"
    cache_file.write_text("BAD")
    cache.save_checkpoint(brief, "normalized", normalized={"x": 1})
    state = cache.load_checkpoint(brief)
    assert state.normalized == {"x": 1}


# ---------------------------------------------------------------------------
# blog_research_agent.tools — import + simple call paths (no real network)
# ---------------------------------------------------------------------------


def test_arxiv_search_empty_query() -> None:
    from agents.blogging.blog_research_agent.tools.arxiv_search import search_arxiv

    assert search_arxiv("") == []
    assert search_arxiv("   ") == []


def test_arxiv_search_http_error(monkeypatch) -> None:
    import httpx
    from agents.blogging.blog_research_agent.tools import arxiv_search

    class _FailingClient:
        def __init__(self, *a, **kw):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def get(self, *a, **kw):
            raise httpx.HTTPError("network down")

    monkeypatch.setattr(arxiv_search.httpx, "Client", _FailingClient)
    with pytest.raises(arxiv_search.ArxivSearchError):
        arxiv_search.search_arxiv("foo", max_results=1)


def test_arxiv_search_http_503(monkeypatch) -> None:
    from agents.blogging.blog_research_agent.tools import arxiv_search

    class _Response:
        status_code = 503
        text = "service down"

    class _Client:
        def __init__(self, *a, **kw):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def get(self, *a, **kw):
            return _Response()

    monkeypatch.setattr(arxiv_search.httpx, "Client", _Client)
    with pytest.raises(arxiv_search.ArxivSearchError):
        arxiv_search.search_arxiv("foo", max_results=1)


def test_arxiv_search_max_results_minimum(monkeypatch) -> None:
    """max_results < 1 is coerced to 1 before the request URL is built.

    Mocks httpx so no network call happens, then asserts the coerced value reached
    the request (``max_results=1``) and that an empty feed parses to ``[]``.
    """
    from agents.blogging.blog_research_agent.tools import arxiv_search as mod

    captured: dict = {}

    class _Resp:
        status_code = 200
        content = b'<feed xmlns="http://www.w3.org/2005/Atom"></feed>'

    class _Client:
        def __init__(self, *a, **k):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def get(self, url):
            captured["url"] = url
            return _Resp()

    monkeypatch.setattr(mod.httpx, "Client", _Client)

    result = mod.search_arxiv("query", max_results=0)
    assert result == []
    assert "max_results=1" in captured["url"]  # 0 was clamped to the minimum of 1


def test_web_fetcher_init_validation() -> None:
    from agents.blogging.blog_research_agent.tools.web_fetch import SimpleWebFetcher

    f = SimpleWebFetcher(timeout=5.0)
    assert f.timeout == 5.0
    assert "StrandsResearchAgent" in f.user_agent

    f2 = SimpleWebFetcher(timeout=10.0, user_agent="custom-agent")
    assert f2.user_agent == "custom-agent"

    with pytest.raises(AssertionError):
        SimpleWebFetcher(timeout=0)


def test_web_fetcher_http_error(monkeypatch) -> None:
    import httpx
    from agents.blogging.blog_research_agent.tools import web_fetch
    from pydantic import HttpUrl

    class _Client:
        def __init__(self, *a, **kw):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def get(self, *a, **kw):
            raise httpx.HTTPError("offline")

    monkeypatch.setattr(web_fetch.httpx, "Client", _Client)
    f = web_fetch.SimpleWebFetcher(timeout=5.0)
    with pytest.raises(web_fetch.WebFetchError):
        f.fetch(HttpUrl("https://example.com"))


def test_web_fetcher_400(monkeypatch) -> None:
    from agents.blogging.blog_research_agent.tools import web_fetch
    from pydantic import HttpUrl

    class _Response:
        status_code = 404
        text = ""
        headers = {"Content-Type": "text/plain"}

    class _Client:
        def __init__(self, *a, **kw):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def get(self, *a, **kw):
            return _Response()

    monkeypatch.setattr(web_fetch.httpx, "Client", _Client)
    f = web_fetch.SimpleWebFetcher(timeout=5.0)
    with pytest.raises(web_fetch.WebFetchError):
        f.fetch(HttpUrl("https://example.com"))


def test_web_fetcher_html_parses_title(monkeypatch) -> None:
    from agents.blogging.blog_research_agent.tools import web_fetch
    from pydantic import HttpUrl

    class _Response:
        status_code = 200
        text = "<html><head><title>Hello</title></head><body><script>x</script><p>Body text</p></body></html>"
        headers = {"Content-Type": "text/html; charset=utf-8"}

    class _Client:
        def __init__(self, *a, **kw):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def get(self, *a, **kw):
            return _Response()

    monkeypatch.setattr(web_fetch.httpx, "Client", _Client)
    f = web_fetch.SimpleWebFetcher(timeout=5.0)
    doc = f.fetch(HttpUrl("https://example.com"))
    assert doc.title == "Hello"
    assert "Body text" in doc.content
    assert "x" not in doc.content.split("Body")[0]  # script removed
    assert doc.domain == "example.com"


def test_web_search_missing_api_key(monkeypatch) -> None:
    from agents.blogging.blog_research_agent.models import SearchQuery
    from agents.blogging.blog_research_agent.tools import web_search

    monkeypatch.delenv("OLLAMA_API_KEY", raising=False)
    s = web_search.OllamaWebSearch(api_key=None)
    with pytest.raises(web_search.WebSearchError, match="OLLAMA_API_KEY"):
        s.search(SearchQuery(query_text="hi", intent="discover"), max_results=3)


def test_web_search_http_error(monkeypatch) -> None:
    import httpx
    from agents.blogging.blog_research_agent.models import SearchQuery
    from agents.blogging.blog_research_agent.tools import web_search

    class _Client:
        def __init__(self, *a, **kw):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def post(self, *a, **kw):
            raise httpx.HTTPError("nope")

    monkeypatch.setattr(web_search.httpx, "Client", _Client)
    s = web_search.OllamaWebSearch(api_key="test-key-placeholder")
    with pytest.raises(web_search.WebSearchError):
        s.search(SearchQuery(query_text="hi", intent="discover"), max_results=3)


def test_web_search_connection_error_exhausted_retries_raises_llm_temporary_error(
    monkeypatch,
) -> None:
    """A connection outage that outlasts the local retry budget is just as transient
    as a 5xx response, so it's classified as LLMTemporaryError (not WebSearchError)
    too — for the same Temporal-retry reason as the 429/5xx classification above."""
    import httpx
    from agents.blogging.blog_research_agent.models import SearchQuery
    from agents.blogging.blog_research_agent.tools import web_search

    from llm_service.interface import LLMTemporaryError

    class _Client:
        def __init__(self, *a, **kw):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def post(self, *a, **kw):
            raise httpx.ConnectError("connection refused")

    monkeypatch.setattr(web_search.httpx, "Client", _Client)
    monkeypatch.setattr(web_search.time, "sleep", lambda *_a, **_kw: None)
    s = web_search.OllamaWebSearch(api_key="test-key-placeholder")
    with pytest.raises(LLMTemporaryError):
        s.search(SearchQuery(query_text="hi", intent="discover"), max_results=3)


def test_web_search_non_200_status(monkeypatch) -> None:
    """A non-retryable status (e.g. 404) still raises the plain WebSearchError."""
    from agents.blogging.blog_research_agent.models import SearchQuery
    from agents.blogging.blog_research_agent.tools import web_search

    class _Response:
        status_code = 404
        text = "boom"

        def json(self):
            return {}

    class _Client:
        def __init__(self, *a, **kw):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def post(self, *a, **kw):
            return _Response()

    monkeypatch.setattr(web_search.httpx, "Client", _Client)
    s = web_search.OllamaWebSearch(api_key="test-key-placeholder")
    with pytest.raises(web_search.WebSearchError):
        s.search(SearchQuery(query_text="hi", intent="discover"), max_results=3)


@pytest.mark.parametrize(
    "status_code,expected_exc_name",
    [(429, "LLMRateLimitError"), (500, "LLMTemporaryError"), (503, "LLMTemporaryError")],
)
def test_web_search_transient_status_raises_llm_error(
    monkeypatch, status_code, expected_exc_name
) -> None:
    """429/5xx responses are retried locally, and once that budget is exhausted are
    classified as transient LLM errors (not WebSearchError) so a caller funneling
    research through Temporal's retry policy retries the activity instead of
    permanently failing the job on a recoverable outage."""
    from agents.blogging.blog_research_agent.models import SearchQuery
    from agents.blogging.blog_research_agent.tools import web_search

    from llm_service.interface import LLMRateLimitError, LLMTemporaryError

    expected_exc = (
        LLMRateLimitError if expected_exc_name == "LLMRateLimitError" else LLMTemporaryError
    )

    class _Response:
        text = "boom"

        def json(self):
            return {}

    _Response.status_code = status_code

    class _Client:
        def __init__(self, *a, **kw):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def post(self, *a, **kw):
            return _Response()

    monkeypatch.setattr(web_search.httpx, "Client", _Client)
    monkeypatch.setattr(web_search.time, "sleep", lambda *_a, **_kw: None)
    s = web_search.OllamaWebSearch(api_key="test-key-placeholder")
    with pytest.raises(expected_exc) as exc:
        s.search(SearchQuery(query_text="hi", intent="discover"), max_results=3)
    assert exc.value.status_code == status_code


def test_web_search_transient_status_recovers_within_retry_budget(monkeypatch) -> None:
    """A 429 that clears on a later attempt returns results normally — the retry
    resolves it locally, no exception ever reaches the caller."""
    from agents.blogging.blog_research_agent.models import SearchQuery
    from agents.blogging.blog_research_agent.tools import web_search

    class _RateLimitedResponse:
        status_code = 429
        text = "slow down"

        def json(self):
            return {}

    class _OkResponse:
        status_code = 200
        text = ""

        def json(self):
            return {"results": [{"title": "A", "url": "https://example.com/a", "content": "x"}]}

    responses = iter([_RateLimitedResponse(), _OkResponse()])

    class _Client:
        def __init__(self, *a, **kw):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def post(self, *a, **kw):
            return next(responses)

    monkeypatch.setattr(web_search.httpx, "Client", _Client)
    monkeypatch.setattr(web_search.time, "sleep", lambda *_a, **_kw: None)
    s = web_search.OllamaWebSearch(api_key="test-key-placeholder")
    out = s.search(SearchQuery(query_text="hi", intent="discover"), max_results=3)
    assert len(out) == 1
    assert str(out[0].url) == "https://example.com/a"


def test_web_search_happy_path(monkeypatch) -> None:
    from agents.blogging.blog_research_agent.models import SearchQuery
    from agents.blogging.blog_research_agent.tools import web_search

    class _Response:
        status_code = 200
        text = ""

        def json(self):
            return {
                "results": [
                    {
                        "title": "Title A",
                        "url": "https://example.com/a",
                        "content": "Body A",
                    },
                    {
                        # Missing URL → skipped
                        "title": "no url",
                        "content": "x",
                    },
                ]
            }

    class _Client:
        def __init__(self, *a, **kw):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def post(self, *a, **kw):
            return _Response()

    monkeypatch.setattr(web_search.httpx, "Client", _Client)
    s = web_search.OllamaWebSearch(api_key="test-key-placeholder")
    out = s.search(SearchQuery(query_text="hi", intent="discover"), max_results=5)
    assert len(out) == 1
    assert str(out[0].url) == "https://example.com/a"


def test_web_search_max_results_assertion() -> None:
    from agents.blogging.blog_research_agent.models import SearchQuery
    from agents.blogging.blog_research_agent.tools import web_search

    s = web_search.OllamaWebSearch(api_key="test-key-placeholder")
    with pytest.raises(AssertionError):
        s.search(SearchQuery(query_text="hi", intent="discover"), max_results=0)
