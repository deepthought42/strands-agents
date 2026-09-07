# Blogging Team — Use Cases

This document describes the actors, primary use cases, and detailed interaction sequences for the blogging agent suite.

---

## 1. Actors

| Actor | Type | Description |
|-------|------|-------------|
| **Human Author** | Primary | Content creator who initiates pipelines, provides feedback, selects titles, tells stories, and approves publications |
| **Angular UI** | System | Frontend client that renders progress, collects user inputs, and streams SSE events |
| **API Client** | System | Any HTTP client (CLI, script, integration) calling the REST API directly |
| **LLM Service** | Internal | Ollama/Claude inference backend used by all LLM-powered agents |
| **Temporal Server** | Internal | Durable workflow engine for long-running pipeline execution |
| **PostgreSQL** | Internal | Persistent store for job state, story bank, and integration credentials |
| **Medium.com** | External | Publishing platform; stats scraped via Playwright |
| **arXiv** | External | Academic paper repository searched during research |

---

## 2. Primary Use Cases

```mermaid
graph TD
    Author["Human Author"]
    UI["Angular UI"]
    APIClient["API Client"]

    subgraph "Blogging Team Use Cases"
        UC1["Run Full Pipeline"]
        UC2["Review Draft & Provide Feedback"]
        UC3["Select Title"]
        UC4["Provide Story Input"]
        UC5["Answer Agent Questions"]
        UC6["Approve / Unapprove Publication"]
        UC7["Collect Medium Stats"]
        UC8["Monitor Pipeline Progress"]
        UC9["Browse / Reuse Story Bank"]
        UC10["Restart / Resume / Cancel Job"]
    end

    Author --> UC1
    Author --> UC2
    Author --> UC3
    Author --> UC4
    Author --> UC5
    Author --> UC6
    Author --> UC7
    Author --> UC9
    Author --> UC10

    UI --> UC8
    APIClient --> UC1
    APIClient --> UC7
    APIClient --> UC9

    UC1 -->|includes| UC2
    UC1 -->|includes| UC3
    UC1 -->|includes| UC4
    UC1 -->|includes| UC5
    UC1 -->|includes| UC6
```

---

## 3. Use Case: Full Pipeline Execution

The primary use case — an author requests a complete blog post from brief to publishing-ready output.

### 3.1 Sequence Diagram

