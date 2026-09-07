"""
Blog writer agent: takes a research document and an outline and generates
a blog post draft that complies with a brand and writing style guide.
"""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import Any, Callable, List, Optional, Union

from agents.blogging.blog_copy_editor_agent.models import FeedbackItem
from agents.blogging.blog_plan_critic_agent import BlogPlanCriticAgent
from agents.blogging.blog_planning_agent.prompts import GENERATE_PLAN_SYSTEM, REFINE_PLAN_SYSTEM
from agents.blogging.shared.agent_base import _BlogAgentBase
from agents.blogging.shared.content_plan import PlanningInput, PlanningPhaseResult
from agents.blogging.shared.content_planning_loop import (
    complete_plan_json,
    run_content_planning_loop,
)
from agents.blogging.shared.content_profile import LengthPolicy
from agents.blogging.shared.json_retry import run_json_gate
from agents.blogging.shared.prompt_budget import resolve_model_context_tokens
from agents.blogging.shared.system_prompt_assembly import (
    SystemContentSegment,
    build_headed_blogging_system_prompt_content,
    build_system_prompt_with_content,
)
from agents.blogging.shared.text_parsing import (
    extract_draft_after_marker,
    extract_json_array_from_text,
    format_feedback_item_line,
    unwrap_llm_cause,
)
from pydantic import ValidationError
from strands import Agent

from llm_service import (
    LLMError,
    LLMJsonParseError,
    LLMRateLimitError,
    LLMTemporaryError,
    compact_text,
    extract_json_from_response,
)

try:
    from llm_service.strands_adapter import LLMClientModel
except ImportError:  # pragma: no cover - optional adapter absent in some test harnesses
    LLMClientModel = None  # type: ignore[misc, assignment]

from . import revision, self_review
from .feedback_tracker import PersistentFeedbackItem
from .models import (
    ReviseWriterInput,
    RevisionPlan,
    UncertaintyQuestion,
    WriterInput,
    WriterOutput,
    WritingGuidelineUpdate,
)
from .prompts import (
    ANALYZE_USER_FEEDBACK_FOR_GUIDELINES_PROMPT,
    DRAFT_TASK_INSTRUCTIONS,
    ESCALATION_SUMMARY_PROMPT,
    UNCERTAINTY_DETECTION_PROMPT,
    USER_FEEDBACK_REVISION_INSTRUCTIONS,
    WRITING_SYSTEM_PROMPT,
)

logger = logging.getLogger(__name__)

BATCH_EXECUTE_MAX_RETRIES = 3
BATCH_EXECUTE_BACKOFF_BASE_SECONDS = 2.0

_PLACEHOLDER_DRAFT = "# Draft\n\nNo draft was generated. Check the model response or try again."

# Soft JSON instruction appended on JSON-oriented LLM calls (shared to avoid drift).
_SOFT_JSON_INSTRUCTION = "\n\nRespond with valid JSON only, no markdown fences."

# Context budget for compaction — content exceeding these thresholds is compacted
# (LLM-summarised) rather than naively truncated, preserving technical detail.
# The model context (e.g. 262K tokens ≈ 917K chars) is large enough that
# compaction should rarely be needed.
COMPACT_OUTLINE_CHARS = 200_000


_NO_ALLOWED_CLAIMS_SECTION = (
    "---\n"
    "ALLOWED CLAIMS: none available. An allowed-claims list was supplied but contains "
    "no usable claims. Do not make any factual or statistical claims in this draft; "
    "rephrase to avoid them or omit them entirely. No [CLAIM:id] tag should appear "
    "anywhere, since no ID is available to use."
)

# Discriminator for how the writer must treat factual/statistical claims. Callers
# that need to branch on the policy (e.g. run()'s numeric-figure requirement) should
# use _classify_allowed_claims / this constant set rather than comparing rendered
# prompt text against _NO_ALLOWED_CLAIMS_SECTION -- a future wording change to that
# text would otherwise silently break a text-equality-based branch.
_CLAIMS_POLICY_NONE = "none"  # no allowed-claims artifact was supplied at all
_CLAIMS_POLICY_RESTRICTIVE = "restrictive"  # artifact supplied but yields no usable claim
_CLAIMS_POLICY_POPULATED = "populated"  # at least one usable claim


def _classify_allowed_claims(allowed_claims: Optional[dict]) -> str:
    """Classify ``allowed_claims`` into a claims-policy discriminator, independent
    of how ``_render_allowed_claims_section`` renders that policy as prompt text.

    Preconditions:
        - Same as ``_render_allowed_claims_section``: ``allowed_claims`` is ``None``
          or a dict shaped like allowed_claims.json.
    Postconditions:
        - Returns ``_CLAIMS_POLICY_NONE`` when ``allowed_claims`` is ``None`` or not
          a dict.
        - Returns ``_CLAIMS_POLICY_RESTRICTIVE`` when ``allowed_claims`` is a dict
          but yields no usable claim (``claims`` missing, empty, not a list, or
          every entry malformed).
        - Otherwise returns ``_CLAIMS_POLICY_POPULATED``.
    """
    if not isinstance(allowed_claims, dict):
        return _CLAIMS_POLICY_NONE
    claims = allowed_claims.get("claims")
    if not isinstance(claims, list):
        return _CLAIMS_POLICY_RESTRICTIVE
    has_usable_claim = any(isinstance(c, dict) and c.get("id") and c.get("text") for c in claims)
    return _CLAIMS_POLICY_POPULATED if has_usable_claim else _CLAIMS_POLICY_RESTRICTIVE


def _render_allowed_claims_section(allowed_claims: Optional[dict]) -> str:
    """Render allowed_claims.json content as a prompt section, or "" when no artifact
    was supplied at all.

    Preconditions:
        - ``allowed_claims`` is ``None`` or a dict shaped like allowed_claims.json
          (``{"topic": ..., "claims": [{"id": ..., "text": ...}, ...]}``); malformed
          claim entries are tolerated (skipped) rather than raising.
    Postconditions:
        - Returns ``""`` when ``_classify_allowed_claims`` yields
          ``_CLAIMS_POLICY_NONE`` — i.e. no allowed-claims artifact was supplied at
          all. The writer prompt's own instructions cover this case (write
          normally, no ``[CLAIM:id]`` tags).
        - Returns the restrictive ``_NO_ALLOWED_CLAIMS_SECTION`` block when
          ``_classify_allowed_claims`` yields ``_CLAIMS_POLICY_RESTRICTIVE`` — a
          present-but-empty artifact (e.g. extraction ran and found nothing
          supportable) must not be treated the same as "no artifact was checked",
          since the latter silently permits unsupported factual claims.
        - Otherwise (``_CLAIMS_POLICY_POPULATED``) returns a ``---``-delimited
          prompt block listing every claim as ``- [id] text``, instructing the
          model to tag claims with the given IDs, to invent none, and
          (self-contained, so any rewrite/revision caller that embeds this block
          verbatim gets consistent guidance without adding its own wrapper text)
          to preserve existing ``[CLAIM:id]`` tags when revising.
    """
    policy = _classify_allowed_claims(allowed_claims)
    if policy == _CLAIMS_POLICY_NONE:
        return ""
    if policy == _CLAIMS_POLICY_RESTRICTIVE:
        return _NO_ALLOWED_CLAIMS_SECTION
    claims = allowed_claims.get("claims")
    lines = [
        f"- [{c.get('id')}] {c.get('text')}"
        for c in claims
        if isinstance(c, dict) and c.get("id") and c.get("text")
    ]
    return (
        "---\n"
        "ALLOWED CLAIMS (tag every factual/statistical claim with [CLAIM:id] using "
        "only an ID from this list; never invent an ID; if no claim here supports an "
        "assertion, rephrase or omit it; when revising, preserve any existing "
        "[CLAIM:id] tags exactly; do not remove, renumber, or reassign them "
        "unless the claim they support is removed from the draft):\n"
        "---\n" + "\n".join(lines)
    )


