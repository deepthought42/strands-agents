# Blogging Team — System Design

This document covers the detailed design decisions, data models, persistence patterns, state management, and infrastructure choices that underpin the blogging agent suite.

---

## 1. Core Data Models

The blogging pipeline uses Pydantic `BaseModel` classes for all inter-agent communication. Models enforce validation at system boundaries and serialize cleanly to JSON for artifact persistence.

### 1.1 Content Planning Models

```mermaid
classDiagram
    class ContentPlan {
        +str overarching_topic
        +str narrative_flow
        +str opening_strategy
        +str conclusion_guidance
        +str target_reader
        +List~ContentPlanSection~ sections
        +List~TitleCandidate~ title_candidates
        +RequirementsAnalysis requirements_analysis
        +int plan_version
    }

    class ContentPlanSection {
        +str title
        +str coverage_description
        +List~str~ key_points
        +List~str~ what_to_avoid
        +str reader_takeaway
        +str strongest_point
        +str story_opportunity
        +str opening_hook
        +str transition_to_next
        +int order
        +str research_support_note
        +bool gap_flag
    }

    class TitleCandidate {
        +str title
        +float probability_of_success
        +TitleScoring scoring
    }

    class TitleScoring {
        +float curiosity_gap
        +float specificity
        +float audience_fit
        +float seo_potential
        +float emotional_pull
        +str rationale
    }

    class RequirementsAnalysis {
        +bool plan_acceptable
        +bool scope_feasible
        +List~str~ research_gaps
        +bool fits_profile
        +List~str~ gaps
        +List~str~ risks
        +str suggested_format_change
    }

    class PlanningPhaseResult {
        +ContentPlan content_plan
        +int planning_iterations_used
        +int parse_retry_count
        +float planning_wall_ms_total
        +PlanningFailureReason planning_failure_reason
    }

    class PlanningInput {
        +str brief
        +str audience
        +str tone_or_purpose
        +str research_digest
        +str length_policy_context
        +str series_context_block
    }

    ContentPlan "1" --> "*" ContentPlanSection : sections
    ContentPlan "1" --> "*" TitleCandidate : title_candidates
    ContentPlan "1" --> "1" RequirementsAnalysis : requirements_analysis
    TitleCandidate "1" --> "0..1" TitleScoring : scoring
    PlanningPhaseResult "1" --> "1" ContentPlan : content_plan
```

### 1.2 Writing & Editing Models

```mermaid
classDiagram
    class WriterInput {
        +ContentPlan content_plan
        +str audience
        +str tone_or_purpose
        +int target_word_count
        +str length_guidance
        +str selected_title
        +List~str~ elicited_stories
    }

    class WriterOutput {
        +str draft
    }

    class ReviseWriterInput {
        +str draft
        +List~FeedbackItem~ feedback_items
        +str feedback_summary
        +List~FeedbackItem~ previous_feedback_items
        +List~str~ persistent_issues
        +ContentPlan content_plan
        +str audience
        +str tone_or_purpose
        +int target_word_count
    }

    class CopyEditorInput {
        +str draft
        +str audience
        +str tone_or_purpose
        +str human_feedback
        +List~FeedbackItem~ previous_feedback_items
        +int target_word_count
        +str length_guidance
        +int soft_min_words
        +int soft_max_words
    }

    class CopyEditorOutput {
        +bool approved
        +str summary
        +List~FeedbackItem~ feedback_items
    }

    class FeedbackItem {
        +str category
        +str severity
        +str location
        +str issue
        +str suggestion
    }

    CopyEditorOutput "1" --> "*" FeedbackItem : feedback_items
    CopyEditorInput "1" --> "*" FeedbackItem : previous_feedback_items
    ReviseWriterInput "1" --> "*" FeedbackItem : feedback_items
```

**FeedbackItem categories**: `voice`, `style`, `clarity`, `structure`, `flow`, `engagement`, `technical`, `formatting`, `authenticity`, `length`

