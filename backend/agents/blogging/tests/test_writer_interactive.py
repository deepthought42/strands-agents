"""Tests for blog_writer_agent interactive-review methods:

* ``identify_uncertainty_questions``
* ``analyze_user_feedback_for_guideline_updates``
* ``revise_from_user_feedback``
* ``generate_escalation_summary``
* ``revise()`` end-to-end (one batch attempt)
"""

from __future__ import annotations

import json


def _make_agent():
    from .conftest import make_writer_agent

    return make_writer_agent()


# ---------------------------------------------------------------------------
# identify_uncertainty_questions
# ---------------------------------------------------------------------------


def test_identify_uncertainty_questions_returns_items(monkeypatch) -> None:
    """Parses a JSON array of uncertainty questions into model items."""
    from agents.blogging.blog_writer_agent.agent import BlogWriterAgent

    a = _make_agent()
    monkeypatch.setattr(
        BlogWriterAgent,
        "_call_text",
        lambda self, prompt, system_prompt="": json.dumps(
            [
                {
                    "question_id": "q1",
                    "question": "What audience?",
                    "context": "ctx",
                    "section": "Intro",
                }
            ]
        ),
    )
    out = a.identify_uncertainty_questions("draft", "plan")
    assert len(out) == 1
    assert out[0].question_id == "q1"


def test_identify_uncertainty_questions_empty_array(monkeypatch) -> None:
    """Empty JSON array → empty list."""
    from agents.blogging.blog_writer_agent.agent import BlogWriterAgent

    a = _make_agent()
    monkeypatch.setattr(BlogWriterAgent, "_call_text", lambda self, p, system_prompt="": "[]")
    assert a.identify_uncertainty_questions("d", "p") == []


def test_identify_uncertainty_questions_no_array(monkeypatch) -> None:
    """No JSON array in response → empty list."""
    from agents.blogging.blog_writer_agent.agent import BlogWriterAgent

    a = _make_agent()
    monkeypatch.setattr(
        BlogWriterAgent, "_call_text", lambda self, p, system_prompt="": "no array here"
    )
    assert a.identify_uncertainty_questions("d", "p") == []


def test_identify_uncertainty_questions_markdown_link_before_array(monkeypatch) -> None:
    """A Markdown link before the questions array must not block extraction.

    Regression test: naive first-``[``/last-``]`` slicing would grab the
    Markdown link's brackets and fail to parse; the robust
    ``extract_json_array_from_text`` scan skips non-array ``[`` occurrences.
    """
    from agents.blogging.blog_writer_agent.agent import BlogWriterAgent

    a = _make_agent()
    response = "See [docs](https://example.com/guide) for context.\n\n" + json.dumps(
        [
            {
                "question_id": "q1",
                "question": "What audience?",
                "context": "ctx",
                "section": "Intro",
            }
        ]
    )
    monkeypatch.setattr(BlogWriterAgent, "_call_text", lambda self, p, system_prompt="": response)
    out = a.identify_uncertainty_questions("draft", "plan")
    assert len(out) == 1
    assert out[0].question_id == "q1"


def test_identify_uncertainty_questions_unrelated_dict_array_before_questions(
    monkeypatch,
) -> None:
    """An unrelated dict array (no `question` key) must not be mistaken for questions.

    Regression test: a non-empty list of dicts that doesn't match the
    uncertainty-question schema is syntactically valid JSON, but the scanner
    must keep looking for the real questions array instead of short-circuiting.
    """
    from agents.blogging.blog_writer_agent.agent import BlogWriterAgent

    a = _make_agent()
    response = 'Example metadata: {"references": [{"title": "source"}]}\n\n' + json.dumps(
        [
            {
                "question_id": "q1",
                "question": "What audience?",
                "context": "ctx",
                "section": "Intro",
            }
        ]
    )
    monkeypatch.setattr(BlogWriterAgent, "_call_text", lambda self, p, system_prompt="": response)
    out = a.identify_uncertainty_questions("draft", "plan")
    assert len(out) == 1
    assert out[0].question_id == "q1"


def test_identify_uncertainty_questions_malformed_items_skipped(monkeypatch) -> None:
    """Items missing `question` are skipped; missing `question_id` gets an auto id."""
    from agents.blogging.blog_writer_agent.agent import BlogWriterAgent

    a = _make_agent()
    monkeypatch.setattr(
        BlogWriterAgent,
        "_call_text",
        lambda self, p, system_prompt="": json.dumps(
            [
                {"question": "What?"},  # missing question_id → assigned auto
                {"no_question_key": "x"},  # missing required 'question' → skipped
            ]
        ),
    )
    out = a.identify_uncertainty_questions("d", "p")
    assert len(out) == 1
    assert out[0].question_id == "q-0"


def test_identify_uncertainty_questions_llm_error(monkeypatch) -> None:
    """Non-transient LLM failure → empty list."""
    from agents.blogging.blog_writer_agent.agent import BlogWriterAgent

    from llm_service import LLMJsonParseError

    a = _make_agent()

    def boom(self, prompt, system_prompt=""):
        raise LLMJsonParseError("nope", response_preview="x")

    monkeypatch.setattr(BlogWriterAgent, "_call_text", boom)
    assert a.identify_uncertainty_questions("d", "p") == []


