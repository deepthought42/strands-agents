# Blogging Team — System Architecture

This document describes the high-level architecture of the Blogging Agent Suite: how it fits within the Strands Agents platform, its internal component structure, deployment topology, shared infrastructure dependencies, and agent responsibilities.

---

## 1. System Context

The blogging team operates as one of 24 agent teams within the Strands Agents platform. It is mounted at `/api/blogging` by the Unified API and accessed through the Angular 19 frontend.

```mermaid
graph TD
    subgraph External
        User["Human Author / Browser"]
        Medium["Medium.com"]
        ArXiv["arXiv.org"]
        OllamaCloud["Ollama Cloud API"]
    end

    subgraph Strands Platform
        UI["Angular 19 Frontend<br/>(port 4200)"]
        UnifiedAPI["Unified FastAPI Server<br/>(port 8080)"]
        SecurityGW["Security Gateway"]

        subgraph Blogging Team
            BlogAPI["Blogging FastAPI App<br/>(/api/blogging)"]
            Pipeline["Pipeline Orchestrator<br/>(blog_writing_process_v2)"]
            Agents["10 Agent Roles<br/>(orchestrated + standalone)"]
        end

        Temporal["Temporal Server<br/>(port 7233)"]
        Postgres["PostgreSQL<br/>(port 5432)"]
        LLMService["LLM Service<br/>(Ollama / Claude)"]
    end

    User -->|HTTP / SSE| UI
    UI -->|REST API| UnifiedAPI
    UnifiedAPI --> SecurityGW --> BlogAPI
    BlogAPI -->|async jobs| Pipeline
    Pipeline --> Agents
    Agents -->|inference| LLMService
    LLMService -->|API calls| OllamaCloud
    Agents -->|web search| OllamaCloud
    Agents -->|paper search| ArXiv
    Agents -->|stats scraping| Medium
    BlogAPI -->|durable workflows| Temporal
    BlogAPI -->|job state, stories| Postgres
    Pipeline -.->|SSE events| UI
```

---

## 2. Component Architecture

The pipeline orchestrator (`run_pipeline()` in `agent_implementations/blog_writing_process_v2.py`) coordinates agents across three functional groups that run inline: Content Production, Quality Gates, and Finalization. Planning (`run_planning()`, `pipeline/_common.py`) also invokes the Research Agent inline, ahead of content planning, to produce a `research_packet.md` artifact — see Note 2. Three additional agent modules — Planning Agent (class), Publication Agent, and Medium Stats Agent — live in the team but are **not** invoked by `run_pipeline()` in the current v2 path: they are either standalone modules or are driven directly by the API layer.

```mermaid
graph LR
    subgraph Orchestrator["Orchestrator: run_pipeline()"]
        RP["blog_writing_process_v2.py"]
    end

    subgraph InlinePlanning["Planning (inline)"]
        RA["Research Agent<br/><i>blog_research_agent/</i><br/>(research_packet.md artifact only)"]
        WAPlan["BlogWriterAgent.plan_content()<br/><i>blog_writer_agent/agent.py</i>"]
    end

    subgraph Production["Content Production"]
        WA["Writer Agent<br/><i>blog_writer_agent/</i>"]
        CEA["Copy Editor Agent<br/><i>blog_copy_editor_agent/</i>"]
        GWA["Ghost Writer Elicitation<br/><i>ghost_writer_agent/</i>"]
    end

    subgraph Gates["Quality Gates"]
        VA["Validators<br/><i>validators/runner.py</i>"]
        FCA["Fact Check Agent<br/><i>blog_fact_check_agent/</i>"]
        CMA["Compliance Agent<br/><i>blog_compliance_agent/</i>"]
    end

    subgraph Standalone["Standalone / API-Level"]
        PAStand["Planning Agent class<br/><i>blog_planning_agent/</i><br/>(alternative, not wired)"]
        PUA["Publication Agent<br/><i>blog_publication_agent/</i>"]
        MSA["Medium Stats Agent<br/><i>blog_medium_stats_agent/</i>"]
    end

    RP --> RA
    RP --> WAPlan
    RP --> WA
    RP --> GWA
    RP --> CEA
    RP --> VA
    RP --> FCA
    RP --> CMA

    WAPlan -->|ContentPlan| WA
    WA -->|draft| CEA
    CEA -->|FeedbackItems| WA
    GWA -->|elicited stories| WA
    WA -->|final draft| VA
    VA -->|ValidatorReport| FCA
    FCA -->|FactCheckReport| CMA
    CMA -->|ComplianceReport| RP
    RP -->|PublishingPack| RP
```

