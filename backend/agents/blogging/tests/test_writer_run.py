"""Tests for BlogWriterAgent.run() — the main draft generation method."""

from __future__ import annotations

import pytest


def _agent():
    from .conftest import make_writer_agent

    return make_writer_agent()


def _writer_input(**overrides):
    from agents.blogging.blog_writer_agent.models import WriterInput
    from agents.blogging.shared.content_plan import ContentPlanSection, TitleCandidate

    from ._content_plan_test_utils import make_content_plan

    plan = make_content_plan(
        overarching_topic="Topic",
        narrative_flow="flow",
        sections=[ContentPlanSection(title="Intro", coverage_description="hook", order=0)],
        title_candidates=[TitleCandidate(title="T", probability_of_success=0.5)],
    )
    kwargs = {
        "content_plan": plan,
        "audience": "devs",
        "tone_or_purpose": "inform",
    }
    kwargs.update(overrides)
    return WriterInput(**kwargs)


def test_writer_run_happy_with_all_options(monkeypatch, tmp_path) -> None:
    from agents.blogging.blog_writer_agent.agent import BlogWriterAgent

    a = _agent()
    monkeypatch.setattr(
        BlogWriterAgent,
        "_call_text",
        lambda self, p, system_prompt="": '{"draft": 0}\n---DRAFT---\n# A Title\nBody.\n',
    )
    # Disable the self-review path so tests stay deterministic
    monkeypatch.setattr(
        BlogWriterAgent, "_self_review", lambda self, d, allowed_claims_section="": d
    )

    output_path = tmp_path / "draft.md"
    out = a.run(
        _writer_input(
            selected_title="The Selected One",
            elicited_stories="A real story",
            length_guidance="aim for 1000 words",
        ),
        on_llm_request=lambda msg: None,
        draft_output_path=output_path,
    )
    assert "A Title" in out.draft
    assert output_path.exists()


def test_writer_run_empty_outline_returns_placeholder(monkeypatch) -> None:
    from agents.blogging.blog_writer_agent.models import WriterInput
    from agents.blogging.shared.content_plan import ContentPlanSection, TitleCandidate

    from ._content_plan_test_utils import make_content_plan

    a = _agent()
    plan = make_content_plan(
        overarching_topic="Topic",
        narrative_flow="flow",
        sections=[ContentPlanSection(title="A", coverage_description="x", order=0)],
        title_candidates=[TitleCandidate(title="T", probability_of_success=0.5)],
    )
    # Mock outline_for_prompt to return empty string
    monkeypatch.setattr(WriterInput, "outline_for_prompt", lambda self: "")
    out = a.run(WriterInput(content_plan=plan))
    assert "Add a content plan" in out.draft


def test_writer_run_no_marker_returns_placeholder(monkeypatch) -> None:
    """LLM returns text without ---DRAFT--- marker — placeholder returned."""
    from agents.blogging.blog_writer_agent.agent import BlogWriterAgent

    a = _agent()
    monkeypatch.setattr(
        BlogWriterAgent,
        "_call_text",
        lambda self, p, system_prompt="": "no marker text",
    )
    monkeypatch.setattr(BlogWriterAgent, "_call_agent_json", lambda self, p, **kw: {})
    out = a.run(_writer_input())
    assert "No draft was generated" in out.draft


def test_writer_run_placeholder_skips_self_review(monkeypatch) -> None:
    """Empty draft uses ``_PLACEHOLDER_DRAFT`` and does not invoke self-review."""
    from agents.blogging.blog_writer_agent.agent import (
        _PLACEHOLDER_DRAFT,
        BlogWriterAgent,
    )

    a = _agent()
    monkeypatch.setattr(
        BlogWriterAgent,
        "_call_text",
        lambda self, p, system_prompt="": "no marker text",
    )
    monkeypatch.setattr(BlogWriterAgent, "_call_agent_json", lambda self, p, **kw: {})
    calls: list[str] = []
    monkeypatch.setattr(
        BlogWriterAgent,
        "_self_review",
        lambda self, d, allowed_claims_section="": calls.append(d) or d,
    )
    out = a.run(_writer_input())
    assert out.draft == _PLACEHOLDER_DRAFT
    assert calls == []


