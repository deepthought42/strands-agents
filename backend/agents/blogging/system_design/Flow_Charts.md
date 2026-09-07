# Blogging Team — Flow Charts

This document provides detailed flow charts for the blogging pipeline's execution paths, decision trees, feedback loops, and error handling mechanisms.

---

## 1. Master Pipeline Flow

The complete end-to-end pipeline from API request to publishing pack, showing all decision points and exit conditions.

```mermaid
flowchart TD
    Start([POST /full-pipeline-async or /full-pipeline]) --> CreateJob[Create blog job in store<br/>Status: PENDING]
    CreateJob --> ResolveProfile[Resolve LengthPolicy<br/>from content_profile / target_word_count]
    ResolveProfile --> LoadBrandSpec[Load brand spec + writing guidelines<br/>Render Jinja2 templates with AuthorProfile]
    LoadBrandSpec --> StartPipeline[Start pipeline<br/>Status: RUNNING]

    StartPipeline --> Research["Research Agent<br/>Persist research_packet.md<br/>(research phase sub-progress, no BlogPhase slice)"]
    Research --> Planning["BlogWriterAgent.plan_content()<br/>ContentPlan + refine loop<br/>(bounded research_digest)<br/>(BlogPhase.PLANNING, 0-12%)"]

    Planning --> PlanOK{Plan<br/>acceptable?}
    PlanOK -->|No| PlanFail([FAILED<br/>PlanningError])
    PlanOK -->|Yes| TitleSelect["Title Selection<br/>Wait for user choice<br/>(TITLE_SELECTION, 12-15%)"]
    TitleSelect --> WriteDraft["Writer Agent<br/>Generate draft_v1<br/>(DRAFT_INITIAL, 15-25%)"]

    WriteDraft --> StoryCheck{Story<br/>placeholders<br/>detected?}
    StoryCheck -->|Yes| StoryElicit["Ghost Writer Elicitation<br/>Multi-turn interviews<br/>(story_elicitation sub-phase, ~27%)"]
    StoryCheck -->|No| DraftReady
    StoryElicit --> RegenDraft[Regenerate draft with stories]
    RegenDraft --> DraftReady

    DraftReady[Draft ready] --> UncertaintyCheck{Uncertainty<br/>questions?}
    UncertaintyCheck -->|Yes| WaitAnswers["Wait for user answers<br/>(DRAFT_REVIEW, 30-35%)"]
    UncertaintyCheck -->|No| DraftReview
    WaitAnswers --> ReviseAnswers[Revise draft with answers]
    ReviseAnswers --> DraftReview

    DraftReview["Wait for draft feedback<br/>(DRAFT_REVIEW, 35-45%)"] --> FeedbackReceived{Author<br/>approved?}
    FeedbackReceived -->|Feedback| ReviseDraft[Revise from user feedback]
    ReviseDraft --> DraftReview
    FeedbackReceived -->|Approved / Skipped| CopyEdit

    CopyEdit["Copy Edit Loop<br/>(COPY_EDIT_LOOP, 45-60%)"] --> EditorApproved{Editor<br/>approved?}
    EditorApproved -->|Yes| RunGates
    EditorApproved -->|Loop exhausted| RunGates

    subgraph RunGates["Quality Gates (FACT_CHECK 60-70%, COMPLIANCE 70-82%)"]
        Validators["Validators<br/>Banned phrases, reading level,<br/>paragraph length, required sections"]
        FactCheck["Fact Check Agent<br/>Claims verification, risk flags,<br/>required disclaimers"]
        Compliance["Compliance Agent<br/>Brand spec alignment,<br/>violation detection"]
        Validators --> FactCheck --> Compliance
    end

    Compliance --> GatesPass{All gates<br/>PASS?}
    GatesPass -->|Yes| Finalize["Write final.md +<br/>publishing_pack.json<br/>(FINALIZE, 96-100%)"]
    GatesPass -->|No| RewriteCheck{Rewrite<br/>iterations<br/>remaining?}
    RewriteCheck -->|Yes| Rewrite["Rewrite from consolidated<br/>required_fixes<br/>(REWRITE_LOOP, 82-96%)"]
    Rewrite --> RunGates
    RewriteCheck -->|No| NeedsReview([NEEDS_HUMAN_REVIEW])

    Finalize --> Complete([COMPLETED / PASS])

    style PlanFail fill:#ffcccc,stroke:#cc0000
    style NeedsReview fill:#fff3cd,stroke:#cc9900
    style Complete fill:#ccffcc,stroke:#00cc00
```

