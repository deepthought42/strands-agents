"""Tests for the blog writer agent.

Uses the shared ContentPlan factory from ``_content_plan_test_utils``.
"""

import re

import pytest
from agents.blogging.blog_research_agent.models import ResearchReference
from agents.blogging.blog_writer_agent import WriterInput, WriterOutput
from agents.blogging.shared.content_plan import ContentPlan, ContentPlanSection, TitleCandidate
from agents.blogging.shared.system_prompt_assembly import (
    build_headed_blogging_system_prompt_content,
)

from llm_service import DummyLLMClient, LLMJsonParseError

from ._content_plan_test_utils import make_content_plan
from .conftest import make_writer_agent


def _minimal_plan() -> ContentPlan:
    return make_content_plan(
        overarching_topic="Test topic",
        narrative_flow="Intro, main, wrap.",
        sections=[
            ContentPlanSection(title="Intro", coverage_description="Hook", order=0),
            ContentPlanSection(title="Main", coverage_description="Body", order=1),
        ],
        title_candidates=[TitleCandidate(title="T1", probability_of_success=0.5)],
    )


class _PromptCapturingLLM(DummyLLMClient):
    """Dummy LLM that records all prompts for tests.

    Since the blogging agents now use ``strands.Agent(model=llm)`` which calls
    ``stream()`` -> ``complete_json()``, prompt capture happens in
    ``complete_json`` (called by the inherited ``DummyLLMClient.stream``).
    """

    def __init__(self) -> None:
        super().__init__()
        self.all_prompts: list[str] = []

    def complete_json(self, prompt: str, **kwargs: object) -> dict:
        self.all_prompts.append(prompt)
        lowered = prompt.lower() if isinstance(prompt, str) else ""
        # Self-review prompt: return empty issues list
        if "review this draft" in lowered:
            return {"issues": []}
        # Deterministic fix prompt
        if "fix only these" in lowered:
            return {"draft": "# Draft\n\nPlaceholder draft content."}
        return {"draft": "# Draft\n\nPlaceholder draft content."}


def test_writer_input_requires_content_plan() -> None:
    """WriterInput raises when content_plan is missing."""
    with pytest.raises(ValueError, match="content_plan"):
        WriterInput(
            content_plan=None,  # type: ignore[arg-type]
        )


def test_golden_draft_h2_headings_match_content_plan_sections() -> None:
    """Regression: Markdown H2 headings follow planned section titles (order)."""
    plan = _minimal_plan()

    class H2DraftLLM(DummyLLMClient):
        """Override complete_json so DummyLLMClient.stream() returns a draft
        whose H2 headings match the content plan sections."""

        def complete_json(self, prompt, **kwargs):  # type: ignore[no-untyped-def]
            self._request_count += 1
            lowered = prompt.lower() if isinstance(prompt, str) else ""
            # Self-review prompt: return empty issues list
            if "review this draft" in lowered:
                return {"issues": []}
            # Deterministic fix prompt: pass through
            if "fix only these" in lowered:
                body = "\n\n".join(
                    f"## {s.title}\n\nBody for {s.title}."
                    for s in sorted(plan.sections, key=lambda x: x.order)
                )
                return {"draft": "# Post title\n\n" + body}
            # Default: return draft with planned H2 headings
            body = "\n\n".join(
                f"## {s.title}\n\nBody for {s.title}."
                for s in sorted(plan.sections, key=lambda x: x.order)
            )
            return {"draft": "# Post title\n\n" + body}

    agent = make_writer_agent(
        llm_client=H2DraftLLM(),
        writing_style_guide_content="Use clear sentence flow and plain language.",
        brand_spec_content="Brand voice: practical and trustworthy.",
    )
    out = agent.run(WriterInput(research_document="Compiled research text.", content_plan=plan))
    h2s = re.findall(r"^## (.+)$", out.draft, re.MULTILINE)
    expected = [s.title for s in sorted(plan.sections, key=lambda x: x.order)]
    assert h2s == expected