def test_writer_run_old_short_prefix_still_self_reviews(monkeypatch) -> None:
    """A draft matching only the old short prefix still runs self-review."""
    from agents.blogging.blog_writer_agent.agent import BlogWriterAgent

    a = _agent()
    body = "# Draft\n\nNo draft yet — waiting on research."
    monkeypatch.setattr(
        BlogWriterAgent,
        "_call_text",
        lambda self, p, system_prompt="": f'{{"draft": 0}}\n---DRAFT---\n{body}',
    )
    calls: list[str] = []
    monkeypatch.setattr(
        BlogWriterAgent,
        "_self_review",
        lambda self, d, allowed_claims_section="": calls.append(d) or d,
    )
    out = a.run(_writer_input())
    assert out.draft == body
    assert calls == [body]


def test_writer_run_json_parse_error_then_json_fallback(monkeypatch) -> None:
    """LLMJsonParseError on the text path soft-fails into the JSON draft fallback."""
    from agents.blogging.blog_writer_agent.agent import BlogWriterAgent

    from llm_service import LLMJsonParseError

    a = _agent()
    monkeypatch.setattr(
        BlogWriterAgent,
        "_call_text",
        lambda self, p, system_prompt="": (_ for _ in ()).throw(
            LLMJsonParseError("bad draft text")
        ),
    )
    monkeypatch.setattr(
        BlogWriterAgent,
        "_call_agent_json",
        lambda self, p, **kw: {"draft": "# Fallback\nBody."},
    )
    monkeypatch.setattr(
        BlogWriterAgent, "_self_review", lambda self, d, allowed_claims_section="": d
    )
    out = a.run(_writer_input())
    assert "Fallback" in out.draft


def test_writer_run_wrapped_json_parse_error_then_json_fallback(monkeypatch) -> None:
    """EventLoopException-wrapped LLMJsonParseError still soft-fails to JSON fallback."""
    from agents.blogging.blog_writer_agent.agent import BlogWriterAgent
    from strands.types.exceptions import EventLoopException

    from llm_service import LLMJsonParseError

    a = _agent()
    monkeypatch.setattr(
        BlogWriterAgent,
        "_call_text",
        lambda self, p, system_prompt="": (_ for _ in ()).throw(
            EventLoopException(LLMJsonParseError("bad draft text"))
        ),
    )
    monkeypatch.setattr(
        BlogWriterAgent,
        "_call_agent_json",
        lambda self, p, **kw: {"draft": "# Unwrapped Fallback\nBody."},
    )
    monkeypatch.setattr(
        BlogWriterAgent, "_self_review", lambda self, d, allowed_claims_section="": d
    )
    out = a.run(_writer_input())
    assert "Unwrapped Fallback" in out.draft


def test_writer_run_json_fallback_non_dict_returns_placeholder(monkeypatch) -> None:
    """A non-dict/None return from _call_agent_json in the fallback must not raise
    AttributeError — it should fall through to the placeholder draft."""
    from agents.blogging.blog_writer_agent.agent import BlogWriterAgent

    from llm_service import LLMJsonParseError

    a = _agent()
    monkeypatch.setattr(
        BlogWriterAgent,
        "_call_text",
        lambda self, p, system_prompt="": (_ for _ in ()).throw(
            LLMJsonParseError("bad draft text")
        ),
    )
    monkeypatch.setattr(BlogWriterAgent, "_call_agent_json", lambda self, p, **kw: None)
    out = a.run(_writer_input())
    assert "No draft was generated" in out.draft


def test_writer_run_json_parse_error_and_fallback_also_fails(monkeypatch) -> None:
    """LLMJsonParseError on both text and JSON paths yields the placeholder draft."""
    from agents.blogging.blog_writer_agent.agent import BlogWriterAgent

    from llm_service import LLMJsonParseError

    a = _agent()
    monkeypatch.setattr(
        BlogWriterAgent,
        "_call_text",
        lambda self, p, system_prompt="": (_ for _ in ()).throw(
            LLMJsonParseError("bad draft text")
        ),
    )

    def boom(self, p, **kw):
        raise LLMJsonParseError("bad json fallback")

    monkeypatch.setattr(BlogWriterAgent, "_call_agent_json", boom)
    out = a.run(_writer_input())
    assert "No draft was generated" in out.draft