```mermaid
sequenceDiagram
    actor Author
    participant UI as Angular UI
    participant API as Blogging API
    participant JS as Job Store
    participant Pipeline as Pipeline Orchestrator
    participant RA as Research Agent
    participant WA as Writer Agent
    participant GW as Ghost Writer
    participant CE as Copy Editor
    participant Val as Validators
    participant FC as Fact Check Agent
    participant CA as Compliance Agent
    Author->>UI: Submit brief, audience, tone, profile
    UI->>API: POST /full-pipeline-async {brief, audience, ...}
    API->>JS: create_blog_job(job_id)
    API-->>UI: {job_id}
    UI->>API: GET /job/{job_id}/stream (SSE)

    Note over Pipeline: Phase 1: PLANNING (0-12%)
    API->>Pipeline: run_blog_full_pipeline_job()
    Pipeline->>RA: run(ResearchBriefInput)
    RA-->>Pipeline: ResearchAgentOutput<br/>(compiled_document, references, notes)
    Note over Pipeline: run_planning() writes research_packet.md<br/>and builds a bounded research digest
    Pipeline->>WA: plan_content(PlanningInput)
    Note over WA: Planning and plan critic receive<br/>PlanningInput.research_digest

    loop Refine until acceptable (max 5 iterations)
        WA->>WA: Generate / refine ContentPlan
        WA->>WA: Check requirements_analysis<br/>(plan_acceptable AND scope_feasible)
    end

    WA-->>Pipeline: PlanningPhaseResult
    Pipeline-->>UI: SSE {phase: planning, progress: 12}

    Note over Pipeline: Phase 2: TITLE_SELECTION (12-15%)
    Pipeline-->>UI: SSE {waiting_for_title_selection, title_choices}
    Author->>UI: Select preferred title
    UI->>API: POST /job/{id}/select-title
    Pipeline-->>UI: SSE {phase: title_selection, progress: 15}

    Note over Pipeline: Phase 3: DRAFT_INITIAL (15-30%)
    Pipeline->>WA: run(WriterInput)
    WA-->>Pipeline: WriterOutput (draft_v1)

    opt Story placeholders detected
        Pipeline->>GW: elicit_stories(StoryGap[])
        loop Per story gap
            GW-->>UI: SSE {waiting_for_story_input}
            Author->>UI: Provide story details
            UI->>API: POST /job/{id}/story-response
            API->>GW: deliver user message
        end
        GW-->>Pipeline: StoryElicitationResult[]
        Pipeline->>WA: Regenerate draft with stories
    end

    Pipeline-->>UI: SSE {phase: draft_initial, progress: 30}

    Note over Pipeline: Phase 4: DRAFT_REVIEW (30-45%)
    opt Uncertainty questions detected
        Pipeline-->>UI: SSE {waiting_for_answers, pending_questions}
        Author->>UI: Provide answers
        UI->>API: POST /job/{id}/answers
        Pipeline->>WA: revise_from_user_feedback()
    end

    Pipeline-->>UI: SSE {waiting_for_draft_feedback, draft_preview}
    Author->>UI: Review draft, provide feedback
    UI->>API: POST /job/{id}/draft-feedback
    Pipeline->>WA: revise_from_user_feedback()
    Pipeline-->>UI: SSE {phase: draft_review, progress: 45}

    Note over Pipeline: Phase 5: COPY_EDIT (45-60%)
    loop Copy edit iterations
        Pipeline->>CE: run(CopyEditorInput)
        CE-->>Pipeline: CopyEditorOutput {feedback_items}
        alt Editor approves
            Pipeline->>Pipeline: Break loop
        else Feedback provided
            Pipeline->>WA: revise_from_feedback(ReviseWriterInput)
            WA-->>Pipeline: WriterOutput (revised)
        end
    end
    Pipeline-->>UI: SSE {phase: copy_edit, progress: 60}

    Note over Pipeline: Phase 6-7: FACT_CHECK + COMPLIANCE (60-82%)
    Pipeline->>Val: run_validators(draft)
    Val-->>Pipeline: ValidatorReport
    Pipeline->>FC: run(draft)
    FC-->>Pipeline: FactCheckReport
    Pipeline->>CA: run(draft, brand_spec, validator_report)
    CA-->>Pipeline: ComplianceReport

    alt All gates PASS
        Pipeline-->>UI: SSE {phase: compliance, progress: 82}
    else Any gate FAIL
        Note over Pipeline: Phase 8: REWRITE_LOOP (82-96%)
        loop Max rewrite iterations
            Pipeline->>WA: revise_from_feedback(required_fixes)
            Pipeline->>Val: re-run validators
            Pipeline->>FC: re-run fact check
            Pipeline->>CA: re-run compliance
        end
    end

    Note over Pipeline: Phase 9: FINALIZE (96-100%)
    Pipeline->>Pipeline: Generate PublishingPack
    Pipeline->>JS: complete_blog_job()
    Pipeline-->>UI: SSE {phase: finalize, progress: 100, status: COMPLETED}
```

---

## 4. Use Case: Human Collaboration Points

The pipeline pauses at multiple points for human input. Each pause sets a `waiting_for_*` flag on the job record and resumes when the corresponding API endpoint receives input.

### 4.1 Title Selection

```mermaid
sequenceDiagram
    actor Author
    participant UI as Angular UI
    participant API as Blogging API
    participant JS as Job Store
    participant Pipeline as Pipeline

    Pipeline->>JS: update(waiting_for_title_selection=true, title_choices=[...])
    JS-->>UI: SSE {title_choices: [{title, probability, scoring}]}
    UI->>Author: Display title candidates with scores

    Note over Author: Reviews titles:<br/>curiosity_gap, specificity,<br/>audience_fit, seo_potential,<br/>emotional_pull

    Author->>UI: Select preferred title
    UI->>API: POST /job/{id}/select-title {title}
    API->>JS: submit_title_selection(job_id, title)
    JS->>Pipeline: Resume with selected title
    Pipeline->>Pipeline: Continue to DRAFT_INITIAL phase<br/>(selected title threaded into writer/revision prompts)
```

### 4.2 Story Elicitation (Multi-turn Interview)

```mermaid
sequenceDiagram
    actor Author
    participant UI as Angular UI
    participant API as Blogging API
    participant JS as Job Store
    participant GW as Ghost Writer Agent

    Note over GW: Scans draft for [Author: ...] placeholders
    GW->>JS: update(story_gaps=[{section, context, seed_question}])

    loop For each story gap
        GW->>JS: update(waiting_for_story_input=true, current_gap_idx=N)
        JS-->>UI: SSE {story_gap: {section_title, seed_question}}
        UI->>Author: Display interview question

        loop Multi-turn interview (max rounds)
            Author->>UI: Share story detail
            UI->>API: POST /job/{id}/story-response {message}
            API->>JS: submit_story_user_message(job_id, message)
            GW->>GW: Process response, generate follow-up
            GW-->>UI: Follow-up question or "story complete"
        end

        GW->>GW: Compile narrative from conversation
        GW->>JS: Save story to story bank (Postgres)
    end

    GW-->>Pipeline: StoryElicitationResult[] for all gaps
```