def test_call_agent_json_raises_on_non_dict_response(monkeypatch) -> None:
    """Regression: a non-dict parsed JSON response raises LLMJsonParseError.

    ``extract_json_from_response`` can return lists/strings/numbers, and
    ``_call_agent_json`` is annotated (and relied upon by callers) to
    return ``dict``. A non-dict response must fail loudly, not surface as
    an AttributeError/KeyError at a caller's ``.get(...)`` call.
    """
    agent = make_writer_agent()
    monkeypatch.setattr(agent, "_call_json_raw", lambda *a, **kw: "[1, 2, 3]")

    with pytest.raises(LLMJsonParseError):
        agent._call_agent_json("prompt")


def test_blog_writer_agent_run() -> None:
    """BlogWriterAgent returns a non-empty draft from research + content plan."""
    agent = make_writer_agent(
        writing_style_guide_content="Use clear sentence flow and plain language.",
        brand_spec_content="Brand voice: practical and trustworthy.",
    )

    draft_input = WriterInput(
        research_document="Compiled research: Source 1 summary. Source 2 key points.",
        content_plan=_minimal_plan(),
    )

    result = agent.run(draft_input)

    assert isinstance(result, WriterOutput)
    assert result.draft
    assert (
        "draft" in result.draft.lower()
        or "introduction" in result.draft.lower()
        or "placeholder" in result.draft.lower()
    )


def test_run_skips_self_review_only_for_exact_placeholder() -> None:
    """run() skips self-review when the draft is exactly the placeholder, but not for
    real drafts that merely share the placeholder's leading text (regression for the
    prior brittle ``startswith`` check)."""
    from unittest.mock import patch

    from agents.blogging.blog_writer_agent.agent import _PLACEHOLDER_DRAFT

    agent = make_writer_agent(
        writing_style_guide_content="Use clear sentence flow and plain language.",
        brand_spec_content="Brand voice: practical and trustworthy.",
    )
    draft_input = WriterInput(
        research_document="Compiled research text.",
        content_plan=_minimal_plan(),
    )

    with patch.object(agent, "_self_review", wraps=agent._self_review) as mock_review:
        with patch.object(agent, "_call_text", return_value=""):
            result = agent.run(draft_input)
        assert result.draft == _PLACEHOLDER_DRAFT
        mock_review.assert_not_called()

    with patch.object(agent, "_self_review", wraps=agent._self_review) as mock_review:
        with patch.object(
            agent,
            "_call_text",
            return_value=f"---DRAFT---\n{_PLACEHOLDER_DRAFT} plus real content that follows.",
        ):
            result = agent.run(draft_input)
        assert result.draft != _PLACEHOLDER_DRAFT
        mock_review.assert_called_once()


def test_blog_writer_agent_with_style_guide() -> None:
    """BlogWriterAgent uses writing_style_guide_content passed at init."""
    agent = make_writer_agent(
        writing_style_guide_content="Write like a mentor. Clear, natural-length sentences. No em dashes.",
        brand_spec_content="Brand voice: practical and clear.",
    )

    draft_input = WriterInput(
        research_document="Research here.",
        content_plan=_minimal_plan(),
    )

    result = agent.run(draft_input)
    assert result.draft


def test_blog_writer_agent_run_with_research_references() -> None:
    """BlogWriterAgent runs parallel extraction then draft when research_references is provided."""
    agent = make_writer_agent(
        writing_style_guide_content="Use clear sentence flow and plain language.",
        brand_spec_content="Brand voice: practical and trustworthy.",
    )

    refs = [
        ResearchReference(
            title="Source One",
            url="https://example.com/one",
            summary="Summary of first source.",
            key_points=["Point A", "Point B"],
        ),
        ResearchReference(
            title="Source Two",
            url="https://example.com/two",
            summary="Summary of second source.",
        ),
    ]
    draft_input = WriterInput(
        research_document=None,
        research_references=refs,
        content_plan=_minimal_plan(),
    )

    result = agent.run(draft_input)

    assert isinstance(result, WriterOutput)
    assert result.draft
    assert (
        "draft" in result.draft.lower()
        or "placeholder" in result.draft.lower()
        or "introduction" in result.draft.lower()
    )