def test_writer_run_wrapped_json_parse_error_and_fallback_also_fails(monkeypatch) -> None:
    """Wrapped parse errors on both paths still yield the placeholder draft."""
    from agents.blogging.blog_writer_agent.agent import BlogWriterAgent
    from strands.types.exceptions import EventLoopException

    from llm_service import LLMJsonParseError

    a = _agent()
    monkeypatch.setattr(
        BlogWriterAgent,
        "_call_text",
        lambda self, p, system_prompt="": (_ for _ in ()).throw(
            EventLoopException(LLMJsonParseError("bad draft text"))
        ),
    )

    def boom(self, p, **kw):
        raise EventLoopException(LLMJsonParseError("bad json fallback"))

    monkeypatch.setattr(BlogWriterAgent, "_call_agent_json", boom)
    out = a.run(_writer_input())
    assert "No draft was generated" in out.draft


@pytest.mark.parametrize(
    "exc",
    [TypeError("programmer bug"), ValueError("programmer bug")],
    ids=["TypeError", "ValueError"],
)
def test_writer_run_programming_error_propagates(monkeypatch, exc: Exception) -> None:
    """TypeError/ValueError on draft generation must propagate, not soft-fail."""
    from agents.blogging.blog_writer_agent.agent import BlogWriterAgent

    a = _agent()
    monkeypatch.setattr(
        BlogWriterAgent,
        "_call_text",
        lambda self, p, system_prompt="": (_ for _ in ()).throw(exc),
    )
    with pytest.raises(type(exc), match="programmer bug"):
        a.run(_writer_input())


def test_writer_run_wrapped_programming_error_propagates(monkeypatch) -> None:
    """EventLoopException wrapping a programming error must still propagate."""
    from agents.blogging.blog_writer_agent.agent import BlogWriterAgent
    from strands.types.exceptions import EventLoopException

    a = _agent()
    wrapped = EventLoopException(TypeError("programmer bug"))
    monkeypatch.setattr(
        BlogWriterAgent,
        "_call_text",
        lambda self, p, system_prompt="": (_ for _ in ()).throw(wrapped),
    )
    with pytest.raises(EventLoopException) as excinfo:
        a.run(_writer_input())
    assert isinstance(excinfo.value.original_exception, TypeError)
    assert "programmer bug" in str(excinfo.value.original_exception)


def test_writer_run_default_length_guidance(monkeypatch) -> None:
    """When length_guidance is empty, the default 'TARGET LENGTH' block is appended."""
    from agents.blogging.blog_writer_agent.agent import BlogWriterAgent

    a = _agent()
    captured = {"prompt": ""}

    def fake_call(self, prompt, system_prompt=""):
        captured["prompt"] = prompt
        return '{"draft": 0}\n---DRAFT---\n# Out\nBody.'

    monkeypatch.setattr(BlogWriterAgent, "_call_text", fake_call)
    monkeypatch.setattr(
        BlogWriterAgent, "_self_review", lambda self, d, allowed_claims_section="": d
    )
    a.run(_writer_input(length_guidance=""))
    assert "TARGET LENGTH" in captured["prompt"]


def test_fallback_draft_via_json_success(monkeypatch) -> None:
    """_fallback_draft_via_json invokes run_json_gate correctly and returns a stripped draft."""

    a = _agent()
    captured: dict = {}

    def fake_gate(model, system_prompt, prompt, **kwargs):
        captured["max_attempts"] = kwargs.get("max_attempts")
        captured["prompt"] = prompt
        captured["strict"] = kwargs.get("strict_json_suffix", "")
        captured["fresh_agent_per_attempt"] = kwargs.get("fresh_agent_per_attempt")
        assert callable(kwargs.get("fallback_builder"))
        return {"draft": "  # From JSON  \n"}

    monkeypatch.setattr(
        "agents.blogging.blog_writer_agent.agent.run_json_gate",
        fake_gate,
    )
    out = a._fallback_draft_via_json("revise this draft")
    assert out == "# From JSON"
    assert captured["max_attempts"] == 2
    assert captured["fresh_agent_per_attempt"] is True
    assert "Respond with valid JSON only" in captured["prompt"]
    assert "draft" in captured["strict"].lower()