def test_identify_uncertainty_questions_programming_error_propagates(monkeypatch) -> None:
    """Unexpected programming errors must not be soft-failed to []."""
    import pytest
    from agents.blogging.blog_writer_agent.agent import BlogWriterAgent

    a = _make_agent()

    def boom(self, prompt, system_prompt=""):
        raise RuntimeError("programmer bug")

    monkeypatch.setattr(BlogWriterAgent, "_call_text", boom)
    with pytest.raises(RuntimeError, match="programmer bug"):
        a.identify_uncertainty_questions("d", "p")


def test_identify_uncertainty_questions_rate_limit_reraises(monkeypatch) -> None:
    import pytest
    from agents.blogging.blog_writer_agent.agent import BlogWriterAgent

    from llm_service import LLMRateLimitError

    a = _make_agent()

    def boom(self, prompt, system_prompt=""):
        raise LLMRateLimitError("rate limited")

    monkeypatch.setattr(BlogWriterAgent, "_call_text", boom)
    with pytest.raises(LLMRateLimitError, match="rate limited"):
        a.identify_uncertainty_questions("d", "p")


def test_identify_uncertainty_questions_wrapped_temporary_reraises(monkeypatch) -> None:
    import pytest
    from agents.blogging.blog_writer_agent.agent import BlogWriterAgent
    from strands.types.exceptions import EventLoopException

    from llm_service import LLMTemporaryError

    a = _make_agent()
    wrapped = LLMTemporaryError("temporary")

    def boom(self, prompt, system_prompt=""):
        raise EventLoopException(wrapped)

    monkeypatch.setattr(BlogWriterAgent, "_call_text", boom)
    with pytest.raises(LLMTemporaryError) as excinfo:
        a.identify_uncertainty_questions("d", "p")
    assert excinfo.value is wrapped


# ---------------------------------------------------------------------------
# analyze_user_feedback_for_guideline_updates
# ---------------------------------------------------------------------------


def test_analyze_feedback_returns_updates(monkeypatch) -> None:
    """Parses guideline updates from structured feedback analysis."""
    from agents.blogging.blog_writer_agent.agent import BlogWriterAgent

    a = _make_agent()
    monkeypatch.setattr(
        BlogWriterAgent,
        "_call_agent_json",
        lambda self, p, **kw: {
            "has_guideline_updates": True,
            "updates": [{"category": "tone", "description": "softer", "guideline_text": "be soft"}],
        },
    )
    out = a.analyze_user_feedback_for_guideline_updates("user fb", "current")
    assert len(out) == 1
    assert out[0].category == "tone"


def test_analyze_feedback_no_updates(monkeypatch) -> None:
    """`has_guideline_updates=False` → empty list."""
    from agents.blogging.blog_writer_agent.agent import BlogWriterAgent

    a = _make_agent()
    monkeypatch.setattr(
        BlogWriterAgent,
        "_call_agent_json",
        lambda self, p, **kw: {"has_guideline_updates": False, "updates": []},
    )
    assert a.analyze_user_feedback_for_guideline_updates("fb", "g") == []


def test_analyze_feedback_non_dict(monkeypatch) -> None:
    """Non-dict structured response → empty list."""
    from agents.blogging.blog_writer_agent.agent import BlogWriterAgent

    a = _make_agent()
    monkeypatch.setattr(BlogWriterAgent, "_call_agent_json", lambda self, p, **kw: "garbage")
    assert a.analyze_user_feedback_for_guideline_updates("fb", "g") == []


def test_analyze_feedback_malformed_skipped(monkeypatch) -> None:
    """Malformed update entries are skipped; valid ones are kept."""
    from agents.blogging.blog_writer_agent.agent import BlogWriterAgent

    a = _make_agent()
    monkeypatch.setattr(
        BlogWriterAgent,
        "_call_agent_json",
        lambda self, p, **kw: {
            "has_guideline_updates": True,
            "updates": [
                {"category": "tone"},  # missing keys → skipped
                {"category": "x", "description": "y", "guideline_text": "z"},
            ],
        },
    )
    out = a.analyze_user_feedback_for_guideline_updates("fb", "g")
    assert len(out) == 1


def test_analyze_feedback_updates_not_list(monkeypatch) -> None:
    """A non-list ``updates`` field soft-fails to an empty list."""
    from agents.blogging.blog_writer_agent.agent import BlogWriterAgent

    a = _make_agent()
    monkeypatch.setattr(
        BlogWriterAgent,
        "_call_agent_json",
        lambda self, p, **kw: {"has_guideline_updates": True, "updates": "not-a-list"},
    )
    assert a.analyze_user_feedback_for_guideline_updates("fb", "g") == []