**FeedbackItem severities**: `must_fix`, `should_fix`, `consider`

### 1.3 Quality Gate Models

```mermaid
classDiagram
    class ValidatorReport {
        +str status
        +List~CheckResult~ checks
    }

    class CheckResult {
        +str name
        +str status
        +dict details
    }

    class ComplianceReport {
        +str status
        +List~Violation~ violations
        +List~str~ required_fixes
        +str notes
    }

    class Violation {
        +str rule_id
        +str description
        +List~str~ evidence_quotes
        +str location_hint
    }

    class FactCheckReport {
        +str claims_status
        +str risk_status
        +List~str~ claims_verified
        +List~str~ risk_flags
        +List~str~ required_disclaimers
        +str notes
    }

    ValidatorReport "1" --> "*" CheckResult : checks
    ComplianceReport "1" --> "*" Violation : violations
```

**Gate status values**: `PASS` or `FAIL`. All three gates must report `PASS` for the draft to proceed to publication.

### 1.4 Publication Models

```mermaid
classDiagram
    class PublicationSubmission {
        +str submission_id
        +str slug
        +str file_path
        +str state
        +str message
    }

    class ApprovalResult {
        +str submission_id
        +str folder_path
        +str draft_path
        +str medium_path
        +str devto_path
        +str substack_path
        +str message
    }

    class PublishingPack {
        +List~str~ title_options
        +str meta_description
        +str header_polish
        +List~str~ internal_links
        +str snippet_copy
        +List~str~ tags
    }

    class PublicationMetadata {
        +str submission_id
        +str slug
        +str title
        +str draft_content
        +str audience
        +str tone_or_purpose
        +List~str~ tags
        +str state
        +str rejection_feedback
        +datetime created_at
        +datetime approved_at
    }

    PublicationSubmission ..> ApprovalResult : on approve
```

**Publication states**: `awaiting_approval` → `approved` or `collecting_rejection_feedback`

---

## 2. Error Hierarchy

All pipeline exceptions inherit from `BloggingError`, enabling consistent error handling at the orchestrator level. Each exception carries contextual metadata for debugging and job status updates.

```mermaid
classDiagram
    class BloggingError {
        +str message
        +str phase
        +Exception cause
    }

    class ResearchError {
        +int sources_found
    }

    class PlanningError {
        +str failure_reason
    }

    class DraftError {
        +int iteration
    }

    class CopyEditError {
        +int iteration
    }

    class ComplianceError {
        +int violation_count
    }

    class FactCheckError {
        +int unverified_claims
        +int high_risk_count
    }

    class ValidationError {
        +list failed_checks
    }

    class PublicationError

    BloggingError <|-- ResearchError
    BloggingError <|-- PlanningError
    BloggingError <|-- DraftError
    BloggingError <|-- CopyEditError
    BloggingError <|-- ComplianceError
    BloggingError <|-- FactCheckError
    BloggingError <|-- ValidationError
    BloggingError <|-- PublicationError
```

LLM-specific errors (`LLMError`, `LLMRateLimitError`, `LLMTemporaryError`, `LLMUnreachableAfterRetriesError`, `LLMJsonParseError`) are raised and caught via `llm_service.interface`, not this hierarchy.

**Design decision**: Each error includes a `phase` field so the orchestrator can update the job store with `failed_phase` without parsing the exception type. `PlanningError` carries a `failure_reason` enum (`MAX_ITERATIONS_REACHED`, `INFEASIBLE_SCOPE`, `PARSE_FAILURE`, `MODEL_ABORT`) for API consumers.

---

## 3. Job State Machine

### 3.1 Job Lifecycle States