def test_fallback_draft_via_json_rejects_empty_prompt() -> None:
    """Empty/whitespace-only prompt raises ValueError, surviving `-O` optimization."""

    a = _agent()
    with pytest.raises(ValueError, match="prompt must be a non-empty string"):
        a._fallback_draft_via_json("   ")


def test_fallback_draft_via_json_rejects_non_string_prompt() -> None:
    """Non-string prompt raises ValueError, surviving `-O` optimization."""

    a = _agent()
    with pytest.raises(ValueError, match="prompt must be a non-empty string"):
        a._fallback_draft_via_json(None)


def test_fallback_draft_via_json_empty_draft_returns_none(monkeypatch) -> None:
    """Whitespace-only draft values are normalized to None so callers keep the original."""

    a = _agent()
    monkeypatch.setattr(
        "agents.blogging.blog_writer_agent.agent.run_json_gate",
        lambda *a, **k: {"draft": "   "},
    )
    assert a._fallback_draft_via_json("prompt") is None


def test_fallback_draft_via_json_missing_draft_returns_none(monkeypatch) -> None:
    """A JSON response with no 'draft' key yields None."""

    a = _agent()
    monkeypatch.setattr(
        "agents.blogging.blog_writer_agent.agent.run_json_gate",
        lambda *a, **k: {},
    )
    assert a._fallback_draft_via_json("prompt") is None


def test_fallback_draft_via_json_exhausted_hook_returns_none(monkeypatch) -> None:
    """fallback_builder returning {} on JSON-parse exhaustion yields None (keep original draft)."""
    from llm_service import LLMJsonParseError

    a = _agent()

    def fake_gate(model, system_prompt, prompt, **kwargs):
        return kwargs["fallback_builder"](LLMJsonParseError("bad json"))

    monkeypatch.setattr(
        "agents.blogging.blog_writer_agent.agent.run_json_gate",
        fake_gate,
    )
    assert a._fallback_draft_via_json("prompt") is None


def test_fallback_draft_via_json_unexpected_hook_returns_none(monkeypatch) -> None:
    """fallback_builder returning {} on an unexpected error yields None."""

    a = _agent()

    def fake_gate(model, system_prompt, prompt, **kwargs):
        assert kwargs.get("max_attempts") == 2
        return kwargs["fallback_builder"](RuntimeError("boom"))

    monkeypatch.setattr(
        "agents.blogging.blog_writer_agent.agent.run_json_gate",
        fake_gate,
    )
    assert a._fallback_draft_via_json("prompt") is None


def test_fallback_draft_via_json_transient_reraises(monkeypatch) -> None:
    """Transient LLM errors from run_json_gate are re-raised, not converted to None."""
    from llm_service import LLMRateLimitError

    a = _agent()

    def fake_gate(model, system_prompt, prompt, **kwargs):
        assert kwargs.get("max_attempts") == 2
        raise LLMRateLimitError("rate limited")

    monkeypatch.setattr(
        "agents.blogging.blog_writer_agent.agent.run_json_gate",
        fake_gate,
    )

    with pytest.raises(LLMRateLimitError):
        a._fallback_draft_via_json("prompt")


def test_fallback_draft_via_json_unwraps_event_loop_transient(monkeypatch) -> None:
    """Strands EventLoopException wrappers must re-raise the unwrapped transient cause.

    The draft-stage Temporal funnel retries only on LLMRateLimitError /
    LLMTemporaryError; re-raising the wrapper would be swallowed by the fallback
    and silently keep the unrevised draft.
    """
    from agents.blogging.shared import json_retry as json_retry_mod
    from strands.types.exceptions import EventLoopException

    from llm_service import LLMRateLimitError

    a = _agent()
    wrapped = LLMRateLimitError("429 after client retries")

    class _BoomAgent:
        def __init__(self, *args, **kwargs):
            pass

        def __call__(self, prompt):
            raise EventLoopException(wrapped)

    monkeypatch.setattr(json_retry_mod, "Agent", _BoomAgent)
    with pytest.raises(LLMRateLimitError) as excinfo:
        a._fallback_draft_via_json("prompt")
    assert excinfo.value is wrapped
    assert not isinstance(excinfo.value, EventLoopException)