def test_analyze_feedback_json_parse_error(monkeypatch) -> None:
    """Expected LLMJsonParseError → soft-failed to empty list."""
    from agents.blogging.blog_writer_agent.agent import BlogWriterAgent

    from llm_service import LLMJsonParseError

    a = _make_agent()

    def boom(self, p, **kw):
        raise LLMJsonParseError("bad json", response_preview="x")

    monkeypatch.setattr(BlogWriterAgent, "_call_agent_json", boom)
    assert a.analyze_user_feedback_for_guideline_updates("fb", "g") == []


def test_analyze_feedback_permanent_error_soft_fails(monkeypatch) -> None:
    """A non-transient LLMPermanentError is soft-failed, not propagated.

    This is an optional analysis step in the draft pipeline; a permanent LLM
    failure here should not abort the whole draft stage.
    """
    from agents.blogging.blog_writer_agent.agent import BlogWriterAgent

    from llm_service import LLMPermanentError

    a = _make_agent()

    def boom(self, p, **kw):
        raise LLMPermanentError("permanent")

    monkeypatch.setattr(BlogWriterAgent, "_call_agent_json", boom)
    assert a.analyze_user_feedback_for_guideline_updates("fb", "g") == []


def test_analyze_feedback_unexpected_error_propagates(monkeypatch) -> None:
    """Unexpected/programming-bug exceptions must propagate, not be swallowed."""
    import pytest
    from agents.blogging.blog_writer_agent.agent import BlogWriterAgent

    a = _make_agent()

    def boom(self, p, **kw):
        raise RuntimeError("LLM")

    monkeypatch.setattr(BlogWriterAgent, "_call_agent_json", boom)
    with pytest.raises(RuntimeError, match="LLM"):
        a.analyze_user_feedback_for_guideline_updates("fb", "g")


def test_analyze_feedback_rate_limit_reraises(monkeypatch) -> None:
    import pytest
    from agents.blogging.blog_writer_agent.agent import BlogWriterAgent

    from llm_service import LLMRateLimitError

    a = _make_agent()

    def boom(self, p, **kw):
        raise LLMRateLimitError("rate limited")

    monkeypatch.setattr(BlogWriterAgent, "_call_agent_json", boom)
    with pytest.raises(LLMRateLimitError, match="rate limited"):
        a.analyze_user_feedback_for_guideline_updates("fb", "g")


# ---------------------------------------------------------------------------
# revise_from_user_feedback
# ---------------------------------------------------------------------------


def test_revise_from_user_feedback_happy(monkeypatch, tmp_path) -> None:
    """Extracts revised draft after `---DRAFT---` and writes the output path."""
    from agents.blogging.blog_writer_agent.agent import BlogWriterAgent

    a = _make_agent()
    monkeypatch.setattr(
        BlogWriterAgent,
        "_call_text",
        lambda self, p, system_prompt="": (
            '{"draft": 0}\n---DRAFT---\n# Revised by user feedback\nBody.'
        ),
    )
    out = a.revise_from_user_feedback(
        draft="# Old\nBody",
        user_feedback="be more specific",
        content_plan_text="# Plan",
        audience="devs",
        tone_or_purpose="inform",
        selected_title="Selected",
        elicited_stories="A story",
        target_word_count=800,
        length_guidance="aim for 800",
        uncertainty_answers={"q1": "answer"},
        draft_output_path=tmp_path / "out.md",
    )
    assert "Revised by user feedback" in out.draft
    written = (tmp_path / "out.md").read_text()
    assert "Revised by user feedback" in written
    assert "Body." in written


def test_revise_from_user_feedback_literal_braces_in_feedback(monkeypatch) -> None:
    """Feedback containing literal braces (e.g. a JSON snippet) must not raise."""
    from agents.blogging.blog_writer_agent.agent import BlogWriterAgent

    a = _make_agent()
    captured_prompts = []

    def fake_call_text(self, prompt, system_prompt=""):
        captured_prompts.append(prompt)
        return '{"draft": 0}\n---DRAFT---\n# Revised\nBody.'

    monkeypatch.setattr(BlogWriterAgent, "_call_text", fake_call_text)
    feedback = 'Please fix this snippet: {"key": "value", "nested": {"a": 1}}'
    out = a.revise_from_user_feedback(
        draft="# Old\nBody",
        user_feedback=feedback,
        content_plan_text="# Plan",
    )
    assert "Revised" in out.draft
    assert feedback in captured_prompts[0]


def test_revise_from_user_feedback_empty_draft(monkeypatch) -> None:
    """Whitespace-only draft is returned unchanged (no LLM call)."""
    from agents.blogging.blog_writer_agent.agent import BlogWriterAgent

    a = _make_agent()
    calls: list = []
    monkeypatch.setattr(
        BlogWriterAgent,
        "_call_text",
        lambda *args, **kwargs: calls.append((args, kwargs)) or "should not be called",
    )
    out = a.revise_from_user_feedback(draft="   ", user_feedback="x", content_plan_text="cp")
    assert out.draft == "   "
    assert calls == []