**Implementation notes:**

- `run_planning()` (`pipeline/_common.py`) calls `ResearchAgent.run()` ahead of planning, persists its compiled document as `research_packet.md`, and passes a digest hard-capped to the smallest context budget across the planner, optional plan critic, and current failover candidates into `PlanningInput.research_digest`. A run with no web references or academic papers keeps the diagnostic packet but supplies an empty digest; outline revisions reuse the initial digest. Progress is reported on a "research" phase (not a `BlogPhase` slice).
- Planning is done inline by `BlogWriterAgent.plan_content()` (`blog_writer_agent/agent.py:294`), not by a separate `BlogPlanningAgent` class.
- Validators, fact-check, and compliance all run inside a single loop bounded by `max_rewrite_iterations`. Title selection is only reached after an iteration where every gate returned `PASS`.

---

## 2. Planning Refine Loop

The planning agent generates and refines the content plan until it meets acceptance criteria or exhausts iterations.

```mermaid
flowchart TD
    Start([Begin Planning]) --> LoadResearch[Load research digest<br/>Cap at 200K chars]
    LoadResearch --> BuildContext[Build planning context:<br/>brief + audience + length policy<br/>+ series context]

    BuildContext --> Iteration{Iteration<br/>count}
    Iteration -->|First| Generate[Generate initial ContentPlan<br/>GENERATE_PLAN_SYSTEM prompt<br/>temperature=0.25, think=true]
    Iteration -->|Subsequent| Refine[Refine existing ContentPlan<br/>REFINE_PLAN_SYSTEM prompt<br/>with previous analysis feedback]

    Generate --> ParseJSON
    Refine --> ParseJSON

    ParseJSON[Parse JSON response] --> ParseOK{Parse<br/>successful?}
    ParseOK -->|No| RetryParse{Parse retries<br/>remaining?}
    RetryParse -->|Yes| FallbackParse["Fallback: complete() +<br/>parse_json_object()"]
    FallbackParse --> ParseOK
    RetryParse -->|No| ParseFail([PlanningError<br/>PARSE_FAILURE])

    ParseOK -->|Yes| ValidateSections[Validate section count<br/>against content_profile bounds]
    ValidateSections --> SectionOK{Section count<br/>in bounds?}
    SectionOK -->|No| OverridePlan["Set plan_acceptable = false<br/>Add gap: section count mismatch"]
    SectionOK -->|Yes| CheckAnalysis

    OverridePlan --> CheckAnalysis

    CheckAnalysis{requirements_analysis:<br/>plan_acceptable AND<br/>scope_feasible?}
    CheckAnalysis -->|Yes| Success([PlanningPhaseResult<br/>content_plan + metrics])
    CheckAnalysis -->|No| IterCheck{Max iterations<br/>reached?}
    IterCheck -->|No| Iteration
    IterCheck -->|Yes| MaxIter([PlanningError<br/>MAX_ITERATIONS_REACHED])

    style ParseFail fill:#ffcccc,stroke:#cc0000
    style MaxIter fill:#ffcccc,stroke:#cc0000
    style Success fill:#ccffcc,stroke:#00cc00
```

**Key constants**:
- `BLOG_PLANNING_MAX_ITERATIONS`: default 5 (env configurable)
- `BLOG_PLANNING_MAX_PARSE_RETRIES`: default 3 (env configurable)
- Temperature: 0.25 with `think=true` for structured reasoning

---

## 3. Copy-Edit Feedback Loop

The copy editor and writer iterate until the draft is approved, the loop stalls, or the escalation threshold triggers human intervention.