> **Note 1:** Planning is performed by `BlogWriterAgent.plan_content()` (see `blog_writer_agent/agent.py:294`), which implements the refine-until-done loop. The `BlogPlanningAgent` class in `blog_planning_agent/agent.py` is an alternative implementation that is not currently wired into `run_pipeline()`.
>
> **Note 2:** `run_planning()` (`pipeline/_common.py`) invokes `ResearchAgent.run()` ahead of content planning, persists its compiled document as the `research_packet.md` artifact when `work_dir` is set, and passes a digest capped to the smallest context budget across the planner, optional plan critic, and each current failover candidate into `PlanningInput.research_digest`. Shared compaction also sizes each source chunk for the smallest failover candidate and skips the LLM call when that context cannot fit its fixed prompt/response reserves. A run with no web references or academic papers supplies an empty digest while retaining the packet for diagnostics. Outline revisions reuse the same digest.
>
> **Note 3:** `run_pipeline()` writes a `PublishingPack` artifact but does **not** call the Publication Agent. Publication (approve/unapprove) and Medium stats collection are handled by separate API endpoints.
>
> **Note 4:** `allowed_claims.json` is documented (`blog_research_agent/allowed_claims.py`'s module docstring) as a Research Librarian output, but its extraction isn't yet wired to the research call described in Note 2. As an interim measure, planning itself calls `extract_allowed_claims()` against the content plan's own markdown with an empty reference list (see `_persist_content_plan_artifacts()` in `agent_implementations/pipeline/_common.py`, used by both `run_planning()` and `run_planning_stage()`'s outline re-plan loop), producing a citation-free `allowed_claims.json`. This call site should move to consume `ResearchAgent`'s output as a separate follow-up.

### Pipeline Phase Mapping

Phase names and progress ranges come from `BlogPhase` and `PHASE_PROGRESS_RANGES` in `shared/models.py`.

| `BlogPhase` | Enum value | Progress | Agent(s) Involved |
|-------------|-----------|----------|-------------------|
| `PLANNING` | `planning` | 0–12% | `BlogWriterAgent.plan_content()` (refine loop) |
| `TITLE_SELECTION` | `title_selection` | 12–15% | Human choice (via job store) |
| `DRAFT_INITIAL` | `draft_initial` | 15–30% | Writer Agent, Ghost Writer Elicitation |
| `DRAFT_REVIEW` | `draft_review` | 30–45% | Writer Agent (human feedback loop) |
| `COPY_EDIT_LOOP` | `copy_edit` | 45–60% | Copy Editor Agent ↔ Writer Agent |
| `FACT_CHECK` | `fact_check` | 60–70% | Validators + Fact Check Agent |
| `COMPLIANCE` | `compliance` | 70–82% | Compliance Agent |
| `REWRITE_LOOP` | `rewrite` | 82–96% | Writer Agent (gate-driven rewrites) |
| `FINALIZE` | `finalize` | 96–100% | Pipeline (publishing pack generation) |

**Implementation note:** Title selection runs exactly once per run, at the end of the planning stage (`run_planning_stage`, immediately after outline approval) — before any draft exists, so the author's choice can be threaded into the writer/revision prompts. `run_pipeline()`'s gates stage separately evaluates the gates inside a single loop bounded by `max_rewrite_iterations`, running Validators → Fact Check → Compliance on each iteration; it reads the already-selected title (`ctx.selected_title`) to build `PublishingPack.title_options` and to preserve it across gate-driven rewrites, but runs no title-selection round of its own.

---

## 3. Infrastructure & Deployment

```mermaid
graph TD
    subgraph "Docker Compose Stack"
        subgraph "Application Layer"
            AgentsContainer["Agents Container<br/>port 8080<br/>Unified API + all teams"]
            UIContainer["UI Container<br/>port 4200<br/>Angular 19"]
        end

        subgraph "Infrastructure Layer"
            PG["PostgreSQL<br/>port 5432"]
            TemporalSvc["Temporal Server<br/>port 7233"]
            TemporalUI["Temporal UI<br/>port 8088"]
            Ollama["Ollama Server<br/>port 11434"]
        end

        subgraph "Shared Volume"
            Vol["agents_data<br/>/data/agents"]
        end
    end

    subgraph "External Services"
        OllamaCloudSvc["Ollama Cloud API<br/>(OLLAMA_API_KEY)"]
        MediumSvc["Medium.com<br/>(Playwright scraping)"]
        ArXivSvc["arXiv API<br/>(paper search)"]
    end

    UIContainer -->|REST / SSE| AgentsContainer
    AgentsContainer -->|SQL| PG
    AgentsContainer -->|gRPC| TemporalSvc
    AgentsContainer -->|HTTP| Ollama
    Ollama -->|API| OllamaCloudSvc
    AgentsContainer -->|Playwright| MediumSvc
    AgentsContainer -->|HTTP| ArXivSvc
    AgentsContainer --- Vol
    UIContainer --- Vol

    style Vol fill:#e6f3ff,stroke:#4a90d9
```