def test_revise_from_user_feedback_no_marker_then_json_fallback(monkeypatch) -> None:
    """LLM returns no ---DRAFT--- marker but JSON fallback works."""
    from agents.blogging.blog_writer_agent.agent import BlogWriterAgent

    a = _make_agent()

    monkeypatch.setattr(
        BlogWriterAgent,
        "_call_text",
        lambda self, prompt, system_prompt="": "no marker here",
    )
    monkeypatch.setattr(
        BlogWriterAgent,
        "_fallback_draft_via_json",
        lambda self, p, system_prompt="": "# Fallback",
    )
    out = a.revise_from_user_feedback(draft="# Original", user_feedback="x", content_plan_text="cp")
    assert "# Fallback" in out.draft


def test_revise_from_user_feedback_programming_error_propagates(monkeypatch) -> None:
    """Non-transient text-path errors must propagate from user-feedback revise."""
    import pytest
    from agents.blogging.blog_writer_agent.agent import BlogWriterAgent

    a = _make_agent()
    import agents.blogging.blog_writer_agent.agent as wa_mod

    monkeypatch.setattr(wa_mod.time, "sleep", lambda *_: None)

    def boom(self, p, system_prompt=""):
        raise RuntimeError("programmer bug")

    monkeypatch.setattr(BlogWriterAgent, "_call_text", boom)
    with pytest.raises(RuntimeError, match="programmer bug"):
        a.revise_from_user_feedback(
            draft="# Original", user_feedback="tighten", content_plan_text="cp"
        )


def test_revise_from_user_feedback_transient_retries_then_fallback(monkeypatch) -> None:
    """LLMTemporaryError on the text path is retried; JSON fallback may recover."""
    from agents.blogging.blog_writer_agent.agent import BlogWriterAgent

    from llm_service import LLMTemporaryError

    a = _make_agent()
    import agents.blogging.blog_writer_agent.agent as wa_mod

    monkeypatch.setattr(wa_mod.time, "sleep", lambda *_: None)

    def boom(self, p, system_prompt=""):
        raise LLMTemporaryError("503")

    monkeypatch.setattr(BlogWriterAgent, "_call_text", boom)
    monkeypatch.setattr(
        BlogWriterAgent,
        "_fallback_draft_via_json",
        lambda self, p, system_prompt="": "# User Feedback Recovered",
    )
    out = a.revise_from_user_feedback(
        draft="# Original", user_feedback="tighten", content_plan_text="cp"
    )
    assert "User Feedback Recovered" in out.draft


def test_revise_from_user_feedback_json_fallback_wrapped_rate_limit_reraises(monkeypatch) -> None:
    """A wrapped transient error from the JSON fallback must propagate, not be swallowed.

    Regression test: the fallback block previously checked ``(LLMRateLimitError,
    LLMTemporaryError)`` without unwrapping ``EventLoopException`` first, so a wrapped
    transient error fell through to the broad ``except Exception`` and was silently
    swallowed, returning the original (unrevised) draft instead of propagating for
    Temporal to retry.
    """
    import pytest
    from agents.blogging.blog_writer_agent.agent import BlogWriterAgent
    from strands.types.exceptions import EventLoopException

    from llm_service import LLMRateLimitError, LLMTemporaryError

    a = _make_agent()
    import agents.blogging.blog_writer_agent.agent as wa_mod

    monkeypatch.setattr(wa_mod.time, "sleep", lambda *_: None)

    def boom(self, p, system_prompt=""):
        raise LLMTemporaryError("503")

    wrapped = LLMRateLimitError("429")

    def fallback_boom(self, p, system_prompt=""):
        raise EventLoopException(wrapped)

    monkeypatch.setattr(BlogWriterAgent, "_call_text", boom)
    monkeypatch.setattr(BlogWriterAgent, "_fallback_draft_via_json", fallback_boom)

    with pytest.raises(LLMRateLimitError) as excinfo:
        a.revise_from_user_feedback(
            draft="# Original", user_feedback="tighten", content_plan_text="cp"
        )
    assert excinfo.value is wrapped


def test_revise_from_user_feedback_json_parse_error_skips_sleep(monkeypatch) -> None:
    """LLMJsonParseError must use the no-sleep handler, not the transient backoff."""
    import agents.blogging.blog_writer_agent.agent as wa_mod
    from agents.blogging.blog_writer_agent.agent import BlogWriterAgent

    from llm_service import LLMJsonParseError

    a = _make_agent()
    sleep_calls: list[float] = []

    def boom(self, prompt, system_prompt=""):
        raise LLMJsonParseError("bad json", response_preview="x")

    def fail_json(self, p, **kw):
        raise LLMJsonParseError("bad json", response_preview="x")

    monkeypatch.setattr(BlogWriterAgent, "_call_text", boom)
    monkeypatch.setattr(BlogWriterAgent, "_call_agent_json", fail_json)
    monkeypatch.setattr(wa_mod.time, "sleep", lambda secs: sleep_calls.append(secs))

    out = a.revise_from_user_feedback(
        draft="# Original",
        user_feedback="tighten the intro",
        content_plan_text="cp",
    )
    assert out.draft == "# Original"
    assert sleep_calls == []