```mermaid
flowchart TD
    Start([Begin Copy Edit]) --> Init[Initialize FeedbackTracker<br/>window_size=3]

    Init --> EditorRun["Copy Editor Agent<br/>run(CopyEditorInput)"]
    EditorRun --> EditorResult{Editor<br/>approved?}

    EditorResult -->|Yes| Approved([Draft Approved<br/>Proceed to gates])

    EditorResult -->|No| TrackFeedback[Track feedback items<br/>in FeedbackTracker]
    TrackFeedback --> StaleCheck{Feedback<br/>stale?}
    StaleCheck -->|"Yes (same issues repeating)"| StaleAccept([Accept draft<br/>Loop stalled])

    StaleCheck -->|No| EscalationCheck{Iterations >=<br/>ESCALATION_THRESHOLD<br/>(default 10)?}

    EscalationCheck -->|Yes| Escalate["Pause for human feedback<br/>waiting_for_draft_feedback=true"]
    Escalate --> HumanResponse{Human<br/>response?}
    HumanResponse -->|Approved| Approved
    HumanResponse -->|Feedback| HumanRevise[Revise from human feedback]
    HumanRevise --> ContinueLoop

    EscalationCheck -->|No| WriterRevise

    WriterRevise["Writer Agent<br/>revise_from_feedback(ReviseWriterInput)<br/>with FeedbackItems + persistent_issues"]
    WriterRevise --> WriterDone[Updated draft]

    ContinueLoop --> IterCheck
    WriterDone --> IterCheck{Max iterations<br/>reached?<br/>(default 30)}
    IterCheck -->|No| EditorRun
    IterCheck -->|Yes| Exhausted([Accept draft<br/>Max iterations])

    style Approved fill:#ccffcc,stroke:#00cc00
    style StaleAccept fill:#fff3cd,stroke:#cc9900
    style Exhausted fill:#fff3cd,stroke:#cc9900
```

**Staleness detection**: The `FeedbackTracker` keeps a sliding window of the last 3 feedback rounds. If the same issue categories and locations repeat without resolution, the loop is considered stalled and the draft is accepted as-is.

**Escalation**: After `COPY_EDIT_ESCALATION_THRESHOLD` (default 10) iterations without editor approval, the pipeline pauses and presents the draft to the human author for intervention.

---

## 4. Quality Gate System

The three-tier validation system that determines whether a draft is publication-ready.

```mermaid
flowchart TD
    Start([Draft ready for gates]) --> HasWorkDir{work_dir and<br/>run_gates=true?}
    HasWorkDir -->|No| SkipGates([Skip gates<br/>Return draft as-is])
    HasWorkDir -->|Yes| RunValidators

    RunValidators["Deterministic Validators<br/>- banned_phrases<br/>- banned_patterns<br/>- paragraph_length<br/>- reading_level<br/>- required_sections<br/>- claims_policy"]
    RunValidators --> ValResult[ValidatorReport]

    ValResult --> RunFactCheck["Fact Check Agent<br/>- Verify claims against research<br/>- Flag unsubstantiated statements<br/>- Identify required disclaimers"]
    RunFactCheck --> FCResult[FactCheckReport]

    FCResult --> RunCompliance["Compliance Agent<br/>- Brand spec alignment<br/>- Validator report context<br/>- Style enforcement"]
    RunCompliance --> CompResult[ComplianceReport]

    CompResult --> EvalGates{Evaluate all<br/>gate results}

    EvalGates --> ValPass{Validators<br/>PASS?}
    ValPass -->|No| CollectFixes
    ValPass -->|Yes| FCPass{Fact Check<br/>claims_status &<br/>risk_status PASS?}
    FCPass -->|No| CollectFixes
    FCPass -->|Yes| CompPass{Compliance<br/>PASS?}
    CompPass -->|No| CollectFixes
    CompPass -->|Yes| AllPass([All Gates PASS<br/>Proceed to title selection])

    CollectFixes["Collect required_fixes from<br/>all failed gates<br/>Consolidate into FeedbackItems"]
    CollectFixes --> RewriteAvail{Rewrite<br/>iterations<br/>remaining?}
    RewriteAvail -->|Yes| Rewrite["Writer Agent<br/>revise_from_feedback()<br/>with consolidated fixes"]
    Rewrite --> RunValidators
    RewriteAvail -->|No| HumanReview([NEEDS_HUMAN_REVIEW<br/>Max rewrites exhausted])

    style AllPass fill:#ccffcc,stroke:#00cc00
    style HumanReview fill:#fff3cd,stroke:#cc9900
    style SkipGates fill:#e6f3ff,stroke:#4a90d9
```

**Gate evaluation order**: Validators (cheapest, no LLM) → Fact Check → Compliance (most expensive). All three run even if early gates fail, so the rewrite loop gets comprehensive feedback in one pass.