### Volume & Storage Layout

All blogging team artifacts persist under the shared `agents_data` Docker volume:

```
/data/agents/
  blogging_team/                    # AGENT_CACHE/blogging_team
    medium_stats_runs/              # BLOGGING_MEDIUM_STATS_ROOT
  blogging/runs/                    # BLOGGING_RUN_ARTIFACTS_ROOT (per-job)
    {job_id}/
      research_packet.md
      content_plan.json
      draft_v1.md
      ...
      publishing_pack.json
```

### Port Allocation

| Service | Port | Protocol |
|---------|------|----------|
| Unified API (all teams) | 8080 | HTTP |
| Angular UI | 4200 | HTTP |
| PostgreSQL | 5432 | TCP |
| Temporal Server | 7233 | gRPC |
| Temporal UI | 8088 | HTTP |
| Ollama | 11434 | HTTP |

---

## 4. Shared Infrastructure Dependencies

The blogging team relies on four shared infrastructure modules provided by the platform.

```mermaid
graph TD
    Blog["Blogging Team"]

    subgraph "Shared Infrastructure"
        SP["shared.postgres/<br/>Schema registry + connection pool"]
        ST["shared.temporal/<br/>Workflow orchestration + checkpoints"]
        LLM["llm_service/<br/>Unified LLM client (Ollama / Claude)"]
        EB["event_bus/<br/>SSE pub/sub per job"]
    end

    Blog -->|"register_team_schemas(SCHEMA)"| SP
    Blog -->|"BlogFullPipelineWorkflow"| ST
    Blog -->|"get_client() → complete_json()"| LLM
    Blog -->|"publish() / subscribe()"| EB

    SP -->|"get_conn() + timed_query()"| PG["PostgreSQL"]
    ST -->|"workflow.execute_activity()"| TMP["Temporal Server"]
    LLM -->|"HTTP inference"| OLL["Ollama / Claude"]
```

| Module | Blogging Team Usage |
|--------|---------------------|
| **shared.postgres** | `blogging_stories` table for story bank persistence; schema registered at FastAPI lifespan startup via `register_team_schemas(SCHEMA)`. See `postgres/__init__.py` |
| **shared.temporal** | `BlogFullPipelineWorkflow` wraps the full pipeline as a single long-lived activity. `schedule_to_close_timeout=12h`, `heartbeat_timeout=5m`, `RetryPolicy(maximum_attempts=3, initial_interval=30s, maximum_interval=2m, backoff_coefficient=2.0)`. A background heartbeat thread calls `activity.heartbeat()` every 30s (`shared/run_pipeline_job.py:170`). See `temporal/workflows.py:15-38` |
| **llm_service** | `get_client("blog")` returns an `OllamaLLMClient`; agents call `complete_json()` for structured output and `complete()` for text generation. Planning can use a different model via the `BLOG_PLANNING_MODEL` env var |
| **event_bus** | Thread-safe SSE pub/sub (`shared/job_event_bus.py`): `run_pipeline_job.py` publishes update events for every job store mutation; the UI subscribes via `GET /job/{job_id}/stream` |

---

## 5. Agent Responsibility Matrix

Agents marked **[pipeline]** are orchestrated by `run_pipeline()`. Agents marked **[standalone]** live in the team but are not invoked by the pipeline — they are either triggered directly by the API layer or exist as alternative implementations.