def test_revise_from_user_feedback_wrapped_json_parse_error_skips_sleep(monkeypatch) -> None:
    """EventLoopException-wrapped LLMJsonParseError must also use the no-sleep handler.

    Regression test: the ``except Exception`` branch used to unwrap the cause
    only to check for LLMRateLimitError/LLMTemporaryError, re-raising a wrapped
    LLMJsonParseError instead of retrying it without backoff like an unwrapped one.
    """
    import agents.blogging.blog_writer_agent.agent as wa_mod
    from agents.blogging.blog_writer_agent.agent import BlogWriterAgent
    from strands.types.exceptions import EventLoopException

    from llm_service import LLMJsonParseError

    a = _make_agent()
    sleep_calls: list[float] = []

    def boom(self, prompt, system_prompt=""):
        raise EventLoopException(LLMJsonParseError("bad json", response_preview="x"))

    def fail_json(self, p, **kw):
        raise LLMJsonParseError("bad json", response_preview="x")

    monkeypatch.setattr(BlogWriterAgent, "_call_text", boom)
    monkeypatch.setattr(BlogWriterAgent, "_call_agent_json", fail_json)
    monkeypatch.setattr(wa_mod.time, "sleep", lambda secs: sleep_calls.append(secs))

    out = a.revise_from_user_feedback(
        draft="# Original",
        user_feedback="tighten the intro",
        content_plan_text="cp",
    )
    assert out.draft == "# Original"
    assert sleep_calls == []


# ---------------------------------------------------------------------------
# generate_escalation_summary
# ---------------------------------------------------------------------------


def test_generate_escalation_summary_happy(monkeypatch) -> None:
    """Returns the LLM escalation summary text."""
    from agents.blogging.blog_copy_editor_agent.models import FeedbackItem
    from agents.blogging.blog_writer_agent.agent import BlogWriterAgent

    a = _make_agent()
    monkeypatch.setattr(
        BlogWriterAgent,
        "_call_text",
        lambda self, p, system_prompt="": "Summary: stuck on tone and flow.",
    )
    items = [
        FeedbackItem(category="tone", severity="major", issue="too formal"),
        FeedbackItem(category="flow", severity="major", issue="abrupt"),
    ]
    out = a.generate_escalation_summary(
        revision_count=5,
        latest_feedback_items=items,
        persistent_issues=[],
    )
    assert "Summary" in out


def test_generate_escalation_summary_handles_error(monkeypatch) -> None:
    """Non-transient LLM failure still returns a fallback string."""
    from agents.blogging.blog_writer_agent.agent import BlogWriterAgent

    from llm_service import LLMJsonParseError

    a = _make_agent()

    def boom(self, p, system_prompt=""):
        raise LLMJsonParseError("nope")

    monkeypatch.setattr(BlogWriterAgent, "_call_text", boom)
    out = a.generate_escalation_summary(
        revision_count=10,
        latest_feedback_items=[],
        persistent_issues=[],
    )
    # Returns a fallback string (non-empty) or empty
    assert isinstance(out, str)


def test_generate_escalation_summary_reraises_non_llm_error(monkeypatch) -> None:
    """A programming error (not an LLM failure) must propagate, not be swallowed."""
    import pytest
    from agents.blogging.blog_writer_agent.agent import BlogWriterAgent

    a = _make_agent()

    def boom(self, p, system_prompt=""):
        raise TypeError("bad internal state")

    monkeypatch.setattr(BlogWriterAgent, "_call_text", boom)
    with pytest.raises(TypeError, match="bad internal state"):
        a.generate_escalation_summary(
            revision_count=10,
            latest_feedback_items=[],
            persistent_issues=[],
        )


def test_generate_escalation_summary_rate_limit_reraises(monkeypatch) -> None:
    """LLM rate-limit errors during escalation summary generation must propagate."""
    import pytest
    from agents.blogging.blog_writer_agent.agent import BlogWriterAgent

    from llm_service import LLMRateLimitError

    a = _make_agent()

    def boom(self, p, system_prompt=""):
        raise LLMRateLimitError("rate limited")

    monkeypatch.setattr(BlogWriterAgent, "_call_text", boom)
    with pytest.raises(LLMRateLimitError, match="rate limited"):
        a.generate_escalation_summary(
            revision_count=10,
            latest_feedback_items=[],
            persistent_issues=[],
        )


# ---------------------------------------------------------------------------
# revise() full path
# ---------------------------------------------------------------------------