```mermaid
stateDiagram-v2
    [*] --> PENDING : POST /full-pipeline-async
    PENDING --> RUNNING : Pipeline starts

    RUNNING --> COMPLETED : All gates PASS
    RUNNING --> NEEDS_REVIEW : Gates FAIL after max rewrites
    RUNNING --> FAILED : Unrecoverable error
    RUNNING --> CANCELLED : Temporal CancelledError

    COMPLETED --> [*]
    NEEDS_REVIEW --> [*]
    FAILED --> PENDING : POST /restart/{id}
    CANCELLED --> PENDING : POST /restart/{id}
```

### 3.2 Pipeline Phase Transitions

```mermaid
stateDiagram-v2
    [*] --> PLANNING
    PLANNING --> TITLE_SELECTION : Outline approved
    PLANNING --> FAILED_PLANNING : Max iterations / parse failure

    TITLE_SELECTION --> DRAFT_INITIAL : Title selected (or skipped)

    DRAFT_INITIAL --> DRAFT_REVIEW : Draft v1 generated
    DRAFT_INITIAL --> DRAFT_REVIEW : Story placeholders filled

    DRAFT_REVIEW --> COPY_EDIT_LOOP : User approves or skips
    DRAFT_REVIEW --> DRAFT_REVIEW : User provides feedback

    COPY_EDIT_LOOP --> FACT_CHECK : Editor approves or loop exhausted

    FACT_CHECK --> COMPLIANCE : Validators + fact check complete
    COMPLIANCE --> FINALIZE : All gates PASS
    COMPLIANCE --> REWRITE_LOOP : Any gate FAIL

    REWRITE_LOOP --> FACT_CHECK : Revised draft ready
    REWRITE_LOOP --> FINALIZE : Max rewrite iterations (NEEDS_HUMAN_REVIEW)

    FINALIZE --> [*] : Publishing pack written

    state DRAFT_REVIEW {
        [*] --> WaitingForAnswers : Uncertainty questions detected
        WaitingForAnswers --> WaitingForFeedback : Answers submitted
        WaitingForFeedback --> RevisionLoop : Feedback received
        RevisionLoop --> WaitingForFeedback : Revision complete
        WaitingForFeedback --> [*] : Approved
    }
```

### 3.3 Progress Tracking

Each phase maps to a progress range from `PHASE_PROGRESS_RANGES` in `shared/models.py`. The `get_phase_progress(phase, sub_progress)` function computes the overall percentage:

| `BlogPhase` | Enum value | Range | Calculation |
|-------------|-----------|-------|-------------|
| `PLANNING` | `planning` | 0–12% | `0 + 12 * sub_progress` |
| `TITLE_SELECTION` | `title_selection` | 12–15% | `12 + 3 * sub_progress` |
| `DRAFT_INITIAL` | `draft_initial` | 15–30% | `15 + 15 * sub_progress` |
| `DRAFT_REVIEW` | `draft_review` | 30–45% | `30 + 15 * sub_progress` |
| `COPY_EDIT_LOOP` | `copy_edit` | 45–60% | `45 + 15 * sub_progress` |
| `FACT_CHECK` | `fact_check` | 60–70% | `60 + 10 * sub_progress` |
| `COMPLIANCE` | `compliance` | 70–82% | `70 + 12 * sub_progress` |
| `REWRITE_LOOP` | `rewrite` | 82–96% | `82 + 14 * sub_progress` |
| `FINALIZE` | `finalize` | 96–100% | `96 + 4 * sub_progress` |

Note that the two loop phases have shorter enum values (`copy_edit`, `rewrite`) than their enum names suggest.

---

## 4. Artifact Persistence

All pipeline outputs are written to `work_dir/{job_id}/` as versioned artifacts. The canonical filenames come from `ARTIFACT_NAMES` in `shared/artifacts.py:18-34`, and per-artifact producer metadata lives in `ARTIFACT_PRODUCER` (same file). The `write_artifact()` / `read_artifact()` helpers auto-serialize JSON for `.json` files.