**Rewrite budget**: Default `max_rewrite_iterations=3` (configurable per request, hard cap at 100).

---

## 5. Story Elicitation Flow

The ghost writer agent identifies story opportunities in the content plan and conducts multi-turn interviews to collect personal narratives.

```mermaid
flowchart TD
    Start([Draft generated]) --> ScanDraft["Scan draft for<br/>[Author: topic] placeholders"]
    ScanDraft --> HasPlaceholders{Placeholders<br/>found?}
    HasPlaceholders -->|No| Done([Continue pipeline<br/>No stories needed])

    HasPlaceholders -->|Yes| CheckStoryBank["Query story bank<br/>for matching keywords"]
    CheckStoryBank --> HasExisting{Relevant stories<br/>in bank?}
    HasExisting -->|Yes| MergeStories["Merge existing stories<br/>with new gaps"]
    HasExisting -->|No| IdentifyGaps

    MergeStories --> IdentifyGaps["Identify remaining<br/>StoryGap[] list"]
    IdentifyGaps --> HasGaps{Unfilled gaps<br/>remain?}
    HasGaps -->|No| RegenerateDraft

    HasGaps -->|Yes| SetGapIdx["Set current_story_gap_index=0"]
    SetGapIdx --> BeginInterview

    subgraph Interview["Per-Gap Interview Loop"]
        BeginInterview["Present seed_question<br/>to author"]
        BeginInterview --> WaitInput["Wait for user story input<br/>waiting_for_story_input=true"]
        WaitInput --> UserResponse["Author provides<br/>story details"]
        UserResponse --> ProcessResponse["Ghost Writer processes<br/>response"]
        ProcessResponse --> FollowUp{More detail<br/>needed?}
        FollowUp -->|Yes| AskFollowUp["Generate follow-up<br/>question"]
        AskFollowUp --> WaitInput
        FollowUp -->|No / Max rounds| CompileNarrative["Compile narrative<br/>from conversation"]
    end

    CompileNarrative --> SaveToBank["Save story to<br/>Postgres story bank"]
    SaveToBank --> NextGap{More gaps?}
    NextGap -->|Yes| IncrementGap["Increment gap index"]
    IncrementGap --> BeginInterview
    NextGap -->|No| RegenerateDraft

    RegenerateDraft["Regenerate draft<br/>with all stories +<br/>skip instructions for<br/>used placeholders"]
    RegenerateDraft --> SaveDraft["Persist draft_v1_answered.md"]
    SaveDraft --> DoneWithStories([Continue pipeline<br/>with enriched draft])

    style Done fill:#e6f3ff,stroke:#4a90d9
    style DoneWithStories fill:#ccffcc,stroke:#00cc00
```

**Story bank reuse**: Stories persisted in Postgres (`blogging_stories` table) are queried by keyword overlap for future posts, avoiding redundant interviews.

---

## 6. Error Handling & Recovery

```mermaid
flowchart TD
    Start([Pipeline execution]) --> TryCatch{Try / Except}

    TryCatch -->|CancelledError| CheckTemporal{Temporal<br/>cancellation?}
    CheckTemporal -->|Yes| MarkCancelled["Mark job CANCELLED<br/>Stop heartbeat thread"]
    CheckTemporal -->|No| Propagate[Propagate exception]

    TryCatch -->|PlanningError| CheckPlanCancel{External<br/>cancellation in<br/>exception chain?}
    CheckPlanCancel -->|Yes| MarkCancelled
    CheckPlanCancel -->|No| FailPlanning["fail_job:<br/>failed_phase=planning<br/>planning_failure_reason"]

    TryCatch -->|BloggingError| FailWithPhase["fail_job:<br/>failed_phase from error.phase<br/>error message"]

    TryCatch -->|Exception| FailGeneric["fail_job:<br/>Log error<br/>Generic failure message"]

    TryCatch -->|Success| Complete["complete_blog_job:<br/>status=COMPLETED or<br/>NEEDS_HUMAN_REVIEW"]

    MarkCancelled --> Cleanup
    FailPlanning --> Cleanup
    FailWithPhase --> Cleanup
    FailGeneric --> Cleanup
    Complete --> Cleanup

    Cleanup["Finally block:<br/>- Stop heartbeat thread<br/>- Join heartbeat thread<br/>- Publish terminal SSE event<br/>- Cleanup job from event bus"]

    style MarkCancelled fill:#e6f3ff,stroke:#4a90d9
    style FailPlanning fill:#ffcccc,stroke:#cc0000
    style FailWithPhase fill:#ffcccc,stroke:#cc0000
    style FailGeneric fill:#ffcccc,stroke:#cc0000
    style Complete fill:#ccffcc,stroke:#00cc00
```