def test_revise_with_feedback_batches(monkeypatch, tmp_path) -> None:
    """revise() with a non-empty feedback list runs through batch revision."""
    from agents.blogging.blog_copy_editor_agent.models import FeedbackItem
    from agents.blogging.blog_writer_agent import revision
    from agents.blogging.blog_writer_agent.agent import BlogWriterAgent
    from agents.blogging.blog_writer_agent.models import ReviseWriterInput, RevisionPlan
    from agents.blogging.shared.content_plan import ContentPlanSection, TitleCandidate

    from ._content_plan_test_utils import make_content_plan

    a = _make_agent()

    # Stub generate_revision_plan + _call_agent to keep things fast
    monkeypatch.setattr(
        revision,
        "generate_revision_plan",
        lambda draft, items, ri, *, call_json, call_text, llm=None: RevisionPlan(
            summary="planned", changes=[], risks=[]
        ),
    )
    monkeypatch.setattr(
        BlogWriterAgent,
        "_call_text",
        lambda self, p, system_prompt="": '{"draft": 0}\n---DRAFT---\n# Revised\nBody.',
    )

    plan = make_content_plan(
        overarching_topic="x",
        narrative_flow="f",
        sections=[ContentPlanSection(title="A", coverage_description="a", order=0)],
        title_candidates=[TitleCandidate(title="T", probability_of_success=0.5)],
    )

    out = a.revise(
        ReviseWriterInput(
            draft="# Original\n\nBody.",
            feedback_items=[FeedbackItem(category="grammar", severity="minor", issue="comma")],
            feedback_summary="fix",
            content_plan=plan,
        ),
        draft_output_path=tmp_path / "rev.md",
        work_dir=tmp_path,
        iteration=1,
    )
    assert "Revised" in out.draft


def test_revise_skips_json_fallback_when_primary_returns_identical_draft(monkeypatch) -> None:
    """Primary success that yields the same text must not waste a JSON fallback call."""
    from agents.blogging.blog_copy_editor_agent.models import FeedbackItem
    from agents.blogging.blog_writer_agent import revision
    from agents.blogging.blog_writer_agent.agent import BlogWriterAgent
    from agents.blogging.blog_writer_agent.models import ReviseWriterInput, RevisionPlan
    from agents.blogging.shared.content_plan import ContentPlanSection, TitleCandidate

    from ._content_plan_test_utils import make_content_plan

    a = _make_agent()
    original = "# Original\nBody"
    fallback_calls = {"n": 0}

    monkeypatch.setattr(
        revision,
        "generate_revision_plan",
        lambda draft, items, ri, *, call_json, call_text, llm=None: RevisionPlan(
            summary="planned", changes=[], risks=[]
        ),
    )
    monkeypatch.setattr(
        BlogWriterAgent,
        "_call_text",
        lambda self, p, system_prompt="": f'{{"draft": 0}}\n---DRAFT---\n{original}',
    )

    def tracking_fallback(self, prompt, system_prompt=""):
        fallback_calls["n"] += 1
        return "# Should not be used"

    monkeypatch.setattr(BlogWriterAgent, "_fallback_draft_via_json", tracking_fallback)

    plan = make_content_plan(
        overarching_topic="x",
        narrative_flow="f",
        sections=[ContentPlanSection(title="A", coverage_description="a", order=0)],
        title_candidates=[TitleCandidate(title="T", probability_of_success=0.5)],
    )
    out = a.revise(
        ReviseWriterInput(
            draft=original,
            feedback_items=[FeedbackItem(category="x", severity="minor", issue="y")],
            feedback_summary="s",
            content_plan=plan,
        ),
    )
    assert out.draft == original
    assert fallback_calls["n"] == 0


def test_revise_programming_error_propagates(monkeypatch) -> None:
    """Non-transient text-path errors must propagate from batch revise."""
    import pytest
    from agents.blogging.blog_copy_editor_agent.models import FeedbackItem
    from agents.blogging.blog_writer_agent.agent import BlogWriterAgent
    from agents.blogging.blog_writer_agent.models import ReviseWriterInput, RevisionPlan
    from agents.blogging.shared.content_plan import ContentPlanSection, TitleCandidate

    from ._content_plan_test_utils import make_content_plan

    a = _make_agent()
    import agents.blogging.blog_writer_agent.agent as wa_mod

    monkeypatch.setattr(wa_mod.time, "sleep", lambda *_: None)
    monkeypatch.setattr(
        wa_mod.revision,
        "generate_revision_plan",
        lambda draft, items, ri, *, call_json, call_text, llm=None: RevisionPlan(
            summary="planned", changes=[], risks=[]
        ),
    )

    def boom(self, *a, **kw):
        raise RuntimeError("programmer bug")

    monkeypatch.setattr(BlogWriterAgent, "_call_text", boom)

    plan = make_content_plan(
        overarching_topic="x",
        narrative_flow="f",
        sections=[ContentPlanSection(title="A", coverage_description="a", order=0)],
        title_candidates=[TitleCandidate(title="T", probability_of_success=0.5)],
    )
    with pytest.raises(RuntimeError, match="programmer bug"):
        a.revise(
            ReviseWriterInput(
                draft="# Original\nBody",
                feedback_items=[FeedbackItem(category="x", severity="minor", issue="y")],
                feedback_summary="s",
                content_plan=plan,
            ),
        )