def _write_draft_to_path(draft: str, path: Union[str, Path]) -> None:
    """Write draft content to path; create parent dirs if needed. Log the saved path.

    Preconditions:
        - ``draft`` must be a string (may be empty).
        - ``path`` must be a ``str`` or ``pathlib.Path``.
        - ``path`` must not contain ``..`` components (rejects path traversal).
    Postconditions:
        - Parent directories of ``path`` exist.
        - The resolved path contains ``draft`` as UTF-8 text.
        - A success log records the resolved path.
    """
    if not isinstance(draft, str):
        raise TypeError(f"draft must be a string, got {type(draft).__name__}")
    if not isinstance(path, (str, Path)):
        raise TypeError(f"path must be a str or Path, got {type(path).__name__}")
    raw = Path(path)
    if ".." in raw.parts:
        raise ValueError(f"Draft path must not contain '..' components: {path!r}")
    p = raw.expanduser().resolve()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(draft, encoding="utf-8")
    logger.info("Draft written to %s", p)


class BlogWriterAgent(_BlogAgentBase):
    """
    Expert agent that generates a blog post draft from a research document and outline,
    following a provided brand and writing style guide.
    """

    def __init__(
        self,
        llm_client: Any,
        *,
        writing_style_guide_content: str = "",
        brand_spec_content: str = "",
    ) -> None:
        """
        Preconditions:
            - llm_client is not None.
        Callers load writing style and brand spec files before instantiation and pass full contents here.

        Raises:
            ValueError: if ``llm_client`` is ``None``.
        """
        if llm_client is None:
            raise ValueError("llm_client must not be None")
        super().__init__(llm_client)
        # ``_call_text`` produces the ``---DRAFT---`` hybrid format (JSON
        # marker line + Markdown body), which only works when the underlying
        # adapter is in text mode — JSON mode would force a single JSON object
        # on the wire and the marker pattern would disappear. Derive a
        # text-mode sibling from the injected model when possible; fall back
        # to the passed model so test fixtures (MagicMock, fakes) continue to
        # work. ``_call_json_raw`` / ``_call_agent_json`` use ``self._model``
        # directly for structured helpers. ``LLMClientModel`` is imported at
        # module level (optional: ``None`` when the adapter is absent).
        if LLMClientModel is not None and isinstance(llm_client, LLMClientModel):
            self._text_model = llm_client.clone(response_format="text")
        else:
            self._text_model = llm_client
        self._writing_style_prompt = (writing_style_guide_content or "").strip()
        self._brand_spec_prompt = (brand_spec_content or "").strip()
        # Cacheable system-content segment carrying the (headed) brand spec and
        # writing style guide, or None when both are blank. Delivered via
        # Agent(system_prompt=...) at the call sites below rather than embedded
        # as plain text in the user prompt, so a stable prefix isn't re-billed
        # on every turn.
        self._system_prompt_content = build_headed_blogging_system_prompt_content(
            self._brand_spec_prompt, self._writing_style_prompt
        )
        self._writing_system_prompt_with_content = build_system_prompt_with_content(
            WRITING_SYSTEM_PROMPT, self._system_prompt_content
        )

    def _call_agent(
        self, model: Any, prompt: str, system_prompt: Union[str, List[SystemContentSegment]] = ""
    ) -> str:
        """Construct a Strands Agent, invoke it, and return stripped text.

        Shared invocation path for ``_call_text`` and ``_call_json_raw``, which
        differ only in which model they pass.

        Preconditions:
            - ``model`` is a configured LLM client/model object.
            - ``prompt`` is a non-empty string.
            - ``system_prompt`` is a plain persona string, or a Strands
              system-content-block list (e.g. from
              ``build_system_prompt_with_content``) when a cacheable segment
              is being attached. When falsy (e.g. ``""``), falls back to
              ``WRITING_SYSTEM_PROMPT``.
        Postconditions:
            - Returns the agent's response as a stripped string.
        Raises:
            ValueError: if ``prompt`` is not a non-empty string.
        """
        if not isinstance(prompt, str) or not prompt.strip():
            raise ValueError("prompt must be a non-empty string")
        agent = Agent(model=model, system_prompt=system_prompt or WRITING_SYSTEM_PROMPT)
        return str(agent(prompt)).strip()

    def _call_text(
        self, prompt: str, system_prompt: Union[str, List[SystemContentSegment]] = ""
    ) -> str:
        """Call the text-mode Strands Agent and return its stripped text output.

        Used for drafting and revision paths that emit the ``---DRAFT---``
        marker + Markdown hybrid format. The text-mode sibling avoids forcing
        ``response_format=json_object`` on the wire so the marker survives.

        Preconditions:
            - ``prompt`` is a non-empty string (enforced by ``_call_agent``).
            - ``system_prompt``, if falsy, falls back to
              ``WRITING_SYSTEM_PROMPT`` (via ``_call_agent``).
        """
        return self._call_agent(self._text_model, prompt, system_prompt)

    def _call_json_raw(
        self, prompt: str, system_prompt: Union[str, List[SystemContentSegment]] = ""
    ) -> str:
        """Invoke the injected model via Strands and return its stripped assistant text.

        Uses ``self._model`` as supplied by the caller (typically already configured
        for structured/JSON-oriented completions). Does not clone or force
        ``response_format=json_object`` here — callers that need a specific wire
        format must configure that on the injected client. Prefer this over
        parsing when a caller needs to extract JSON itself (e.g. planning paths
        that call ``extract_json_from_response``).

        Preconditions:
            - ``prompt`` is a non-empty string (enforced by ``_call_agent``).
            - ``system_prompt``, if falsy, falls back to
              ``WRITING_SYSTEM_PROMPT`` (via ``_call_agent``).
        """
        return self._call_agent(self._model, prompt, system_prompt)

    def _call_agent_json(
        self, prompt: str, system_prompt: Union[str, List[SystemContentSegment]] = ""
    ) -> dict:
        """Invoke the injected model via Strands and parse JSON from the result.

        Appends a soft JSON-only instruction and runs ``extract_json_from_response``
        as defensive cleanup if the model wraps the object. Relies on ``self._model``
        already being suitable for structured replies; this method does not force
        ``response_format=json_object`` on the wire.

        Preconditions:
            - ``prompt`` is a non-empty string (enforced by ``_call_agent`` via
              ``_call_json_raw``).
            - ``system_prompt``, if falsy, falls back to ``WRITING_SYSTEM_PROMPT``
              (via ``_call_json_raw``). Accepts a plain persona string or a
              Strands system-content-block list (e.g. from
              ``build_system_prompt_with_content``).
        Postconditions:
            - Returns a parsed JSON **object** (``dict``). Non-dict JSON values
              raise ``LLMJsonParseError``.
        Raises:
            ``LLMJsonParseError`` when the response contains no extractable JSON,
            or when the extracted JSON parses to something other than a dict.
        """
        raw = self._call_json_raw(
            prompt + _SOFT_JSON_INSTRUCTION,
            system_prompt,
        )
        data = extract_json_from_response(raw)
        if not isinstance(data, dict):
            raise LLMJsonParseError(f"Expected a JSON object, got {type(data).__name__}")
        return data

    def _fallback_draft_via_json(
        self, prompt: str, system_prompt: Union[str, List[SystemContentSegment]] = ""
    ) -> Optional[str]:
        """Parse a revised draft via shared JSON retry when the text path fails.

        Preconditions:
            - ``prompt`` is a non-empty string (same prompt used for the text path).
            - ``system_prompt``, if given, mirrors the one used for the failed
              text-path call; falls back to ``WRITING_SYSTEM_PROMPT`` when empty,
              matching ``_call_text``/``_call_json_raw``.
        Postconditions:
            - Returns a non-empty stripped draft string on success.
            - Returns ``None`` when JSON cannot yield a usable draft (caller keeps
              the prior draft).
            - Transient LLM transport errors (``LLMRateLimitError`` /
              ``LLMTemporaryError``), including when strands wraps them in
              ``EventLoopException``, propagate unwrapped from
              ``run_json_gate`` so the draft-stage retry funnel can catch them.
        Raises:
            ValueError: if ``prompt`` is not a non-empty string.
        """
        if not isinstance(prompt, str) or not prompt.strip():
            raise ValueError("prompt must be a non-empty string")

        strict_json_suffix = (
            "\n\nRespond with a single JSON object only (no markdown, no code fence). "
            'Keys: "draft" (string — the full revised blog post in Markdown).'
        )

        # fresh_agent_per_attempt=True preserves the prior behavior of constructing a
        # new Agent for every attempt (including retries after a JSON-parse failure).
        data = run_json_gate(
            self._model,
            system_prompt or WRITING_SYSTEM_PROMPT,
            prompt + _SOFT_JSON_INSTRUCTION,
            max_attempts=2,
            strict_json_suffix=strict_json_suffix,
            fresh_agent_per_attempt=True,
            fallback_builder=lambda e: {},
            logger=logger,
        )
        raw_draft = data.get("draft") if isinstance(data, dict) else None
        if isinstance(raw_draft, str) and raw_draft.strip():
            return raw_draft.strip()
        return None

    def _assert_guidelines_present(self) -> None:
        """Require both brand and writing guideline inputs before drafting/revising."""
        missing: list[str] = []
        if not self._brand_spec_prompt:
            missing.append("brand guidelines")
        if not self._writing_style_prompt:
            missing.append("writing guidelines")
        if missing:
            raise ValueError(
                "BlogWriterAgent requires both brand and writing guidelines to ensure compliant output. "
                f"Missing: {', '.join(missing)}."
            )

    # ------------------------------------------------------------------
    # Planning (delegates to shared.content_planning_loop; also used by
    # blog_planning_agent.BlogPlanningAgent, which delegates identically)
    # ------------------------------------------------------------------

    def _complete_plan_json(
        self,
        prompt: str,
        *,
        system: str,
        on_llm_request: Optional[Callable[[str], None]],
        max_parse_retries: int,
    ) -> tuple[dict[str, Any], int]:
        """Delegate to ``shared.content_planning_loop.complete_plan_json``, wiring this
        agent's ``_call_agent_json`` / ``_call_json_raw`` as the JSON and raw-text callers.

        Postconditions:
            - Returns the parsed plan dict and the number of parse retries consumed,
              per ``complete_plan_json``'s contract.
        """
        return complete_plan_json(
            prompt,
            system=system,
            on_llm_request=on_llm_request,
            max_parse_retries=max_parse_retries,
            call_json_fn=lambda p, s: self._call_agent_json(p, system_prompt=s),
            call_raw_fn=lambda p, s: self._call_json_raw(p, system_prompt=s),
        )

    def plan_content(
        self,
        planning_input: PlanningInput,
        *,
        length_policy: LengthPolicy,
        on_llm_request: Optional[Callable[[str], None]] = None,
        max_iterations: int = 5,
        max_parse_retries: int = 3,
        plan_critic: Optional[BlogPlanCriticAgent] = None,
        work_dir: Optional[Union[str, Path]] = None,
    ) -> PlanningPhaseResult:
        """Generate and refine a ContentPlan until the planner (and optional critic) agree.

        When ``plan_critic`` is supplied, its verdict is authoritative: the loop
        terminates only when the planner's self-eval is done AND the critic
        approves. Refine feedback comes from the critic's structured violations
        instead of a generic string. When absent, legacy planner-self-eval only.

        Args:
            planning_input: Brief/context the planner drafts and refines against.
            length_policy: Target-length policy passed through to the loop.
            on_llm_request: Optional progress callback invoked before each LLM call.
            max_iterations: Cap on generate/refine loop iterations before giving up.
            max_parse_retries: Cap on JSON-parse retries per LLM call within the loop.
            plan_critic: Optional critic agent whose approval is required for the
                loop to terminate; omit for legacy planner-self-eval-only behavior.
            work_dir: Optional directory to persist intermediate plan artifacts to.

        Returns:
            The final ``PlanningPhaseResult`` (content plan plus loop metadata),
            per ``run_content_planning_loop``'s contract.
        """
        return run_content_planning_loop(
            planning_input,
            length_policy=length_policy,
            on_llm_request=on_llm_request,
            max_iterations=max_iterations,
            max_parse_retries=max_parse_retries,
            plan_critic=plan_critic,
            brand_spec_prompt=self._brand_spec_prompt,
            writing_guidelines=self._writing_style_prompt,
            work_dir=work_dir,
            generate_system=GENERATE_PLAN_SYSTEM,
            refine_system=REFINE_PLAN_SYSTEM,
            complete_plan_json_fn=self._complete_plan_json,
            planner_context_tokens=resolve_model_context_tokens(self._model),
        )

    # ------------------------------------------------------------------
    # Self-check: deterministic + LLM review
    # ------------------------------------------------------------------

    def _deterministic_self_check(self, draft: str) -> list[str]:
        """Scan draft for mechanical violations. Returns list of violation descriptions.

        Delegates to ``self_review.deterministic_self_check``; see that function
        (and its ``BANNED_PHRASES`` / ``VAGUE_CITATION_PATTERNS`` / threshold
        constants) in ``self_review.py`` for the full rule set.

        Preconditions:
            - ``draft`` is a string (may be empty).
        Raises:
            TypeError: if ``draft`` is not a string.
        """
        return self_review.deterministic_self_check(draft)

    def _fix_deterministic_violations(
        self, draft: str, violations: list[str], allowed_claims_section: str = ""
    ) -> str:
        """Call LLM once to fix deterministic violations. Returns cleaned draft.

        Delegates to ``self_review.fix_deterministic_violations``, passing
        ``self._call_text`` as the text-completion callback.

        Preconditions:
            - ``draft`` is a non-empty string when callers intend a real fix (empty is allowed).
            - ``violations`` is a list of human-readable violation strings (may be empty).
            - ``allowed_claims_section`` is an already-rendered allowed-claims prompt
              block (e.g. via ``_render_allowed_claims_section``), or ``""``.
        Postconditions:
            - On success with extractable fixed draft, returns that stripped draft.
            - When ``allowed_claims_section`` is non-empty, the fix prompt instructs
              the model to preserve existing ``[CLAIM:id]`` tags.
            - On soft-fail (``LLMError`` excluding types re-raised below, or
              ``json.JSONDecodeError`` / ``TypeError`` / ``ValueError`` / ``AttributeError``),
              logs with traceback via ``logger.exception`` and returns the original ``draft``.
            - ``LLMRateLimitError`` and ``LLMTemporaryError`` (including when wrapped in
              ``EventLoopException``) propagate as the unwrapped cause.
            - Unexpected exceptions propagate unchanged.
        """
        return self_review.fix_deterministic_violations(
            draft, violations, self._call_text, allowed_claims_section
        )

    def _llm_self_review(self, draft: str, allowed_claims_section: str = "") -> str:
        """Run a focused LLM self-review for subjective violations. Returns cleaned draft.

        Delegates to ``self_review.llm_self_review``, passing ``self._call_text``
        as the text-completion callback.

        Preconditions:
            - ``draft`` is a string (may be empty).
            - ``allowed_claims_section`` is an already-rendered allowed-claims prompt
              block (e.g. via ``_render_allowed_claims_section``), or ``""``.
        Postconditions:
            - On success, returns the reviewed/fixed draft or the original when no issues.
            - When issues are found and ``allowed_claims_section`` is non-empty, the fix
              prompt instructs the model to preserve existing ``[CLAIM:id]`` tags.
            - If the response's JSON parses to a value that is not a list of issue
              dicts (e.g. a top-level object, or salvaged prose residue with no
              recoverable issues array), returns the original ``draft`` unchanged.
            - List elements lacking a truthy ``"issue"`` key are discarded before
              use (whether the list came directly from ``extract_json_from_response``
              or from the prose-rescan fallback); if none remain, returns the
              original ``draft`` unchanged.
            - On soft-fail (``LLMError`` excluding types re-raised below, or
              ``json.JSONDecodeError`` / ``TypeError`` / ``ValueError`` / ``AttributeError``),
              logs with traceback via ``logger.exception`` and returns the original ``draft``.
            - ``LLMRateLimitError`` and ``LLMTemporaryError`` (including when wrapped in
              ``EventLoopException``) propagate as the unwrapped cause.
            - Unexpected exceptions propagate unchanged.
        """
        return self_review.llm_self_review(draft, self._call_text, allowed_claims_section)

    def _self_review(self, draft: str, allowed_claims_section: str = "") -> str:
        """Run deterministic check then LLM self-review. Returns cleaned draft.

        Orchestrates the delegating methods above (``_fix_deterministic_violations``
        and ``_llm_self_review``, rather than delegating directly to
        ``self_review.self_review``) so that each sub-step still runs through this
        agent's own bound methods.

        ``allowed_claims_section`` is an already-rendered allowed-claims prompt
        block (e.g. via ``_render_allowed_claims_section``), or ``""``; forwarded
        unchanged to both sub-steps so a mechanical or subjective rewrite cannot
        silently drop or corrupt ``[CLAIM:id]`` tagging.

        Both sub-steps (``_fix_deterministic_violations``, ``_llm_self_review``)
        already return the original draft on their own soft-fail paths, so this
        method has no additional failure handling of its own.
        """
        # Step 1: Deterministic checks
        violations = self._deterministic_self_check(draft)
        if violations:
            logger.info("Deterministic self-check found %s violation(s)", len(violations))
            draft = self._fix_deterministic_violations(draft, violations, allowed_claims_section)

        # Step 2: LLM self-review for subjective issues
        draft = self._llm_self_review(draft, allowed_claims_section)

        return draft

    def run(
        self,
        draft_input: WriterInput,
        *,
        on_llm_request: Optional[Callable[[str], None]] = None,
        draft_output_path: Optional[Union[str, Path]] = None,
    ) -> WriterOutput:
        """
        Generate a blog post draft from the approved content plan.

        When draft_output_path is set, writes the draft to that path and logs the path.

        Preconditions:
            - Brand and writing guidelines are present (enforced by
              ``_assert_guidelines_present``).
            - ``draft_input`` is a valid ``WriterInput``.
        Postconditions:
            - Returns a ``WriterOutput`` with a non-empty draft string.
            - The prompt includes an ALLOWED CLAIMS section per
              ``_render_allowed_claims_section(draft_input.allowed_claims)``:
              a list of ``[CLAIM:id]``-taggable claims when at least one claim
              has a non-empty ``id`` and ``text``; a restrictive "make no
              factual/statistical claims" instruction when ``allowed_claims``
              is a dict but yields no usable claim; omitted entirely only when
              ``allowed_claims`` is absent (``None``/not a dict).
            - The same rendered section is passed to ``_self_review``, so the
              deterministic-fix and LLM-self-review rewrite passes that may run
              after generation are instructed to preserve existing ``[CLAIM:id]``
              tags rather than silently dropping or corrupting them.
            - When the restrictive no-claims policy is in effect, the prompt's
              "at least one specific number" checklist item is replaced with a
              "no specific numbers" instruction, so the two mandates never
              conflict for a quantitative topic.
            - Expected LLM parse failures (``LLMJsonParseError``, including when
              Strands wraps them in ``EventLoopException``) soft-fail into a JSON
              fallback, then a placeholder if both paths yield no content.
            - Any other exception from the LLM call path propagates unchanged —
              this includes transient transport errors (``LLMRateLimitError`` /
              ``LLMTemporaryError``, left for Temporal to retry) and
              non-transient LLM errors (e.g. ``LLMPermanentError``), not only
              unexpected programming errors.
        Invariants:
            - The agent's configuration, style guide, and brand spec are not mutated.
        """
        self._assert_guidelines_present()
        outline = draft_input.outline_for_prompt().strip()
        outline = compact_text(outline, COMPACT_OUTLINE_CHARS, self._model, "content plan")
        if not outline:
            logger.warning("Empty content plan; returning minimal draft.")
            return WriterOutput(draft="# Draft\n\nAdd a content plan to generate a draft.")

        logger.info(
            "Generating draft: outline len=%s, style_guide len=%s",
            len(outline),
            len(self._writing_style_prompt),
        )

        # Brand spec and writing style guide are delivered once via the cached
        # system-prompt segment (self._writing_system_prompt_with_content, see
        # __init__) rather than embedded here; _assert_guidelines_present()
        # above guarantees both are non-empty, so there is no no-brand case to
        # cover with a fallback line.
        prompt_parts = [
            DRAFT_TASK_INSTRUCTIONS,
        ]
        prompt_parts.extend(
            [
                "",
                "---",
                "CONTENT PLAN (follow narrative flow and section coverage):",
                "---",
                outline,
            ]
        )
        if draft_input.selected_title:
            prompt_parts.append("")
            prompt_parts.append("---")
            prompt_parts.append(
                f"AUTHOR-CHOSEN TITLE (NON-NEGOTIABLE): Use this exact string as the H1 heading at the top of the post — do not rephrase, shorten, or change it:\n{draft_input.selected_title}"
            )
        if draft_input.elicited_stories:
            prompt_parts.append("")
            prompt_parts.append("---")
            prompt_parts.append(
                "AUTHOR'S PERSONAL STORIES (use these in the relevant sections — do not invent new details beyond what is provided):\n"
                + draft_input.elicited_stories
            )
        claims_section = _render_allowed_claims_section(draft_input.allowed_claims)
        if claims_section:
            prompt_parts.append("")
            prompt_parts.append(claims_section)
        if draft_input.audience:
            prompt_parts.append("")
            prompt_parts.append(f"Audience: {draft_input.audience}")
        if draft_input.tone_or_purpose:
            prompt_parts.append(f"Tone/Purpose: {draft_input.tone_or_purpose}")
        # The restrictive no-claims policy forbids every factual/statistical assertion,
        # so the numeric-figure requirement below must not apply when it's in effect —
        # otherwise the model gets two contradictory mandates for a quantitative topic
        # and may invent an unsupported number to satisfy this one. Classified from
        # draft_input.allowed_claims directly (not by comparing claims_section's
        # rendered text) so a future wording change to _NO_ALLOWED_CLAIMS_SECTION
        # can't silently break this branch.
        if _classify_allowed_claims(draft_input.allowed_claims) == _CLAIMS_POLICY_RESTRICTIVE:
            numeric_requirement = (
                "no specific numbers, dollar figures, percentages, or durations (the "
                "ALLOWED CLAIMS section above forbids factual/statistical claims; do "
                "not invent any to satisfy this); "
            )
        else:
            numeric_requirement = (
                "at least one specific number (dollar figure, percentage, or duration) "
                "if the topic supports it; "
            )
        prompt_parts.append("")
        prompt_parts.append("---")
        prompt_parts.append(
            "Before outputting, ensure: no banned phrases; no em dashes or en dashes; 8th grade reading level; "
            "descriptive headings; first-person opening hook from author-provided stories (or placeholder if none "
            "provided, NEVER fabricate); at least one transparent-failure moment from author stories (or placeholder "
            "if none, NEVER fabricate); "
            + numeric_requirement
            + "trade-offs acknowledged; technical concepts "
            "introduced through the pain they solve (not as definitions); one practical next step in the conclusion. "
            "QUALITY CHECK: Does this sound like the author's voice per the brand spec, not an AI? Would a skeptical reader find the "
            "arguments convincing? Is it actionable and valuable to the target audience? Does it flow logically "
            "from intro to conclusion? "
            "FINAL CHECK: scan every 'I' or 'my' sentence, if it describes a specific event not from the "
            "AUTHOR'S PERSONAL STORIES section, replace it with a placeholder."
        )
        if (draft_input.length_guidance or "").strip():
            prompt_parts.append("")
            prompt_parts.append("---")
            prompt_parts.append(draft_input.length_guidance.strip())
        else:
            prompt_parts.append(
                f"TARGET LENGTH: Aim for roughly {draft_input.target_word_count} words "
                f"(acceptable range: {int(draft_input.target_word_count * 0.75)}–{int(draft_input.target_word_count * 1.3)} words). "
                "Hit the intent of the content profile first — do not pad to reach the number, "
                "and do not cut necessary substance to stay under it."
            )
        prompt_parts.append("")
        prompt_parts.append(
            'Use this format: first line {"draft": 0}, then ---DRAFT---, then the full blog post in Markdown.'
        )
        prompt = "\n".join(prompt_parts)

        if on_llm_request:
            on_llm_request("Generating draft...")

        # Use raw-text completion so the model can output the hybrid format (---DRAFT--- then markdown).
        # complete_json() forces a single JSON object, so the model would output only {"draft": 0} and we'd get no content.
        # Soft-fail only on expected LLM parse failures (unwrap Strands EventLoopException);
        # programming bugs (TypeError/ValueError/etc.) propagate.
        draft = ""
        try:
            raw_response = self._call_text(
                prompt, system_prompt=self._writing_system_prompt_with_content
            )
            draft = extract_draft_after_marker(raw_response)
        except Exception as e:
            cause = unwrap_llm_cause(e)
            if not isinstance(cause, LLMJsonParseError):
                raise
            logger.warning(
                "Draft text completion failed: %s; trying JSON fallback.",
                cause,
            )
            try:
                data = self._call_agent_json(
                    prompt, system_prompt=self._writing_system_prompt_with_content
                )
                if isinstance(data, dict):
                    raw_draft = data.get("draft")
                    if isinstance(raw_draft, str) and raw_draft.strip():
                        draft = raw_draft.strip()
            except Exception as e2:
                cause2 = unwrap_llm_cause(e2)
                if not isinstance(cause2, LLMJsonParseError):
                    raise
                logger.warning("JSON draft fallback also failed: %s", cause2)

        if not draft:
            logger.warning("LLM returned no draft content; returning placeholder.")
            draft = _PLACEHOLDER_DRAFT

        logger.info("Draft generated: length=%s", len(draft))
        if draft != _PLACEHOLDER_DRAFT:
            if on_llm_request:
                on_llm_request("Running self-review...")
            draft = self._self_review(draft, claims_section)
        if draft_output_path:
            _write_draft_to_path(draft, draft_output_path)
        return WriterOutput(draft=draft)

    def _format_feedback_item_line(self, item: Any, index: int) -> str:
        """One numbered feedback line (+ optional suggestion) for batch revise prompts.

        Delegates to ``text_parsing.format_feedback_item_line``; see that
        function for the authoritative contract.

        Preconditions:
            ``index`` is a positive int (``bool`` is rejected). ``item`` exposes
            ``severity``, ``category``, and ``issue`` via attribute or duck
            typing; ``location`` and ``suggestion`` are optional.
        Postconditions:
            Returns the numbered feedback line produced by
            ``text_parsing.format_feedback_item_line``.
        Raises:
            ValueError: per ``text_parsing.format_feedback_item_line`` — a
                non-positive/non-int index or a missing required item field.
        """
        return format_feedback_item_line(item, index)

    def revise(
        self,
        revise_input: ReviseWriterInput,
        *,
        on_llm_request: Optional[Callable[[str], None]] = None,
        draft_output_path: Optional[Union[str, Path]] = None,
        work_dir: Optional[Union[str, Path]] = None,
        iteration: Optional[int] = None,
    ) -> WriterOutput:
        """
        Revise a draft by analysing all feedback, creating a structured revision
        plan, then executing the plan in a single pass.

        Steps:
            1. **Analyse** — review all feedback items at once.
            2. **Plan** — produce a ``RevisionPlan`` (summary, ordered changes, risks).
               Persisted in *work_dir* as ``revision_plan_{iteration}.json`` when
               *iteration* is a positive int, otherwise ``revision_plan.json``.
            3. **Execute** — apply the plan to produce the revised draft.
               Persisted as *draft_output_path* (e.g. ``draft_v{iteration}.md``).

        Preconditions:
            - ``revise_input`` is a ``ReviseWriterInput``.
            - Brand and writing guidelines have both been loaded
              (``_assert_guidelines_present``).
        Postconditions:
            - Strips leading/trailing whitespace from ``revise_input.draft`` before
              revision. If the result is empty, returns ``revise_input.draft``
              unchanged (preserves the caller's original whitespace-only text).
            - Otherwise returns a ``WriterOutput`` whose draft is the revised text,
              the stripped draft when feedback is empty, or the stripped draft when
              the batch revise path and JSON fallback both fail to produce a usable
              draft.
            - During batch execute retries, unwrapped ``LLMJsonParseError``
              (including ``EventLoopException`` wrappers) retries without a
              backoff sleep, and unwrapped ``LLMRateLimitError`` /
              ``LLMTemporaryError`` (including wrappers) retry with backoff;
              unexpected exceptions propagate immediately.
            - Transient errors raised by the JSON fallback path (including when
              wrapped in ``EventLoopException``) propagate so Temporal can retry.
            - When ``draft_output_path`` is provided, writes the final draft to
              that path before returning.
        """
        self._assert_guidelines_present()
        original_draft = revise_input.draft or ""
        draft = original_draft.strip()
        if not draft:
            logger.warning("Empty draft in revise; returning as-is.")
            return WriterOutput(draft=original_draft)
        if not revise_input.feedback_items:
            logger.info("No feedback items; returning draft unchanged.")
            return WriterOutput(draft=draft)

        items = list(revise_input.feedback_items)
        num_items = len(items)
        logger.info("Revising draft: %s feedback items (plan-first batch revision)", num_items)

        # ── Step 1+2: Analyse feedback and create structured revision plan ──
        if on_llm_request:
            on_llm_request(f"Analysing {num_items} feedback items and creating revision plan...")
        revision_plan: RevisionPlan = revision.generate_revision_plan(
            draft,
            items,
            revise_input,
            call_json=self._call_agent_json,
            call_text=self._call_text,
            llm=self._model,
        )
        logger.info(
            "Revision plan: %s planned changes, %s risks identified",
            len(revision_plan.changes),
            len(revision_plan.risks),
        )

        # Persist the plan as a JSON artifact so it's visible to the user
        if work_dir is not None:
            plan_name = (
                f"revision_plan_{iteration}.json"
                if iteration is not None and iteration > 0
                else "revision_plan.json"
            )
            try:
                from agents.blogging.shared.artifacts import write_artifact

                write_artifact(work_dir, plan_name, revision_plan.model_dump(mode="json"))
                logger.info("Persisted %s", plan_name)
            except Exception as e:
                logger.warning("Failed to persist revision plan: %s", e)

        # ── Step 3: Execute the plan ────────────────────────────────────────
        if on_llm_request:
            on_llm_request(f"Executing revision plan ({len(revision_plan.changes)} changes)...")
        # Serialise the structured plan for the LLM prompt (list + join, not += in a loop)
        plan_parts: list[str] = [revision_plan.summary]
        if revision_plan.changes:
            plan_parts.append("\n\nPLANNED CHANGES (execute in order):\n")
            for i, ch in enumerate(revision_plan.changes, 1):
                ids = ", ".join(str(fid) for fid in ch.feedback_ids)
                plan_parts.append(f"\n{i}. [{ch.action.upper()}] {ch.section}")
                if ids:
                    plan_parts.append(f"  (feedback #{ids})")
                plan_parts.append(f"\n   {ch.rationale}")
        if revision_plan.risks:
            plan_parts.append(
                "\n\nRISKS TO WATCH:\n" + "\n".join(f"- {r}" for r in revision_plan.risks)
            )
        plan_text = "".join(plan_parts)

        prompt = revision.build_revise_all_items_prompt(
            draft,
            items,
            plan_text,
            revise_input,
            llm=self._model,
            allowed_claims_section=_render_allowed_claims_section(revise_input.allowed_claims),
        )
        current_draft = draft
        primary_succeeded = False
        for attempt in range(BATCH_EXECUTE_MAX_RETRIES):
            try:
                raw_response = self._call_text(
                    prompt, system_prompt=self._writing_system_prompt_with_content
                )
                revised = extract_draft_after_marker(raw_response)
                if revised and revised.strip():
                    current_draft = revised.strip()
                    primary_succeeded = True
                    break
            # The underlying Strands Agent call can surface LLMJsonParseError; retry
            # without the transient backoff sleep.
            except LLMJsonParseError as e:
                logger.warning(
                    "Batch revise failed (attempt %s/%s): %s",
                    attempt + 1,
                    BATCH_EXECUTE_MAX_RETRIES,
                    e,
                )
            except Exception as e:
                cause = unwrap_llm_cause(e)
                if isinstance(cause, LLMJsonParseError):
                    logger.warning(
                        "Batch revise failed (attempt %s/%s): %s",
                        attempt + 1,
                        BATCH_EXECUTE_MAX_RETRIES,
                        cause,
                    )
                    continue
                if isinstance(cause, (LLMRateLimitError, LLMTemporaryError)):
                    logger.warning(
                        "Batch revise transient error (attempt %s/%s); retrying.",
                        attempt + 1,
                        BATCH_EXECUTE_MAX_RETRIES,
                    )
                    time.sleep(BATCH_EXECUTE_BACKOFF_BASE_SECONDS * (2**attempt))
                    continue
                raise
        if not primary_succeeded:
            try:
                fallback = self._fallback_draft_via_json(
                    prompt, system_prompt=self._writing_system_prompt_with_content
                )
                if fallback:
                    current_draft = fallback
            except Exception as e:
                cause = unwrap_llm_cause(e)
                if isinstance(cause, (LLMRateLimitError, LLMTemporaryError)):
                    raise cause
                logger.warning(
                    "Batch revise JSON fallback failed: %s; keeping original draft.", cause
                )

        logger.info(
            "Revision complete: %s items addressed, final length=%s", num_items, len(current_draft)
        )
        if draft_output_path:
            _write_draft_to_path(current_draft, draft_output_path)
        return WriterOutput(draft=current_draft)

    # ------------------------------------------------------------------
    # Interactive draft review: user-as-editor methods
    # ------------------------------------------------------------------

    def identify_uncertainty_questions(
        self,
        draft: str,
        content_plan_text: str,
    ) -> list[UncertaintyQuestion]:
        """Scan a draft for areas of high uncertainty that need user input.

        Returns a list of UncertaintyQuestion objects. An empty list means the
        agent is confident in the draft, the model returned no questions, or an
        expected LLM/parse failure was soft-failed after logging. Transient
        ``LLMRateLimitError`` / ``LLMTemporaryError`` (including when wrapped in
        ``EventLoopException``) propagate so Temporal can retry the draft stage.
        Unexpected programming errors propagate rather than being swallowed.
        """
        prompt = UNCERTAINTY_DETECTION_PROMPT.format(
            content_plan=content_plan_text,
            draft=draft,
        )
        try:
            # NOTE: use ``_call_text`` (not ``_call_agent_json``). The prompt asks
            # for a top-level JSON *array*, but JSON-mode adapters constrain
            # output to a single object, so a JSON-mode call can wrap or empty
            # the array. ``extract_json_array_from_text`` extracts ``[...]``
            # from prose, skipping Markdown links and other non-array ``[``.
            # Deliberately does not carry self._writing_system_prompt_with_content:
            # this call scans a draft for uncertainty, it does not generate brand/
            # style-governed prose, so it uses its own minimal persona instead.
            raw = self._call_text(
                prompt,
                system_prompt="You are a careful writing assistant that identifies areas of genuine uncertainty.",
            )
            cleaned = raw.strip()
            items = extract_json_array_from_text(cleaned, required_keys=("question",))
            if not items:
                return []
            questions = []
            for item in items:
                if not isinstance(item, dict):
                    logger.warning("Skipping non-dict uncertainty question item: %s", item)
                    continue
                try:
                    questions.append(
                        UncertaintyQuestion(
                            question_id=item.get("question_id", f"q-{len(questions)}"),
                            question=item["question"],
                            context=item.get("context", ""),
                            section=item.get("section"),
                        )
                    )
                except (KeyError, TypeError, AttributeError, ValidationError) as e:
                    logger.warning("Skipping malformed uncertainty question: %s", e)
            logger.info("Identified %s uncertainty question(s) in draft", len(questions))
            return questions
        except Exception as e:
            cause = unwrap_llm_cause(e)
            if isinstance(cause, (LLMRateLimitError, LLMTemporaryError)):
                raise cause
            if not isinstance(cause, (LLMError, json.JSONDecodeError, TypeError, ValueError)):
                raise
            logger.warning("Uncertainty detection failed: %s", cause)
            return []

    def analyze_user_feedback_for_guideline_updates(
        self,
        user_feedback: str,
        current_guidelines: str,
    ) -> list[WritingGuidelineUpdate]:
        """Analyze user feedback and extract any writing guideline updates.

        When the user/editor gives feedback about tone, cadence, sound, writing
        patterns, content structure, etc., this method extracts those as
        concrete guideline updates that can be persisted to the writing style guide.

        Returns an empty list when the feedback has no guideline-relevant content,
        the response is malformed / non-dict, or any non-transient ``LLMError``
        (including ``LLMJsonParseError`` and ``LLMPermanentError``) is soft-failed
        with a logged traceback — this is an optional analysis step, and a
        non-transient LLM failure here should not abort the draft stage.
        Transient ``LLMRateLimitError`` / ``LLMTemporaryError`` (including when
        wrapped in ``EventLoopException``) propagate so Temporal can retry the
        draft stage. An unexpected programming error (not an ``LLMError``)
        propagates rather than being swallowed.
        """
        prompt = ANALYZE_USER_FEEDBACK_FOR_GUIDELINES_PROMPT.format(
            user_feedback=user_feedback,
            current_guidelines=current_guidelines,
        )
        try:
            # Deliberately omits system_prompt (falls back to bare WRITING_SYSTEM_PROMPT):
            # this call extracts guideline-update suggestions from user feedback, it does
            # not generate blog prose, so the brand/style segment is not relevant here.
            data = self._call_agent_json(prompt)
            if not isinstance(data, dict):
                return []
            if not data.get("has_guideline_updates"):
                logger.info("User feedback contains no guideline updates")
                return []
            updates = []
            updates_data = data.get("updates", [])
            if not isinstance(updates_data, list):
                logger.warning(
                    "Expected 'updates' to be a list, got %s; returning no guideline updates",
                    type(updates_data).__name__,
                )
                return []
            for item in updates_data:
                try:
                    updates.append(
                        WritingGuidelineUpdate(
                            category=item["category"],
                            description=item["description"],
                            guideline_text=item["guideline_text"],
                        )
                    )
                except (KeyError, TypeError, ValidationError) as e:
                    logger.warning("Skipping malformed guideline update: %s", e)
            logger.info("Extracted %s writing guideline update(s) from user feedback", len(updates))
            return updates
        except Exception as e:
            cause = unwrap_llm_cause(e)
            if isinstance(cause, (LLMRateLimitError, LLMTemporaryError)):
                raise cause
            if not isinstance(cause, LLMError):
                raise
            logger.exception("Guideline update analysis failed: %s", cause)
            return []

    def revise_from_user_feedback(
        self,
        draft: str,
        user_feedback: str,
        content_plan_text: str,
        *,
        audience: Optional[str] = None,
        tone_or_purpose: Optional[str] = None,
        selected_title: Optional[str] = None,
        elicited_stories: Optional[str] = None,
        target_word_count: int = 1000,
        length_guidance: str = "",
        uncertainty_answers: Optional[dict[str, str]] = None,
        allowed_claims: Optional[dict[str, Any]] = None,
        on_llm_request: Optional[Callable[[str], None]] = None,
        draft_output_path: Optional[Union[str, Path]] = None,
    ) -> WriterOutput:
        """Revise a draft based on direct user/editor feedback.

        Unlike ``revise()`` which handles structured copy-editor feedback items,
        this method handles free-form user feedback from the interactive review
        cycle where the user acts as the editor.

        Postconditions:
            - Brand-spec and writing-guideline content is delivered via the
              system prompt (``self._writing_system_prompt_with_content``) on
              both the primary text path and the JSON fallback path, not
              embedded in the user prompt.
            - The prompt includes an ALLOWED CLAIMS section (per
              ``_render_allowed_claims_section``) when ``allowed_claims`` yields
              a non-empty rendered section, so `[CLAIM:id]` tags survive this
              revision path too; omitted otherwise.
            - Returns a ``WriterOutput`` whose ``draft`` field is the original
              ``draft`` unchanged when it is blank.
            - Otherwise retries the text-completion path up to
              ``BATCH_EXECUTE_MAX_RETRIES`` times: an unwrapped
              ``LLMJsonParseError`` (including ``EventLoopException`` wrappers)
              retries without a backoff sleep, and an unwrapped
              ``LLMRateLimitError`` / ``LLMTemporaryError`` (including wrappers)
              retries with backoff. If all attempts fail, falls back to
              ``_fallback_draft_via_json``; if both paths fail to produce a
              usable draft, returns the original ``draft`` unchanged.
            - Transient errors raised by the fallback path propagate so the
              caller (e.g. Temporal) can retry.
            - Unexpected non-LLM programming errors propagate.
            - When ``draft_output_path`` is provided, writes the final draft to
              that path before returning.
        """
        self._assert_guidelines_present()
        if not draft.strip():
            return WriterOutput(draft=draft)

        prompt_parts = [
            USER_FEEDBACK_REVISION_INSTRUCTIONS.replace("{user_feedback}", user_feedback),
            "",
            "---",
            "CONTENT PLAN:",
            "---",
            content_plan_text,
            "",
        ]

        if uncertainty_answers:
            answer_lines = []
            for qid, answer in uncertainty_answers.items():
                answer_lines.append(f"- {qid}: {answer}")
            prompt_parts.extend(
                [
                    "---",
                    "ANSWERS TO PREVIOUSLY ASKED QUESTIONS (incorporate these into the revision):",
                    "---",
                    "\n".join(answer_lines),
                    "",
                ]
            )

        if selected_title:
            prompt_parts.extend(
                ["---", f"AUTHOR-CHOSEN TITLE (preserve this exact H1): {selected_title}", ""]
            )
        if elicited_stories:
            prompt_parts.extend(["---", "AUTHOR'S PERSONAL STORIES:\n" + elicited_stories, ""])
        claims_section = _render_allowed_claims_section(allowed_claims)
        if claims_section:
            prompt_parts.extend([claims_section, ""])
        if audience:
            prompt_parts.append(f"Audience: {audience}")
        if tone_or_purpose:
            prompt_parts.append(f"Tone/Purpose: {tone_or_purpose}")

        length_block = (
            length_guidance.strip()
            if length_guidance.strip()
            else (
                f"TARGET LENGTH: Aim for roughly {target_word_count} words "
                f"(acceptable range: {int(target_word_count * 0.75)}–{int(target_word_count * 1.3)} words)."
            )
        )
        prompt_parts.extend(
            [
                "",
                "---",
                "CURRENT DRAFT:",
                "---",
                draft,
                "",
                "---",
                length_block,
                "",
                "---",
                'Use this format: first line {"draft": 0}, then ---DRAFT---, then the full revised blog post in Markdown.',
            ]
        )
        prompt = "\n".join(prompt_parts)

        if on_llm_request:
            on_llm_request("Revising draft based on editor feedback...")

        current_draft = draft
        primary_succeeded = False
        for attempt in range(BATCH_EXECUTE_MAX_RETRIES):
            try:
                raw_response = self._call_text(
                    prompt, system_prompt=self._writing_system_prompt_with_content
                )
                revised = extract_draft_after_marker(raw_response)
                if revised and revised.strip():
                    current_draft = revised.strip()
                    primary_succeeded = True
                    break
            # The underlying Strands Agent call can surface LLMJsonParseError; retry
            # without the transient backoff sleep (test_revise_from_user_feedback_json_parse_error_skips_sleep).
            except LLMJsonParseError as e:
                logger.warning(
                    "User-feedback revision failed (attempt %s/%s): %s",
                    attempt + 1,
                    BATCH_EXECUTE_MAX_RETRIES,
                    e,
                )
            except Exception as e:
                cause = unwrap_llm_cause(e)
                if isinstance(cause, LLMJsonParseError):
                    logger.warning(
                        "User-feedback revision failed (attempt %s/%s): %s",
                        attempt + 1,
                        BATCH_EXECUTE_MAX_RETRIES,
                        cause,
                    )
                    continue
                if isinstance(cause, (LLMRateLimitError, LLMTemporaryError)):
                    logger.warning(
                        "User-feedback revision transient error (attempt %s/%s); retrying.",
                        attempt + 1,
                        BATCH_EXECUTE_MAX_RETRIES,
                    )
                    time.sleep(BATCH_EXECUTE_BACKOFF_BASE_SECONDS * (2**attempt))
                    continue
                raise

        if not primary_succeeded:
            try:
                fallback = self._fallback_draft_via_json(
                    prompt, system_prompt=self._writing_system_prompt_with_content
                )
                if fallback:
                    current_draft = fallback
            except Exception as e:
                cause = unwrap_llm_cause(e)
                if isinstance(cause, (LLMRateLimitError, LLMTemporaryError)):
                    raise cause
                if not isinstance(cause, LLMError):
                    raise
                logger.warning(
                    "User-feedback JSON fallback failed after retries; keeping original draft: %s",
                    cause,
                )

        logger.info("User-feedback revision complete, final length=%s", len(current_draft))
        if draft_output_path:
            _write_draft_to_path(current_draft, draft_output_path)
        return WriterOutput(draft=current_draft)

    def generate_escalation_summary(
        self,
        revision_count: int,
        latest_feedback_items: list[FeedbackItem],
        persistent_issues: list[PersistentFeedbackItem],
    ) -> str:
        """Generate a human-readable summary when the copy-edit loop hits the escalation threshold.

        Called when the automated editor has gone through ``revision_count`` iterations
        without approving the draft, to produce a clear explanation for the user about
        what is stuck and what guidance is needed.

        Transient ``LLMRateLimitError`` / ``LLMTemporaryError`` (including when wrapped
        in ``EventLoopException``) propagate so Temporal can retry. Other LLM failures
        fall back to a generic summary string.
        """
        feedback_text = "\n".join(
            f"- [{item.severity}] {item.category}: {item.issue}" for item in latest_feedback_items
        )
        persistent_text = (
            "\n".join(
                f"- [{item.severity}] {item.category} "
                f"(flagged {item.occurrence_count} times): {item.issue}"
                for item in persistent_issues
            )
            if persistent_issues
            else "None"
        )

        prompt = ESCALATION_SUMMARY_PROMPT.format(
            revision_count=revision_count,
            latest_feedback=feedback_text or "No specific feedback items.",
            persistent_issues=persistent_text,
        )
        try:
            # Deliberately omits system_prompt (falls back to bare WRITING_SYSTEM_PROMPT):
            # this call summarizes the copy-edit loop's status for the user, it does not
            # generate blog prose, so the brand/style segment is not relevant here.
            summary = self._call_text(prompt)
            return (summary or "").strip()
        except Exception as e:
            cause = unwrap_llm_cause(e)
            if isinstance(cause, (LLMRateLimitError, LLMTemporaryError)):
                raise cause
            if not isinstance(cause, LLMError):
                raise
            logger.warning("Escalation summary generation failed: %s", cause)
            return (
                f"The draft has been through {revision_count} automated revision cycles "
                "without reaching approval. Please review the current draft and provide feedback."
            )