def test_fallback_draft_via_json_agent_construction_error_returns_none(monkeypatch) -> None:
    """Agent construction TypeError is caught by the helper policy and yields None."""
    from agents.blogging.shared import json_retry as json_retry_mod

    a = _agent()

    class _BadAgent:
        def __init__(self, *args, **kwargs):
            raise TypeError("unsupported model config")

        def __call__(self, prompt):
            raise AssertionError("should not be called")

    monkeypatch.setattr(json_retry_mod, "Agent", _BadAgent)
    assert a._fallback_draft_via_json("prompt") is None


# ---------------------------------------------------------------------------
# system_prompt_content forwarding (issue #7895): run()'s two model-call
# routes (_call_text primary, _call_agent_json fallback) and the standalone
# _fallback_draft_via_json primitive must each carry the cached brand/style
# segment list through unmodified. These pin the wiring so it cannot be
# silently deleted -- the existing tests above patch these methods with
# lambdas that accept but never assert on system_prompt, so none of them
# would catch a dropped kwarg.
# ---------------------------------------------------------------------------


def test_writer_run_text_path_forwards_system_prompt_content(monkeypatch) -> None:
    """run()'s primary text-draft call passes the cached brand/style segment list to
    _call_text unmodified -- deleting the system_prompt kwarg at the call site would
    silently drop brand/style context, and this test would catch it."""
    from agents.blogging.blog_writer_agent.agent import BlogWriterAgent

    a = _agent()
    captured: dict = {}

    def fake_call_text(self, p, system_prompt=""):
        captured["system_prompt"] = system_prompt
        return '{"draft": 0}\n---DRAFT---\n# A Title\nBody.\n'

    monkeypatch.setattr(BlogWriterAgent, "_call_text", fake_call_text)
    monkeypatch.setattr(
        BlogWriterAgent, "_self_review", lambda self, d, allowed_claims_section="": d
    )

    out = a.run(_writer_input())
    assert "A Title" in out.draft
    assert captured["system_prompt"] is a._writing_system_prompt_with_content


def test_writer_run_json_fallback_forwards_system_prompt_content(monkeypatch) -> None:
    """run()'s JSON-mode fallback (triggered when the text path raises LLMJsonParseError)
    also passes the cached brand/style segment list to _call_agent_json -- the fallback is
    exactly the path that must not silently lose brand/style context."""
    from agents.blogging.blog_writer_agent.agent import BlogWriterAgent

    from llm_service import LLMJsonParseError

    a = _agent()
    captured: dict = {}
    monkeypatch.setattr(
        BlogWriterAgent,
        "_call_text",
        lambda self, p, system_prompt="": (_ for _ in ()).throw(
            LLMJsonParseError("bad draft text")
        ),
    )

    def fake_call_agent_json(self, p, system_prompt=""):
        captured["system_prompt"] = system_prompt
        return {"draft": "# Fallback\nBody."}

    monkeypatch.setattr(BlogWriterAgent, "_call_agent_json", fake_call_agent_json)
    monkeypatch.setattr(
        BlogWriterAgent, "_self_review", lambda self, d, allowed_claims_section="": d
    )
    out = a.run(_writer_input())
    assert "Fallback" in out.draft
    assert captured["system_prompt"] is a._writing_system_prompt_with_content


def test_fallback_draft_via_json_forwards_system_prompt_to_run_json_gate(monkeypatch) -> None:
    """_fallback_draft_via_json is the fourth route named in issue #7895 -- it bypasses
    _call_agent entirely (constructing its Agent inside shared/json_retry.py instead), so
    it needs its own check that it forwards whatever system_prompt it is given straight
    through to run_json_gate, unmodified."""
    from llm_service import CacheBreakpoint

    a = _agent()
    captured: dict = {}

    def fake_gate(model, system_prompt, prompt, **kwargs):
        captured["system_prompt"] = system_prompt
        return {"draft": "# From JSON"}

    monkeypatch.setattr(
        "agents.blogging.blog_writer_agent.agent.run_json_gate",
        fake_gate,
    )
    segments = a._writing_system_prompt_with_content
    out = a._fallback_draft_via_json("revise this draft", system_prompt=segments)
    assert out == "# From JSON"
    assert captured["system_prompt"] is segments
    assert isinstance(segments, list)
    segment = segments[1]
    assert isinstance(segment, CacheBreakpoint)
    assert "Brand" in segment.text