def test_revise_falls_back_to_original_when_llm_fails(monkeypatch) -> None:
    """If text yields no draft and json fallback fails, return original draft."""
    from agents.blogging.blog_copy_editor_agent.models import FeedbackItem
    from agents.blogging.blog_writer_agent.agent import BlogWriterAgent
    from agents.blogging.blog_writer_agent.models import ReviseWriterInput, RevisionPlan
    from agents.blogging.shared.content_plan import ContentPlanSection, TitleCandidate

    from ._content_plan_test_utils import make_content_plan

    a = _make_agent()

    # Patch time.sleep to skip waits
    import agents.blogging.blog_writer_agent.agent as wa_mod

    monkeypatch.setattr(wa_mod.time, "sleep", lambda *_: None)
    monkeypatch.setattr(
        wa_mod.revision,
        "generate_revision_plan",
        lambda draft, items, ri, *, call_json, call_text, llm=None: RevisionPlan(
            summary="planned", changes=[], risks=[]
        ),
    )
    monkeypatch.setattr(BlogWriterAgent, "_call_text", lambda self, *a, **kw: "no marker")
    monkeypatch.setattr(
        BlogWriterAgent, "_fallback_draft_via_json", lambda self, p, system_prompt="": None
    )

    plan = make_content_plan(
        overarching_topic="x",
        narrative_flow="f",
        sections=[ContentPlanSection(title="A", coverage_description="a", order=0)],
        title_candidates=[TitleCandidate(title="T", probability_of_success=0.5)],
    )
    out = a.revise(
        ReviseWriterInput(
            draft="# Original\nBody",
            feedback_items=[FeedbackItem(category="x", severity="minor", issue="y")],
            feedback_summary="s",
            content_plan=plan,
        ),
    )
    assert "Original" in out.draft


def test_revise_batch_uses_json_fallback_when_text_fails(monkeypatch) -> None:
    """Batch revise uses _fallback_draft_via_json when text path yields no draft."""
    from agents.blogging.blog_copy_editor_agent.models import FeedbackItem
    from agents.blogging.blog_writer_agent.agent import BlogWriterAgent
    from agents.blogging.blog_writer_agent.models import ReviseWriterInput, RevisionPlan
    from agents.blogging.shared.content_plan import ContentPlanSection, TitleCandidate

    from ._content_plan_test_utils import make_content_plan

    a = _make_agent()
    import agents.blogging.blog_writer_agent.agent as wa_mod

    monkeypatch.setattr(wa_mod.time, "sleep", lambda *_: None)
    monkeypatch.setattr(
        wa_mod.revision,
        "generate_revision_plan",
        lambda draft, items, ri, *, call_json, call_text, llm=None: RevisionPlan(
            summary="planned", changes=[], risks=[]
        ),
    )

    monkeypatch.setattr(BlogWriterAgent, "_call_text", lambda self, *a, **kw: "no marker")
    monkeypatch.setattr(
        BlogWriterAgent,
        "_fallback_draft_via_json",
        lambda self, p, system_prompt="": "# Batch Recovered",
    )
    plan = make_content_plan(
        overarching_topic="x",
        narrative_flow="f",
        sections=[ContentPlanSection(title="A", coverage_description="a", order=0)],
        title_candidates=[TitleCandidate(title="T", probability_of_success=0.5)],
    )
    out = a.revise(
        ReviseWriterInput(
            draft="# Original\nBody",
            feedback_items=[FeedbackItem(category="x", severity="minor", issue="y")],
            feedback_summary="s",
            content_plan=plan,
        ),
    )
    assert "Batch Recovered" in out.draft


def test_revise_wrapped_temporary_retries_then_fallback(monkeypatch) -> None:
    """EventLoopException(LLMTemporaryError) is treated as transient and retried."""
    from agents.blogging.blog_copy_editor_agent.models import FeedbackItem
    from agents.blogging.blog_writer_agent.agent import BlogWriterAgent
    from agents.blogging.blog_writer_agent.models import ReviseWriterInput, RevisionPlan
    from agents.blogging.shared.content_plan import ContentPlanSection, TitleCandidate
    from strands.types.exceptions import EventLoopException

    from llm_service import LLMTemporaryError

    from ._content_plan_test_utils import make_content_plan

    a = _make_agent()
    import agents.blogging.blog_writer_agent.agent as wa_mod

    monkeypatch.setattr(wa_mod.time, "sleep", lambda *_: None)
    monkeypatch.setattr(
        wa_mod.revision,
        "generate_revision_plan",
        lambda draft, items, ri, *, call_json, call_text, llm=None: RevisionPlan(
            summary="planned", changes=[], risks=[]
        ),
    )
    wrapped = LLMTemporaryError("temporary")
    call_count = 0

    def boom(self, *a, **kw):
        nonlocal call_count
        call_count += 1
        raise EventLoopException(wrapped)

    monkeypatch.setattr(BlogWriterAgent, "_call_text", boom)
    monkeypatch.setattr(
        BlogWriterAgent,
        "_fallback_draft_via_json",
        lambda self, p, system_prompt="": "# Batch Recovered Wrapped",
    )
    plan = make_content_plan(
        overarching_topic="x",
        narrative_flow="f",
        sections=[ContentPlanSection(title="A", coverage_description="a", order=0)],
        title_candidates=[TitleCandidate(title="T", probability_of_success=0.5)],
    )
    out = a.revise(
        ReviseWriterInput(
            draft="# Original\nBody",
            feedback_items=[FeedbackItem(category="x", severity="minor", issue="y")],
            feedback_summary="s",
            content_plan=plan,
        ),
    )
    assert "Batch Recovered Wrapped" in out.draft
    assert call_count == wa_mod.BATCH_EXECUTE_MAX_RETRIES