def test_draft_prompt_includes_provided_brand_spec() -> None:
    """When brand_spec_content is provided, it reaches the cached system-prompt
    segment (not the user-turn draft prompt, which no longer embeds it)."""
    from llm_service import CacheBreakpoint

    llm = _PromptCapturingLLM()
    agent = make_writer_agent(
        llm_client=llm,
        writing_style_guide_content="Use concise, natural sentences.",
        brand_spec_content="MyBrand: Test brand. Voice: friendly and clear.",
    )
    draft_input = WriterInput(
        research_document="Research here.",
        content_plan=_minimal_plan(),
    )
    agent.run(draft_input)

    assert len(agent._system_prompt_content) == 1
    segment = agent._system_prompt_content[0]
    assert isinstance(segment, CacheBreakpoint)
    assert "MyBrand: Test brand." in segment.text
    assert "--- BRAND SPEC ---" in segment.text
    # The user-turn draft prompt no longer embeds the brand spec.
    draft_prompt = llm.all_prompts[0]
    assert "MyBrand: Test brand." not in draft_prompt
    assert "BRAND AND STYLE" not in draft_prompt


def test_system_prompt_content_delegates_to_shared_helper_both_sections() -> None:
    """agent._system_prompt_content equals build_headed_blogging_system_prompt_content
    called with the same (stripped) inputs -- pins the constructor's wiring
    (strip, then pass brand before writing) without re-encoding the helper's
    own heading/join format, which is already covered by
    test_system_prompt_assembly.py."""
    brand, style = "  MyBrand: Test brand.  \n", "\tUse concise, natural sentences.\n"

    agent = make_writer_agent(brand_spec_content=brand, writing_style_guide_content=style)

    assert agent._system_prompt_content == build_headed_blogging_system_prompt_content(
        brand.strip(), style.strip()
    )


def test_system_prompt_content_delegates_to_shared_helper_writing_guide_blank() -> None:
    """Same wiring check as above, with writing_style_guide_content blank."""
    brand, style = "MyBrand: Test brand.", "   "

    agent = make_writer_agent(brand_spec_content=brand, writing_style_guide_content=style)

    assert agent._system_prompt_content == build_headed_blogging_system_prompt_content(
        brand.strip(), style.strip()
    )


def test_system_prompt_content_delegates_to_shared_helper_brand_spec_blank() -> None:
    """Same wiring check as above, with brand_spec_content blank."""
    brand, style = "", "Use concise, natural sentences."

    agent = make_writer_agent(brand_spec_content=brand, writing_style_guide_content=style)

    assert agent._system_prompt_content == build_headed_blogging_system_prompt_content(
        brand.strip(), style.strip()
    )


def test_system_prompt_content_is_none_when_both_sections_blank() -> None:
    """Both brand_spec_content and writing_style_guide_content blank/whitespace-only
    yields None, not an empty-text CacheBreakpoint -- matching
    build_headed_blogging_system_prompt_content's documented empty-input case."""
    agent = make_writer_agent(brand_spec_content="", writing_style_guide_content="   ")

    assert agent._system_prompt_content is None


def test_outline_for_prompt_includes_section_titles() -> None:
    """outline_for_prompt flattens the content plan for LLM consumption."""
    inp = WriterInput(
        research_document="R",
        content_plan=_minimal_plan(),
    )
    text = inp.outline_for_prompt()
    assert "Test topic" in text
    assert "Intro" in text
    assert "Main" in text


def test_draft_run_requires_both_guidelines() -> None:
    """Draft agent rejects run() when brand/writing guidelines are missing."""
    agent = make_writer_agent(writing_style_guide_content="", brand_spec_content="")
    draft_input = WriterInput(
        research_document="Research here.",
        content_plan=_minimal_plan(),
    )
    with pytest.raises(ValueError, match="requires both brand and writing guidelines"):
        agent.run(draft_input)