### LLM-Level Error Recovery

```mermaid
flowchart TD
    LLMCall([Agent calls LLM]) --> Response{Response<br/>status?}

    Response -->|200 OK| ParseJSON{Valid JSON?}
    ParseJSON -->|Yes| Success([Return parsed result])
    ParseJSON -->|No| RetryParse{Parse retries<br/>remaining?}
    RetryParse -->|Yes| FallbackParse["Try complete() +<br/>parse_json_object()"]
    FallbackParse --> ParseJSON
    RetryParse -->|No| JsonError([LLMJsonParseError])

    Response -->|429| RateLimit([LLMRateLimitError<br/>after retry exhaustion])

    Response -->|5xx| ServerError([LLMTemporaryError<br/>after retry exhaustion])

    Response -->|4xx non-429| PermanentError([LLMPermanentError])

    Response -->|Network error| Unreachable([LLMUnreachableError<br/>after retry exhaustion])

    style Success fill:#ccffcc,stroke:#00cc00
    style JsonError fill:#ffcccc,stroke:#cc0000
    style RateLimit fill:#ffcccc,stroke:#cc0000
    style ServerError fill:#ffcccc,stroke:#cc0000
    style PermanentError fill:#ffcccc,stroke:#cc0000
    style Unreachable fill:#ffcccc,stroke:#cc0000
```

---

## 7. Async Pipeline: Temporal vs Thread Branching

When a client calls `POST /full-pipeline-async`, the API chooses a runtime mode based on whether Temporal is configured. Both paths end up calling the same `run_blog_full_pipeline_job()` entry point.

```mermaid
flowchart TD
    Start([POST /full-pipeline-async]) --> CreateJob[create_blog_job<br/>Status: PENDING]
    CreateJob --> CheckTemporal{TEMPORAL_ADDRESS<br/>set?}

    CheckTemporal -->|Yes| StartWF[temporal client:<br/>start BlogFullPipelineWorkflow]
    StartWF --> Worker[Temporal worker picks up activity]
    Worker --> Activity[run_full_pipeline_activity]
    Activity --> HB[Spawn heartbeat thread<br/>activity.heartbeat every 30s]
    HB --> RunJob

    CheckTemporal -->|No| Thread[Daemon Python thread<br/>runs in API process]
    Thread --> RunJob

    RunJob["run_blog_full_pipeline_job(job_id, request)"]
    RunJob --> Pipeline[run_pipeline orchestrator]
    Pipeline --> JobUpdates[Update Postgres job store<br/>Publish SSE events]

    style CheckTemporal fill:#e6f3ff,stroke:#4a90d9
    style RunJob fill:#ccffcc,stroke:#00cc00
```

In thread mode, a failure crashes only the daemon thread; the API process keeps serving. In Temporal mode, the workflow is durable: if the worker dies mid-pipeline the retry policy reruns the activity up to 3 times (30s→2m backoff), and the `heartbeat_timeout=5m` ensures stuck activities are detected and reassigned.

---

## 8. Temporal Workflow Execution Detail

The durable workflow execution path when `TEMPORAL_ADDRESS` is configured.

