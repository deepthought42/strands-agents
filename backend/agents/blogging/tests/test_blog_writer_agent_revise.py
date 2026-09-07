"""Tests for BlogWriterAgent.revise (plan-first batch feedback processing).

Uses the shared ContentPlan factory from ``_content_plan_test_utils``.
"""

from __future__ import annotations

from typing import Any

from agents.blogging.blog_copy_editor_agent.models import FeedbackItem
from agents.blogging.blog_writer_agent import ReviseWriterInput
from agents.blogging.shared.content_plan import ContentPlan, ContentPlanSection, TitleCandidate

from llm_service import DummyLLMClient

from ._content_plan_test_utils import make_content_plan
from .conftest import make_writer_agent


def _minimal_plan() -> ContentPlan:
    return make_content_plan(
        overarching_topic="Test topic",
        narrative_flow="Intro, main, wrap.",
        sections=[
            ContentPlanSection(title="Intro", coverage_description="Hook", order=0),
        ],
        title_candidates=[TitleCandidate(title="T1", probability_of_success=0.5)],
    )


class _ReviseTrackingLLM(DummyLLMClient):
    """A DummyLLMClient subclass that tracks calls and returns canned responses for the revise flow.

    The first call (revision plan) returns a structured JSON plan.
    Subsequent calls (apply revision) return a hybrid draft format.
    """

    def __init__(self) -> None:
        super().__init__()
        self._call_index = 0
        self.captured_prompts: list[str] = []

    def complete_json(self, prompt: str, **kwargs: Any) -> dict:
        self._request_count += 1
        self._call_index += 1
        self.captured_prompts.append(prompt)
        if self._call_index == 1:
            # First call: revision plan
            return {
                "summary": "Fix opening hook and tighten section two.",
                "changes": [
                    {
                        "section": "intro",
                        "feedback_ids": [1],
                        "action": "rewrite",
                        "rationale": "Weak opening.",
                    },
                    {
                        "section": "section two",
                        "feedback_ids": [2],
                        "action": "rephrase",
                        "rationale": "Drags.",
                    },
                ],
                "risks": [],
            }
        # Subsequent calls: return draft
        return {"draft": "# Revised title\n\nBody here."}


def test_revise_generates_plan_then_applies_all_feedback() -> None:
    llm = _ReviseTrackingLLM()
    agent = make_writer_agent(
        llm_client=llm,
        writing_style_guide_content="Use short paragraphs.",
        brand_spec_content="Brand voice: practical and direct.",
    )
    items = [
        FeedbackItem(
            category="style",
            severity="must_fix",
            location="intro",
            issue="Opening is weak.",
            suggestion="Add a concrete hook.",
        ),
        FeedbackItem(
            category="structure",
            severity="should_fix",
            issue="Section two drags.",
            suggestion="Tighten examples.",
        ),
    ]
    inp = ReviseWriterInput(
        draft="# Original\n\nOld body.\n",
        feedback_items=items,
        content_plan=_minimal_plan(),
    )
    out = agent.revise(inp)

    # At least 2 calls: one for the revision plan, one to apply it
    assert len(llm.captured_prompts) >= 2

    # First call (plan) includes all feedback items
    plan_prompt = llm.captured_prompts[0]
    assert "Opening is weak." in plan_prompt
    assert "Section two drags." in plan_prompt

    # Second call (apply) includes both the revision plan and the feedback
    apply_prompt = llm.captured_prompts[1]
    assert "REVISION PLAN (execute this plan before writing):" in apply_prompt
    assert "COPY EDITOR FEEDBACK (apply every numbered item below):" in apply_prompt
    assert "Section two drags." in apply_prompt

    assert "# Revised title" in out.draft
    assert "Body here." in out.draft


# ---------------------------------------------------------------------------
# system_prompt_content forwarding (issue #7895): revise()'s two model-call
# routes for the batch-execute step (_call_text primary, _fallback_draft_via_json
# JSON fallback) must each carry the cached brand/style segment list through
# unmodified. The plan-generation step (revision.generate_revision_plan) is a
# separate, structural JSON call that never carried brand/style text and is
# out of scope here -- its call_json is stubbed below purely to let revise()
# reach the batch-execute step under test.
# ---------------------------------------------------------------------------

_PLAN_STUB = {"summary": "plan", "changes": [], "risks": []}


def _one_feedback_item():
    return [
        FeedbackItem(
            category="style",
            severity="must_fix",
            location="intro",
            issue="Opening is weak.",
            suggestion="Add a concrete hook.",
        ),
    ]


def test_revise_primary_path_forwards_system_prompt_content(monkeypatch) -> None:
    """revise()'s batch-execute call (the step that actually rewrites prose) passes the
    cached brand/style segment list to _call_text, mirroring run()'s primary path."""
    from agents.blogging.blog_writer_agent.agent import BlogWriterAgent

    agent = make_writer_agent(
        writing_style_guide_content="Use short paragraphs.",
        brand_spec_content="Brand voice: practical and direct.",
    )
    monkeypatch.setattr(
        BlogWriterAgent,
        "_call_agent_json",
        lambda self, p, system_prompt="", **kw: _PLAN_STUB,
    )
    captured: dict = {}

    def fake_call_text(self, p, system_prompt=""):
        captured["system_prompt"] = system_prompt
        return '{"draft": 0}\n---DRAFT---\n# Revised\nBody.'

    monkeypatch.setattr(BlogWriterAgent, "_call_text", fake_call_text)

    inp = ReviseWriterInput(
        draft="# Original\n\nOld body.\n",
        feedback_items=_one_feedback_item(),
        content_plan=_minimal_plan(),
    )
    out = agent.revise(inp)
    assert "Revised" in out.draft
    assert captured["system_prompt"] is agent._writing_system_prompt_with_content


def test_revise_json_fallback_forwards_system_prompt_content(monkeypatch) -> None:
    """revise()'s JSON fallback (_fallback_draft_via_json, reached once the text path
    exhausts its retries) also passes the cached brand/style segment list -- this is the
    fourth "route to the model" issue #7895 names, and it bypasses _call_agent entirely
    so it needs its own forwarding check."""
    from agents.blogging.blog_writer_agent.agent import BlogWriterAgent

    agent = make_writer_agent(
        writing_style_guide_content="Use short paragraphs.",
        brand_spec_content="Brand voice: practical and direct.",
    )
    monkeypatch.setattr(
        BlogWriterAgent,
        "_call_agent_json",
        lambda self, p, system_prompt="", **kw: _PLAN_STUB,
    )
    monkeypatch.setattr(
        BlogWriterAgent,
        "_call_text",
        lambda self, p, system_prompt="": "no marker here",
    )
    captured: dict = {}

    def fake_fallback(self, p, system_prompt=""):
        captured["system_prompt"] = system_prompt
        return "# Fallback draft"

    monkeypatch.setattr(BlogWriterAgent, "_fallback_draft_via_json", fake_fallback)

    inp = ReviseWriterInput(
        draft="# Original\n\nOld body.\n",
        feedback_items=_one_feedback_item(),
        content_plan=_minimal_plan(),
    )
    out = agent.revise(inp)
    assert out.draft == "# Fallback draft"
    assert captured["system_prompt"] is agent._writing_system_prompt_with_content