| Agent | Module | Orchestration | Role | Key I/O |
|-------|--------|---------------|------|---------|
| **Writer Agent** | `blog_writer_agent/` | [pipeline] | Plan generation (`plan_content()`) + initial draft + feedback-driven revision + uncertainty detection | `PlanningInput`/`WriterInput`/`ReviseWriterInput` → `PlanningPhaseResult`/`WriterOutput` |
| **Ghost Writer Elicitation** | `ghost_writer_agent/` | [pipeline] | Identifies `[Author: ...]` placeholders, conducts multi-turn interviews, persists stories to Postgres for reuse | `ContentPlan`/`StoryGap` → `StoryElicitationResult[]` |
| **Copy Editor Agent** | `blog_copy_editor_agent/` | [pipeline] | Read-only feedback loop with staleness detection and human-escalation after `COPY_EDIT_ESCALATION_THRESHOLD` (default 10) iterations | `CopyEditorInput` → `CopyEditorOutput` |
| **Validators** | `validators/` | [pipeline] | Deterministic no-LLM checks (banned phrases, reading level, paragraph length, required sections, claims policy) | Draft text → `ValidatorReport` |
| **Fact Check Agent** | `blog_fact_check_agent/` | [pipeline] | LLM-based claim verification and risk flagging with required-disclaimer detection | Draft + allowed claims → `FactCheckReport` |
| **Compliance Agent** | `blog_compliance_agent/` | [pipeline] | LLM-based brand/style enforcement with veto power — FAIL triggers the closed-loop rewrite | Draft + brand spec + validator report → `ComplianceReport` |
| **Research Agent** | `blog_research_agent/` | [pipeline] | Ollama web_search + arXiv + ranking + synthesis. Called from `run_planning()` to persist `research_packet.md` and feed a bounded digest into planning and plan critique | `ResearchBriefInput` → `ResearchAgentOutput` |
| **Planning Agent (class)** | `blog_planning_agent/` | [standalone] | Alternative planning implementation with its own refine loop. Not wired into v2; planning uses `BlogWriterAgent.plan_content()` instead | `PlanningInput` → `PlanningPhaseResult` |
| **Publication Agent** | `blog_publication_agent/` | [standalone] | Draft submission and platform formatting (Medium, dev.to, Substack). `run_pipeline()` writes the `publishing_pack.json` artifact directly and does not call this agent | `SubmitDraftInput` → `ApprovalResult` |
| **Medium Stats Agent** | `blog_medium_stats_agent/` | [standalone] | Playwright automation against `medium.com/me/stats`, reusing the shared Google browser login when configured | `MediumStatsRunConfig` → `MediumStatsReport` |

---

## 6. API Surface

The blogging team mounts at `/api/blogging`. Routes live in `api/main.py`.

| Category | Endpoints | Purpose |
|----------|-----------|---------|
| **Pipeline** | `POST /full-pipeline` (sync), `POST /full-pipeline-async` | Trigger full pipeline execution |
| **Jobs** | `GET /jobs`, `GET /job/{job_id}`, `DELETE /job/{job_id}`, `POST /job/{job_id}/restart`, `POST /job/{job_id}/resume`, `POST /job/{job_id}/cancel` | List and manage async jobs |
| **Streaming** | `GET /job/{job_id}/stream` (SSE) | Real-time progress events |
| **Collaboration** | `POST /job/{job_id}/select-title`, `POST /job/{job_id}/rate-titles`, `POST /job/{job_id}/draft-feedback`, `POST /job/{job_id}/story-response`, `POST /job/{job_id}/skip-story-gap`, `POST /job/{job_id}/answers` | Human-in-the-loop inputs |
| **Approval** | `POST /job/{job_id}/approve`, `POST /job/{job_id}/unapprove` | Final draft approval |
| **Artifacts** | `GET /job/{job_id}/artifacts`, `GET /job/{job_id}/artifacts/{artifact_name}` | Browse and download pipeline artifacts |
| **Story bank** | `GET /stories`, `GET /stories/{story_id}`, `DELETE /stories/{story_id}`, `GET /stories/search/{keywords}` | Cross-post personal-anecdote reuse |
| **Analytics** | `POST /medium-stats`, `POST /medium-stats-async` | Medium.com stats collection |
| **Health** | `GET /health` | Brand spec configuration status |

---

## 7. Execution Modes

`POST /full-pipeline-async` picks one of two runtime modes based on whether Temporal is configured (see `shared/run_pipeline_job.py`):

| Mode | Trigger | Characteristics |
|------|---------|-----------------|
| **Thread Mode** (fallback) | `TEMPORAL_ADDRESS` not set | A daemon Python thread runs `run_blog_full_pipeline_job()` in-process; job state lives in Postgres (via `blog_job_store`); progress is published to the in-memory SSE bus |
| **Temporal Mode** | `TEMPORAL_ADDRESS` is set | `BlogFullPipelineWorkflow` schedules `run_full_pipeline_activity`; `schedule_to_close_timeout=12h`, `heartbeat_timeout=5m`, retry policy 3 attempts with 30s→2m exponential backoff. A background heartbeat thread calls `activity.heartbeat()` every 30s; state survives worker restarts |

Both modes call the same `run_blog_full_pipeline_job()` entry point, produce identical artifacts, and publish identical SSE events. The synchronous `POST /full-pipeline` endpoint always runs in-process on the request thread.