### 4.3 Draft Feedback with Guideline Updates

```mermaid
sequenceDiagram
    actor Author
    participant UI as Angular UI
    participant API as Blogging API
    participant JS as Job Store
    participant Pipeline as Pipeline
    participant WA as Writer Agent

    Pipeline->>JS: update(waiting_for_draft_feedback=true, draft_for_review=draft)
    JS-->>UI: SSE {draft_preview, waiting_for_draft_feedback}
    UI->>Author: Display draft for review

    Author->>UI: Provide feedback + optional guideline update request
    UI->>API: POST /job/{id}/draft-feedback {approved, feedback, guideline_updates_requested}
    API->>JS: submit_draft_feedback(job_id, UserDraftFeedback)

    alt Guideline updates requested
        Pipeline->>Pipeline: Analyze feedback for style guide improvements
        Pipeline->>Pipeline: Reload writing guidelines
        Pipeline->>Pipeline: Rebuild Writer + Editor agents with new guidelines
        Pipeline->>JS: update(guideline_updates_applied=true)
    end

    Pipeline->>WA: revise_from_user_feedback(draft, feedback)
    WA-->>Pipeline: Revised WriterOutput

    alt Author approved
        Pipeline->>Pipeline: Proceed to copy edit
    else Author not satisfied
        Pipeline->>JS: update(waiting_for_draft_feedback=true)
        Note over Author: Another review round
    end
```

### 4.4 Uncertainty Question Resolution

```mermaid
sequenceDiagram
    actor Author
    participant UI as Angular UI
    participant API as Blogging API
    participant JS as Job Store
    participant Pipeline as Pipeline
    participant WA as Writer Agent

    WA->>Pipeline: DraftReviewResult {uncertainty_questions: [...]}
    Pipeline->>JS: update(waiting_for_answers=true, pending_questions=[...])
    JS-->>UI: SSE {pending_questions: [{question_id, question, context, section}]}
    UI->>Author: Display questions with section context

    Author->>UI: Provide answers
    UI->>API: POST /job/{id}/answers {question_id: answer, ...}
    API->>JS: submit_blog_answers(job_id, answers)

    Pipeline->>WA: revise_from_user_feedback(draft, answers)
    WA-->>Pipeline: Revised draft with answers incorporated
    Pipeline->>Pipeline: Continue pipeline
```

---

## 5. Use Case: Approval Workflow

The API exposes `approve` and `unapprove` endpoints for completed or needs-human-review jobs. There is no `reject` endpoint — authors can unapprove and provide feedback via draft-feedback instead.

> **Note:** The pipeline orchestrator produces a `PublishingPack` artifact directly. The Publication Agent module provides models and platform formatters but is **not** invoked by the pipeline's `run_pipeline()` function.

```mermaid
sequenceDiagram
    actor Author
    participant UI as Angular UI
    participant API as Blogging API
    participant JS as Job Store

    Note over API: Pipeline completed with<br/>status: COMPLETED or NEEDS_HUMAN_REVIEW

    alt Author approves
        Author->>UI: Approve job
        UI->>API: POST /job/{job_id}/approve
        API->>JS: approve_blog_job(job_id)
        JS-->>API: Updated job (approved=true)
        API-->>UI: BlogJobStatusResponse

    else Author wants changes
        Author->>UI: Unapprove job
        UI->>API: POST /job/{job_id}/unapprove
        API->>JS: unapprove_blog_job(job_id)
        JS-->>API: Updated job (approved=false)
        API-->>UI: BlogJobStatusResponse
    end
```

---

## 6. Use Case: Medium Stats Collection

```mermaid
sequenceDiagram
    actor Author
    participant UI as Angular UI
    participant API as Blogging API
    participant MSA as Medium Stats Agent
    participant IntStore as Integration Store
    participant PW as Playwright Browser

    Author->>UI: Request Medium stats
    UI->>API: POST /medium-stats-async

    API->>IntStore: Check medium integration eligible
    IntStore-->>API: (eligible, error_message)

    alt Not eligible
        API-->>UI: 503 Integration not configured
    else Eligible
        API->>IntStore: resolve_medium_stats_storage_state()

        alt Storage state exists
            IntStore-->>MSA: storage_state dict
        else No storage state
            IntStore->>IntStore: Load Google browser credentials (Postgres)
            IntStore->>PW: Auto-login to Medium via Google
            PW-->>IntStore: New storage_state
            IntStore-->>MSA: storage_state dict
        end

        MSA->>PW: Launch Chromium (headless)
        PW->>PW: Navigate to medium.com/me/stats
        PW->>PW: Scrape stats table
        PW-->>MSA: Raw stats data
        MSA->>MSA: Parse into MediumStatsReport
        MSA-->>API: MediumStatsReport
        API-->>UI: {posts: [{title, url, stats}]}
    end
```