def test_blog_writer_agent_derives_text_mode_sibling_when_given_llm_client_model() -> None:
    """BlogWriterAgent's drafting path uses the ``---DRAFT---`` marker
    pattern which only works in text mode. When constructed with a real
    ``LLMClientModel`` (the production path via ``get_strands_model("blog")``
    which defaults to JSON), the writer must derive a ``response_format="text"``
    sibling internally so ``_call_agent`` does not get JSON-forced.
    """
    from llm_service.strands_adapter import LLMClientModel

    json_model = LLMClientModel(DummyLLMClient(), agent_key="blog", response_format="json")
    agent = make_writer_agent(
        llm_client=json_model,
        writing_style_guide_content="Use clear sentences.",
        brand_spec_content="Brand voice: practical.",
    )

    # The original JSON-mode model is preserved for ``_call_agent_json``.
    assert agent._model is json_model
    assert agent._model.get_config()["response_format"] == "json"

    # The text-mode sibling is a different instance with response_format flipped.
    assert agent._text_model is not json_model
    assert agent._text_model.get_config()["response_format"] == "text"

    # Backing client is shared — same retries, telemetry, rate limit guard.
    assert agent._text_model._client is json_model._client


def test_blog_writer_agent_falls_back_when_llm_client_is_not_strands_model() -> None:
    """Test fixtures and offline callers often pass a raw ``LLMClient``
    (DummyLLMClient, MagicMock) instead of a Strands ``LLMClientModel``.
    The writer must not crash trying to derive a sibling — fall back to
    using the injected object as both models."""
    raw_client = DummyLLMClient()
    agent = make_writer_agent(
        llm_client=raw_client,
        writing_style_guide_content="Use clear sentences.",
        brand_spec_content="Brand voice: practical.",
    )
    assert agent._model is raw_client
    assert agent._text_model is raw_client


# ---------------------------------------------------------------------------
# system_prompt_content degradation (issue #7895, AC "passing no segments
# reproduces today's exact Agent construction"): a writer with no brand/style
# content, and _call_agent called with no system_prompt at all, must both
# fall back to the bare WRITING_SYSTEM_PROMPT string -- never a one-element
# content-block list -- so a non-caching client sees byte-identical input.
# ---------------------------------------------------------------------------


def test_writing_system_prompt_with_content_is_plain_string_when_blank() -> None:
    """With no brand/style content, _writing_system_prompt_with_content degrades to the
    plain WRITING_SYSTEM_PROMPT string -- not a one-element list -- so a writer with no
    guidelines configured reproduces today's exact (pre-caching) Agent(system_prompt=...)
    construction. (_assert_guidelines_present blocks real use of such a writer, but the
    attribute itself must still degrade correctly, e.g. for _call_agent's own
    "system_prompt or WRITING_SYSTEM_PROMPT" fallback to be a no-op here.)"""
    from agents.blogging.blog_writer_agent.prompts import WRITING_SYSTEM_PROMPT

    agent = make_writer_agent(brand_spec_content="", writing_style_guide_content="   ")

    assert agent._system_prompt_content is None
    assert agent._writing_system_prompt_with_content == WRITING_SYSTEM_PROMPT
    assert isinstance(agent._writing_system_prompt_with_content, str)


def test_call_agent_with_no_system_prompt_builds_plain_string_agent(monkeypatch) -> None:
    """_call_agent's documented "no segments -> today's exact Agent construction"
    postcondition: with system_prompt omitted, Agent is built with a bare persona
    string, not a content-block list."""
    import agents.blogging.blog_writer_agent.agent as agent_module

    captured: dict = {}

    class _FakeAgent:
        def __init__(self, *, model, system_prompt):
            captured["model"] = model
            captured["system_prompt"] = system_prompt

        def __call__(self, prompt):
            return "response"

    monkeypatch.setattr(agent_module, "Agent", _FakeAgent)
    a = make_writer_agent()
    out = a._call_agent("some-model", "a prompt")
    assert out == "response"
    assert captured["model"] == "some-model"
    assert captured["system_prompt"] == agent_module.WRITING_SYSTEM_PROMPT
    assert isinstance(captured["system_prompt"], str)