| Artifact | `producer_phase` | `producer_agent` | Format | Purpose |
|----------|-----------------|------------------|--------|---------|
| `brand_spec_prompt.md` | `draft_initial` | Pipeline (brand load) | Markdown | Brand and style rules (single source of truth) |
| `research_packet.md` | `research` | BlogResearchAgent | Markdown | Compiled research document, written by `run_planning()` ahead of content planning and passed through `build_research_digest()` into `PlanningInput.research_digest` with a hard cap derived from the smallest planner, critic, or failover-provider context; runs with no web or academic sources retain the packet but supply an empty digest |
| `allowed_claims.json` | `planning` | BlogPlanningAgent | JSON | Evidence-backed claims for the fact-check gate / validators (`read_artifact`'d by `BlogFactCheckAgent`/`run_validators_from_work_dir`). **Interim producer**: the module docstring in `blog_research_agent/allowed_claims.py` designates `BlogResearchAgent` as the intended producer, but extraction isn't yet wired to the research call above — planning derives claims from its own content plan with no reference list instead — see Architecture.md Note 4 |
| `content_plan.json` | `planning` | BlogPlanningAgent | JSON | Structured plan (machine-readable) |
| `content_plan.md` | `planning` | BlogPlanningAgent | Markdown | Human-readable plan with analysis |
| `content_brief.md` | `planning` | BlogPlanningAgent | Markdown | Title choices + outline |
| `outline.md` | `planning` | BlogPlanningAgent | Markdown | Flat outline (display / compatibility) |
| `draft_v1.md` | `draft_initial` | BlogWriterAgent | Markdown | First draft |
| `draft_v2.md` | `copy_edit` | BlogCopyEditorAgent | Markdown | Revised draft after copy editing |
| `final.md` | `finalize` | BlogCopyEditorAgent | Markdown | Approved final draft |
| `editor_feedback.json` | `copy_edit` | BlogCopyEditorAgent | JSON | Feedback items from the editor |
| `validator_report.json` | `compliance` | Validators | JSON | Deterministic check results |
| `compliance_report.json` | `compliance` | BlogComplianceAgent | JSON | Brand/style violations |
| `fact_check_report.json` | `fact_check` | BlogFactCheckAgent | JSON | Claims and risk assessment |
| `publishing_pack.json` | `finalize` | Pipeline | JSON | Title options, meta, tags, platform versions |
| `medium_stats_report.json` | `medium_stats` | BlogMediumStatsAgent | JSON | Medium.com dashboard stats |

**Planning attribution:** the `producer_agent` metadata still names `BlogPlanningAgent` for the four planning artifacts because that is the logical role. In the current v2 wiring, the code path that produces the `ContentPlan` is `BlogWriterAgent.plan_content()` (see `blog_writer_agent/agent.py:294`), which implements the same refine-until-done contract.

---

## 5. Content Profile System

Content profiles provide guideline-based length and structure targets, replacing manual word count guessing.

### 5.1 Profile Presets

| Profile | Target Words | Soft Min | Soft Max | Sections Min | Sections Max |
|---------|-------------|----------|----------|-------------|-------------|
| `short_listicle` | 750 | 500 | 1,100 | 3 | 7 |
| `standard_article` | 1,000 | 750 | 1,300 | 4 | 10 |
| `technical_deep_dive` | 2,200 | 1,500 | 3,200 | 6 | 14 |
| `series_instalment` | 1,400 | 950 | 2,000 | 4 | 10 |

### 5.2 Length Policy Resolution

```mermaid
flowchart TD
    A[Request arrives] --> B{target_word_count<br/>provided?}
    B -->|Yes| C[Use explicit target<br/>clamp 100-10,000]
    B -->|No| D{content_profile<br/>provided?}
    D -->|Yes| E[Use profile preset target]
    D -->|No| F[Default: standard_article<br/>1,000 words]

    C --> G[Scale soft bands<br/>from target]
    E --> G
    F --> G

    G --> H[Build LengthPolicy]
    H --> I["LengthPolicy:<br/>target, soft_min, soft_max,<br/>guidance text, editor ratios"]
```

**Design decision**: The profile still influences editor strictness ratios even when `target_word_count` overrides the numeric target. Tighter over-length checks apply for deep dives; looser for listicles.

### 5.3 Series Context

For multi-part series, `SeriesContext` scopes the outline and draft to a single instalment:

- `series_title`: Overall series name
- `part_number`: Current instalment (1-based)
- `planned_parts`: Total planned parts
- `instalment_scope`: What this specific part covers

---

## 6. Author Profile Architecture

The author profile system personalizes all generated content with the author's identity, voice, and background.

### 6.1 Profile Model

```mermaid
classDiagram
    class AuthorProfile {
        +Identity identity
        +Professional professional
        +Social social
        +Voice voice
        +Background background
        +dict extra
        +author_name() str
        +from_yaml_file(path) AuthorProfile
    }

    class Identity {
        +str full_name
        +str short_name
        +str pronouns
        +str tagline
    }

    class Professional {
        +str current_title
        +str current_employer
        +List~str~ past_employers
        +List~str~ founded_companies
        +List~str~ awards
    }

    class Social {
        +str medium
        +str linkedin
        +str github
        +str twitter
        +str website
        +dict other
    }

    class Voice {
        +str archetype
        +List~str~ tone_words
        +List~str~ signature_phrases
        +List~str~ banned_phrases
        +List~str~ influences
        +str style_notes
    }

    class Background {
        +str bio
        +str origin_story
        +List~str~ expertise
        +List~str~ audiences
        +List~str~ notable_projects
    }

    AuthorProfile --> Identity
    AuthorProfile --> Professional
    AuthorProfile --> Social
    AuthorProfile --> Voice
    AuthorProfile --> Background
```

### 6.2 Profile Resolution Chain

1. `$AUTHOR_PROFILE_PATH` env var (explicit path)
2. `$AGENT_CACHE/author_profile.yaml` (convention-based)
3. Bundled `author_profile.example.yaml` (fallback with warning)
4. Raises error if `AUTHOR_PROFILE_STRICT=true` and no profile found

Profiles are cached by `(resolved_path, mtime_ns)` to avoid re-parsing on every request.

### 6.3 Template Rendering

Writing guidelines (`docs/writing_guidelines.md`) and brand spec (`docs/brand_spec_prompt.md`) are **Jinja2 templates** rendered against the `AuthorProfile` at runtime using `StrictUndefined` so missing fields fail loudly.

---

## 7. LLM Integration Patterns

### 7.1 Client Architecture

```mermaid
flowchart TD
    A["Agent requests LLM"] --> B["get_client(agent_key)"]
    B --> C{Model resolution}
    C --> D["LLM_MODEL_{agent_key}"]
    C --> E["LLM_MODEL"]
    C --> F["AGENT_DEFAULT_MODELS[agent_key]"]
    C --> G["Fallback default"]
    D --> H["OllamaLLMClient(model, base_url, timeout)"]
    E --> H
    F --> H
    G --> H
    H --> I{Method}
    I --> J["complete_json()<br/>Structured JSON output"]
    I --> K["complete()<br/>Raw text output"]
    I --> L["chat_json_round()<br/>Multi-turn with tools"]
```

**Design decision**: Clients are cached by `(model, base_url, timeout)` tuple for thread safety. The factory returns a `DummyLLMClient` in test environments.

### 7.2 Error Recovery

- **JSON parse failures**: Up to `BLOG_PLANNING_MAX_PARSE_RETRIES` (default 3) attempts with fallback to `complete()` + manual `parse_json_object()`
- **Rate limits (429)**: Raised as `LLMRateLimitError` after retry exhaustion
- **Server errors (5xx)**: Raised as `LLMTemporaryError`
- **Compliance fallback**: Up to 3 LLM rounds; on persistent parse failure returns a safe `FAIL` report

### 7.3 Planning Model Override

The `BLOG_PLANNING_MODEL` env var allows using a different model specifically for planning (e.g., a larger model for better content plans) while the rest of the pipeline uses the default `LLM_MODEL`.

---

## 8. Postgres Schema

### 8.1 Story Bank Table

Defined in `postgres/__init__.py` as a `TeamSchema` constant and registered at FastAPI lifespan startup via `register_team_schemas(SCHEMA)`:

```sql
CREATE TABLE IF NOT EXISTS blogging_stories (
    id              TEXT PRIMARY KEY,
    narrative       TEXT NOT NULL,
    section_title   TEXT NOT NULL DEFAULT '',
    section_context TEXT NOT NULL DEFAULT '',
    keywords        JSONB NOT NULL DEFAULT '[]'::jsonb,
    summary         TEXT NOT NULL DEFAULT '',
    source_job_id   TEXT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_blogging_stories_source_job
    ON blogging_stories(source_job_id);
```

**Design decisions**:
- Stories are persisted for cross-post reuse (the ghost writer agent elicits stories once, uses them across future posts)
- `keywords` as JSONB enables flexible search without schema changes
- `find_relevant_stories()` uses two-stage retrieval: keyword overlap SQL query, then LLM reranking
- All queries instrumented via `@timed_query(store="blogging_story_bank")`

### 8.2 Schema Registration Pattern

The blogging team follows **Pattern B**: a pure-data `SCHEMA: TeamSchema` constant is exported from `blogging/postgres/__init__.py`, and the FastAPI lifespan calls `register_team_schemas(SCHEMA)` at startup. The schema is a no-op when `POSTGRES_HOST` is unset.

---

## 9. Real-time Communication (SSE Event Bus)

### 9.1 Architecture

```mermaid
sequenceDiagram
    participant UI as Angular UI
    participant API as Blogging API
    participant Bus as Job Event Bus
    participant Pipeline as Pipeline Orchestrator

    UI->>API: GET /job/{job_id}/stream
    API->>Bus: subscribe(job_id)
    Bus-->>API: Subscription (Event + deque)

    Pipeline->>Bus: publish(job_id, {phase, progress, status_text})
    Bus-->>API: notify event
    API-->>UI: SSE data: {"phase": "planning", "progress": 8}

    Pipeline->>Bus: publish(job_id, {phase: "finalize", progress: 100})
    Bus-->>API: notify terminal event
    API-->>UI: SSE data: {"phase": "finalize", "progress": 100}
    Bus->>Bus: cleanup_job(job_id)
```

### 9.2 Design Decisions

- **Thread-safe**: Uses `threading.Event` for notifications and `deque(maxlen=500)` for event buffering
- **Per-job isolation**: Each job_id has independent subscribers; no cross-job interference
- **Automatic cleanup**: `cleanup_job()` wakes all subscribers on terminal events and removes the job
- **Integration**: The `job_updater()` wrapper in `run_pipeline_job.py` publishes to SSE on every job store update, merging update kwargs with timestamp

---

## 10. Deterministic Validators

The validator system runs rule-based checks without LLM calls, providing fast feedback before the more expensive compliance and fact-check gates.

| Check | What It Detects | Configuration |
|-------|-----------------|---------------|
| `banned_phrases` | Cliches ("In today's fast-paced world", "Furthermore", "In conclusion", etc.) | Case-insensitive match against 20+ phrases |
| `banned_patterns` | Vague citations ("Studies show", "Experts agree"), em-dash overuse | Regex patterns |
| `paragraph_length` | Too-short or too-long paragraphs | Min/max sentences per paragraph |
| `reading_level` | Grade level outside target range | Flesch-Kincaid via readability library |
| `required_sections` | Missing required markdown headings | Configurable heading list |
| `claims_policy` | Missing `[CLAIM:id]` tags on factual claims | When claims tagging is required |

**Design decision**: Validators run before LLM-based gates to catch mechanical issues cheaply. Their report is passed to the Compliance Agent as additional context.