---

## 7. Use Case: Story Bank Reuse

Stories elicited by the Ghost Writer during a previous pipeline run are persisted to the `blogging_stories` Postgres table so they can be reused across posts. The API exposes browse, search, and delete endpoints.

```mermaid
sequenceDiagram
    actor Author
    participant UI as Angular UI
    participant API as Blogging API
    participant PG as Postgres (blogging_stories)

    Author->>UI: Open story bank
    UI->>API: GET /stories
    API->>PG: SELECT * FROM blogging_stories ORDER BY created_at DESC
    PG-->>API: rows
    API-->>UI: Stories list

    Author->>UI: Search "onboarding"
    UI->>API: GET /stories/search/onboarding
    API->>PG: SELECT ... WHERE keywords ? 'onboarding'
    PG-->>API: matching rows
    API-->>UI: Search results

    Author->>UI: Delete story
    UI->>API: DELETE /stories/{story_id}
    API->>PG: DELETE FROM blogging_stories WHERE id = %s
    API-->>UI: {deleted: true}
```

During a new pipeline run, the Ghost Writer elicitation step queries this table for keyword-overlap matches against the current content plan so unchanged stories do not need to be re-interviewed.

---

## 8. API Endpoint Mapping

| Use Case | Method | Endpoint | Request Body | Response |
|----------|--------|----------|-------------|----------|
| Run full pipeline (sync) | POST | `/full-pipeline` | `FullPipelineRequest` | `FullPipelineResponse` |
| Start async pipeline | POST | `/full-pipeline-async` | `FullPipelineRequest` | `StartPipelineResponse {job_id}` |
| List jobs | GET | `/jobs` | — | `ListJobsResponse` |
| Poll job status | GET | `/job/{job_id}` | — | `BlogJobStatusResponse` |
| Stream progress (SSE) | GET | `/job/{job_id}/stream` | — | SSE events |
| Cancel job | POST | `/job/{job_id}/cancel` | — | `CancelJobResponse` |
| Resume job | POST | `/job/{job_id}/resume` | — | `StartPipelineResponse` |
| Restart job | POST | `/job/{job_id}/restart` | — | `StartPipelineResponse` |
| Delete job | DELETE | `/job/{job_id}` | — | `DeleteJobResponse` |
| Select title | POST | `/job/{job_id}/select-title` | `SelectTitleRequest {title}` | `BlogJobStatusResponse` |
| Rate title candidates | POST | `/job/{job_id}/rate-titles` | `RateTitlesRequest {ratings}` | `BlogJobStatusResponse` |
| Submit draft feedback | POST | `/job/{job_id}/draft-feedback` | `DraftFeedbackRequest {feedback, approved}` | `BlogJobStatusResponse` |
| Send story response | POST | `/job/{job_id}/story-response` | `StoryResponseRequest {message}` | `BlogJobStatusResponse` |
| Skip story gap | POST | `/job/{job_id}/skip-story-gap` | — | `BlogJobStatusResponse` |
| Submit answers | POST | `/job/{job_id}/answers` | `BlogAnswersRequest {answers}` | `BlogJobStatusResponse` |
| Approve job | POST | `/job/{job_id}/approve` | — | `BlogJobStatusResponse` |
| Unapprove job | POST | `/job/{job_id}/unapprove` | — | `BlogJobStatusResponse` |
| List artifacts | GET | `/job/{job_id}/artifacts` | — | `ArtifactListResponse` |
| Get artifact content | GET | `/job/{job_id}/artifacts/{artifact_name}` | — | JSON or file download |
| List stories | GET | `/stories` | — | `{stories: [...]}` |
| Get story | GET | `/stories/{story_id}` | — | Story record |
| Delete story | DELETE | `/stories/{story_id}` | — | `{deleted: true}` |
| Search stories | GET | `/stories/search/{keywords}` | — | `{matches: [...]}` |
| Medium stats (sync) | POST | `/medium-stats` | `MediumStatsRequest` | `MediumStatsReport` |
| Medium stats (async) | POST | `/medium-stats-async` | `MediumStatsRequest` | `StartPipelineResponse {job_id}` |
| Health check | GET | `/health` | — | `{status, brand_spec_configured}` |