```mermaid
sequenceDiagram
    participant API as Blogging API
    participant TC as Temporal Client
    participant TW as Temporal Worker
    participant WF as BlogFullPipelineWorkflow
    participant Act as run_full_pipeline_activity
    participant HB as Heartbeat Thread
    participant JS as Job Store
    participant Pipeline as Pipeline Orchestrator

    API->>TC: start_workflow(job_id, request_dict)
    TC->>TW: Schedule workflow execution
    TW->>WF: run(job_id, request_dict)

    WF->>TW: execute_activity(run_full_pipeline_activity)
    Note over WF: schedule_to_close_timeout: 12h<br/>heartbeat_timeout: 5m<br/>retry_policy: 3 attempts, 30s-2m backoff

    TW->>Act: run_full_pipeline_activity(job_id, request_dict)
    Act->>HB: Spawn heartbeat thread (30s interval)
    Act->>Pipeline: run_blog_full_pipeline_job()

    loop Every 30 seconds
        HB->>HB: activity.heartbeat()
        Note over HB: Temporal uses heartbeats<br/>for cancellation detection
    end

    Pipeline->>JS: Update progress, phase, status
    Pipeline-->>API: SSE events via event bus

    alt Pipeline succeeds
        Pipeline-->>Act: Return
        Act->>HB: Stop heartbeat
        Act-->>WF: Activity complete
        WF-->>TC: Workflow complete
    else Pipeline fails
        Pipeline-->>Act: Raise BloggingError
        Act->>HB: Stop heartbeat
        Note over Act: Retry policy triggers<br/>up to 3 attempts
    else External cancellation
        TC->>TW: Cancel workflow
        TW->>Act: CancelledError
        Act->>HB: Stop heartbeat
        Act->>JS: Mark job CANCELLED
    end
```

**Temporal retry policy**:
- `maximum_attempts`: 3
- `initial_interval`: 30 seconds
- `maximum_interval`: 2 minutes
- `backoff_coefficient`: 2.0

---

## 9. Finalization & Approval Decision Tree

After all quality gates pass and the title is selected, the pipeline generates a `PublishingPack` artifact and completes the job. Approval is handled by the API layer via separate endpoints.

> **Note:** The pipeline orchestrator produces the `PublishingPack` directly and does **not** invoke the Publication Agent. The Publication Agent module provides platform formatters and models that can be used independently.

```mermaid
flowchart TD
    Start([All gates PASS<br/>Title selected]) --> GeneratePack["Generate PublishingPack artifact<br/>- title_options<br/>- meta_description<br/>- header_polish<br/>- internal_links<br/>- snippet_copy<br/>- tags"]

    GeneratePack --> WriteFinal["Write final.md +<br/>publishing_pack.json to work_dir"]
    WriteFinal --> CompleteJob["complete_blog_job()<br/>Status: COMPLETED"]
    CompleteJob --> SSE["SSE: {phase: finalize,<br/>progress: 100, status: COMPLETED}"]

    SSE --> WaitApproval["Job available for<br/>author review"]

    WaitApproval --> Decision{Author<br/>decision?}

    Decision -->|"POST /job/{id}/approve"| Approve["approve_blog_job(job_id)<br/>Mark as approved"]
    Approve --> Approved([Job approved])

    Decision -->|"POST /job/{id}/unapprove"| Unapprove["unapprove_blog_job(job_id)<br/>Clear approval flag"]
    Unapprove --> WaitApproval

    style Approved fill:#ccffcc,stroke:#00cc00
```

---

## 10. Research Agent Internal Flow

The research agent lives in `blog_research_agent/` and is invoked by `run_planning()` (see §1, "Research" step) to produce the `research_packet.md` artifact, as well as being usable standalone / via direct scripted use. The flow below documents how the module works internally regardless of caller.

```mermaid
flowchart TD
    Start([ResearchBriefInput]) --> Parse["Parse brief<br/>Normalize audience, tone"]
    Parse --> GenQueries["Generate 3-5 search queries<br/>via LLM (QUERY_GENERATION_PROMPT)"]

    GenQueries --> Search

    subgraph Search["Parallel Search Execution"]
        WebSearch["Ollama web_search<br/>(per query)"]
        ArXivSearch["arXiv API search<br/>(per query)"]
    end

    Search --> FetchDocs["Fetch top documents<br/>SimpleWebFetcher<br/>(up to max_fetch_documents)"]
    FetchDocs --> Score["Score candidates<br/>by relevance to brief<br/>(DOC_RELEVANCE_SCORING_PROMPT)"]
    Score --> Rank["Rank by relevance,<br/>authority, recency, diversity"]
    Rank --> Summarize["Summarize top results<br/>(DOC_SUMMARIZATION_PROMPT)"]
    Summarize --> Synthesize["Synthesize findings<br/>into compiled_document<br/>(FINAL_SYNTHESIS_PROMPT)"]
    Synthesize --> Output([ResearchAgentOutput<br/>query_plan, references,<br/>compiled_document, notes])

    style Output fill:#ccffcc,stroke:#00cc00
```