def test_revise_wrapped_json_parse_error_retries_then_fallback(monkeypatch) -> None:
    """EventLoopException-wrapped LLMJsonParseError must retry (not re-raise), then fall back.

    Regression test: the batch-execute ``except Exception`` branch used to
    unwrap the cause only to check for LLMRateLimitError/LLMTemporaryError,
    re-raising a wrapped LLMJsonParseError instead of retrying it the same way
    as an unwrapped one.
    """
    from agents.blogging.blog_copy_editor_agent.models import FeedbackItem
    from agents.blogging.blog_writer_agent.agent import BlogWriterAgent
    from agents.blogging.blog_writer_agent.models import ReviseWriterInput, RevisionPlan
    from agents.blogging.shared.content_plan import ContentPlanSection, TitleCandidate
    from strands.types.exceptions import EventLoopException

    from llm_service import LLMJsonParseError

    from ._content_plan_test_utils import make_content_plan

    a = _make_agent()
    import agents.blogging.blog_writer_agent.agent as wa_mod

    sleep_calls: list[float] = []
    monkeypatch.setattr(wa_mod.time, "sleep", lambda secs: sleep_calls.append(secs))
    monkeypatch.setattr(
        wa_mod.revision,
        "generate_revision_plan",
        lambda draft, items, ri, *, call_json, call_text, llm=None: RevisionPlan(
            summary="planned", changes=[], risks=[]
        ),
    )

    call_count = 0

    def boom(self, *a, **kw):
        nonlocal call_count
        call_count += 1
        raise EventLoopException(LLMJsonParseError("bad json", response_preview="x"))

    monkeypatch.setattr(BlogWriterAgent, "_call_text", boom)
    monkeypatch.setattr(
        BlogWriterAgent,
        "_fallback_draft_via_json",
        lambda self, p, system_prompt="": "# Batch Recovered From Parse Error",
    )
    plan = make_content_plan(
        overarching_topic="x",
        narrative_flow="f",
        sections=[ContentPlanSection(title="A", coverage_description="a", order=0)],
        title_candidates=[TitleCandidate(title="T", probability_of_success=0.5)],
    )
    out = a.revise(
        ReviseWriterInput(
            draft="# Original\nBody",
            feedback_items=[FeedbackItem(category="x", severity="minor", issue="y")],
            feedback_summary="s",
            content_plan=plan,
        ),
    )
    assert "Batch Recovered From Parse Error" in out.draft
    assert sleep_calls == []
    assert call_count > 1


# ---------------------------------------------------------------------------
# system_prompt_content forwarding (issue #7895): revise_from_user_feedback()'s
# two model-call routes (_call_text primary, _fallback_draft_via_json JSON
# fallback) must each carry the cached brand/style segment list through
# unmodified.
# ---------------------------------------------------------------------------


def test_revise_from_user_feedback_primary_path_forwards_system_prompt_content(monkeypatch) -> None:
    """revise_from_user_feedback()'s primary text-completion call passes the cached
    brand/style segment list to _call_text, unmodified."""
    from agents.blogging.blog_writer_agent.agent import BlogWriterAgent

    a = _make_agent()
    captured: dict = {}

    def fake_call_text(self, prompt, system_prompt=""):
        captured["system_prompt"] = system_prompt
        return '{"draft": 0}\n---DRAFT---\n# Revised\nBody.'

    monkeypatch.setattr(BlogWriterAgent, "_call_text", fake_call_text)
    out = a.revise_from_user_feedback(
        draft="# Old\nBody", user_feedback="x", content_plan_text="cp"
    )
    assert "Revised" in out.draft
    assert captured["system_prompt"] is a._writing_system_prompt_with_content


def test_revise_from_user_feedback_json_fallback_forwards_system_prompt_content(
    monkeypatch,
) -> None:
    """revise_from_user_feedback()'s JSON fallback (_fallback_draft_via_json) is the
    fourth "route to the model" named in issue #7895 -- it bypasses _call_agent entirely,
    so it needs its own check that it still receives the cached brand/style segment list."""
    from agents.blogging.blog_writer_agent.agent import BlogWriterAgent

    a = _make_agent()
    captured: dict = {}
    monkeypatch.setattr(
        BlogWriterAgent,
        "_call_text",
        lambda self, prompt, system_prompt="": "no marker here",
    )

    def fake_fallback(self, p, system_prompt=""):
        captured["system_prompt"] = system_prompt
        return "# Fallback"

    monkeypatch.setattr(BlogWriterAgent, "_fallback_draft_via_json", fake_fallback)
    out = a.revise_from_user_feedback(draft="# Original", user_feedback="x", content_plan_text="cp")
    assert "# Fallback" in out.draft
    assert captured["system_prompt"] is a._writing_system_prompt_with_content
