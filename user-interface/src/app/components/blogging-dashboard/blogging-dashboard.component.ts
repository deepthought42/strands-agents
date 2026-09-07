import { Component, inject, OnDestroy, OnInit, ViewChild } from '@angular/core';
import { ActivatedRoute } from '@angular/router';
import { CommonModule } from '@angular/common';
import { FormsModule } from '@angular/forms';
import { SafeHtml } from '@angular/platform-browser';
import { MatTabsModule } from '@angular/material/tabs';
import { MatCardModule } from '@angular/material/card';
import { Subject, Subscription, timer } from 'rxjs';
import { startWith, switchMap } from 'rxjs/operators';
import type { BlogJobStreamEvent } from '../../models';
import { BloggingApiService } from '../../services/blogging-api.service';
import { TeamAssistantApiService } from '../../services/team-assistant-api.service';
import type { TeamConversationSummary } from '../../models/team-assistant.model';
import { LoadingSpinnerComponent } from '../../shared/loading-spinner/loading-spinner.component';
import { ErrorMessageComponent } from '../../shared/error-message/error-message.component';
import { MatExpansionModule } from '@angular/material/expansion';
import { MatButtonModule } from '@angular/material/button';
import { MatIconModule } from '@angular/material/icon';
import { MatTooltipModule } from '@angular/material/tooltip';
import { MatProgressBarModule } from '@angular/material/progress-bar';
import { MatFormFieldModule } from '@angular/material/form-field';
import { MatInputModule } from '@angular/material/input';
import { TeamAssistantChatComponent } from '../team-assistant-chat/team-assistant-chat.component';
import { DashboardShellComponent } from '../../shared/dashboard-shell/dashboard-shell.component';
import { Router } from '@angular/router';
import { InlineBannerComponent } from '../../shared/inline-banner/inline-banner.component';
import { MarkdownRendererService } from '../../shared/markdown-renderer.service';
import { extractErrorDetail } from '../../shared/extract-error-detail';
import type {
  BlogJobListItem,
  BlogJobStatusResponse,
  ArtifactMeta,
} from '../../models';

/**
 * Blogging API dashboard: research-and-review and full-pipeline forms and results.
 * Shows Jobs panel (running and completed) with job details and produced assets.
 */
const TERMINAL_STATUSES = ['completed', 'needs_human_review', 'failed', 'interrupted'] as const;
const POLL_JOBS_MS = 60000;
const POLL_STATUS_MS = 60000;

/** Pipeline phases in execution order for the phase stepper. Title selection now
 * runs at the end of planning (before the draft is written), matching the
 * backend's PHASE_ORDER in shared/models.py. */
const PIPELINE_PHASES = [
  { key: 'research', label: 'Research' },
  { key: 'planning', label: 'Planning' },
  { key: 'story_elicitation', label: 'Stories' },
  { key: 'title_selection', label: 'Title' },
  { key: 'draft_initial', label: 'Draft' },
  { key: 'draft_review', label: 'Review' },
  { key: 'copy_edit', label: 'Copy Edit' },
  { key: 'fact_check', label: 'Fact Check' },
  { key: 'compliance', label: 'Compliance' },
  { key: 'rewrite', label: 'Rewrite' },
  { key: 'finalize', label: 'Finalize' },
] as const;

export function artifactLabel(name: string): string {
  const labels: Record<string, string> = {
    'brand_spec_prompt.md': 'Brand spec prompt',
    'content_brief.md': 'Content brief',
    'content_plan.json': 'Content plan (JSON)',
    'content_plan.md': 'Content plan',
    'outline.md': 'Outline',
    'draft_v1.md': 'Draft v1',
    'draft_v2.md': 'Draft v2',
    'final.md': 'Final draft',
    'compliance_report.json': 'Compliance report',
    'fact_check_report.json': 'Fact check report',
    'validator_report.json': 'Validator report',
    'publishing_pack.json': 'Publishing pack',
    'medium_stats_report.json': 'Medium statistics report',
  };
  return labels[name] ?? name;
}

@Component({
  selector: 'app-blogging-dashboard',
  standalone: true,
  imports: [
    CommonModule,
    FormsModule,
    MatTabsModule,
    MatCardModule,
    MatExpansionModule,
    MatButtonModule,
    MatIconModule,
    MatTooltipModule,
    MatProgressBarModule,
    MatFormFieldModule,
    MatInputModule,
    LoadingSpinnerComponent,
    ErrorMessageComponent,
    TeamAssistantChatComponent,
    DashboardShellComponent,
    InlineBannerComponent,
  ],
  templateUrl: './blogging-dashboard.component.html',
  styleUrl: './blogging-dashboard.component.scss',
})
export class BloggingDashboardComponent implements OnInit, OnDestroy {
  @ViewChild(TeamAssistantChatComponent) private assistantChat?: TeamAssistantChatComponent;
  readonly artifactLabel = artifactLabel;
  private readonly api = inject(BloggingApiService);
  private readonly assistantApi = inject(TeamAssistantApiService);
  private readonly route = inject(ActivatedRoute);
  private readonly router = inject(Router);
  private readonly markdownRenderer = inject(MarkdownRendererService);
  private jobsSub: Subscription | null = null;
  private statusPollSub: Subscription | null = null;
  private sseSub: Subscription | null = null;
  private queryParamsSub: Subscription | null = null;
  private pendingJobId: string | null = null;
  private refreshTrigger$ = new Subject<void>();

  private static readonly ASSISTANT_URL = '/api/blogging/assistant';

  loading = false;
  error: string | null = null;

  /** Current conversation being viewed or edited. */
  currentConversationId: string | null = null;
  /** Draft conversations not yet linked to a job. */
  draftConversations: TeamConversationSummary[] = [];

  /** Current dashboard view. */
  activeView: 'empty' | 'new-post' | 'jobs' | 'job-detail' = 'empty';

  allJobs: BlogJobListItem[] = [];
  runningJobs: BlogJobListItem[] = [];
  completedJobs: BlogJobListItem[] = [];
  selectedBlogJob: BlogJobListItem | null = null;
  selectedJobStatus: BlogJobStatusResponse | null = null;
  selectedJobArtifacts: ArtifactMeta[] = [];
  artifactsLoading = false;
  artifactsError: string | null = null;
  artifactContent: Record<string, string | object> = {};
  artifactContentLoading: Record<string, boolean> = {};
  /** From GET /health when `brand_spec_configured` is true — hides audience/tone on the form. */
  blogBrandSpecConfigured = false;

  /** Fields shown in the new-post form. When brand spec is configured, audience/tone are pre-filled by the brand. */
  get newPostFields() {
    const fields = [
      { key: 'brief', label: 'Blog brief', placeholder: 'What should the post be about?', required: true },
    ];
    if (!this.blogBrandSpecConfigured) {
      fields.push(
        { key: 'audience', label: 'Target audience', placeholder: 'Who is this for?', required: false },
        { key: 'tone_or_purpose', label: 'Tone / purpose', placeholder: 'e.g. educational, persuasive, casual', required: false },
      );
    }
    fields.push(
      { key: 'content_profile', label: 'Content profile', placeholder: 'short_listicle, standard_article, or technical_deep_dive', required: false },
    );
    return fields;
  }
  viewArtifactModal: { name: string; content: string | object } | null = null;
  viewArtifactLoading = false;
  viewArtifactError: string | null = null;

  // Collaboration: title selection
  collaborationError: string | null = null;

  // Collaboration: story elicitation
  storyResponseText = '';
  storySubmitting = false;

  // Collaboration: Q&A
  qaAnswers: Record<string, string> = {};
  qaSubmitting = false;

  // Collaboration: draft review
  draftFeedbackText = '';
  draftFeedbackSubmitting = false;

  readonly pipelinePhases = PIPELINE_PHASES;

  isTerminalStatus(status: string): boolean {
    return (TERMINAL_STATUSES as readonly string[]).includes(status);
  }

  isPhaseComplete(phaseKey: string): boolean {
    if (!this.selectedJobStatus?.phase) return false;
    if (this.isTerminalStatus(this.selectedJobStatus.status) && this.selectedJobStatus.status !== 'failed') return true;
    const currentIdx = PIPELINE_PHASES.findIndex((p) => p.key === this.selectedJobStatus!.phase);
    const checkIdx = PIPELINE_PHASES.findIndex((p) => p.key === phaseKey);
    return checkIdx >= 0 && currentIdx > checkIdx;
  }

  canStopSelectedJob(): boolean {
    const status = this.selectedJobStatus?.status ?? this.selectedBlogJob?.status;
    return !!this.selectedBlogJob && !!status && !this.isTerminalStatus(status) && status !== 'cancelled';
  }

  cancelSelectedJob(): void {
    if (!this.selectedBlogJob) return;
    const jobId = this.selectedBlogJob.job_id;
    this.api.cancelJob(jobId).subscribe({
      next: () => {
        this.api.getJobStatus(jobId).subscribe({
          next: (status) => {
            this.selectedJobStatus = status;
            this.triggerJobsRefresh();
          },
        });
      },
      error: (err) => {
        this.error = extractErrorDetail(err, 'Failed to cancel job');
      },
    });
  }

  deleteSelectedJob(): void {
    if (!this.selectedBlogJob) return;
    if (!confirm('Delete this job? This cannot be undone.')) return;
    const jobId = this.selectedBlogJob.job_id;
    this.api.deleteJob(jobId).subscribe({
      next: () => {
        this.clearSelection();
        this.triggerJobsRefresh();
      },
      error: (err) => {
        this.error = extractErrorDetail(err, 'Failed to delete job');
      },
    });
  }

  ngOnInit(): void {
    this.api.health().subscribe({
      next: (h) => {
        this.blogBrandSpecConfigured = h.brand_spec_configured === true;
      },
      error: () => {
        this.blogBrandSpecConfigured = false;
      },
    });

    this.queryParamsSub = this.route.queryParams.subscribe((params) => {
      const id = params['jobId'];
      if (id) this.pendingJobId = id;
    });
    this.jobsSub = this.refreshTrigger$.pipe(
      startWith(undefined as void),
      switchMap(() => timer(0, POLL_JOBS_MS)),
      switchMap(() => this.api.getJobs(false)),
    ).subscribe({
      next: (jobs) => {
        this.allJobs = jobs;
        this.runningJobs = jobs.filter((j) => j.status === 'pending' || j.status === 'running');
        this.completedJobs = jobs.filter((j) => this.isTerminalStatus(j.status));
        if (this.pendingJobId != null) {
          const job = jobs.find((j) => j.job_id === this.pendingJobId);
          if (job) this.selectJob(job);
          this.pendingJobId = null;
        } else if (this.selectedBlogJob) {
          const still = jobs.find((j) => j.job_id === this.selectedBlogJob!.job_id);
          if (!still) this.clearSelection();
        }
        // Set initial view based on whether jobs or drafts exist (no auto-selection)
        if (this.activeView === 'empty' && !this.selectedBlogJob) {
          this.activeView = (jobs.length > 0 || this.draftConversations.length > 0) ? 'jobs' : 'empty';
        }
      },
    });

    // Load draft conversations (unlinked to any job)
    this.loadDraftConversations();
  }

  private loadDraftConversations(): void {
    this.assistantApi.listUnlinkedConversations(BloggingDashboardComponent.ASSISTANT_URL).subscribe({
      next: (resp) => {
        this.draftConversations = resp.conversations ?? [];
        // Update view if we're on empty but have drafts
        if (this.activeView === 'empty' && this.draftConversations.length > 0) {
          this.activeView = 'jobs';
        }
      },
    });
  }

  ngOnDestroy(): void {
    this.queryParamsSub?.unsubscribe();
    this.jobsSub?.unsubscribe();
    this.refreshTrigger$.complete();
    this.stopJobStreaming();
  }

  showNewPost(): void {
    this.clearSelection();
    this.currentConversationId = null;
    // Create a new conversation, then switch to the form view
    this.assistantApi.createConversation(BloggingDashboardComponent.ASSISTANT_URL).subscribe({
      next: (resp) => {
        this.currentConversationId = resp.conversation_id;
        this.activeView = 'new-post';
      },
      error: () => {
        // Fallback: show the form without a tracked conversation (singleton)
        this.activeView = 'new-post';
      },
    });
  }

  resumeConversation(conv: TeamConversationSummary): void {
    this.clearSelection();
    this.currentConversationId = conv.conversation_id;
    this.activeView = 'new-post';
  }

  deleteDraftConversation(convId: string): void {
    if (!confirm('Delete this draft? This cannot be undone.')) return;
    this.assistantApi.deleteConversation(BloggingDashboardComponent.ASSISTANT_URL, convId).subscribe({
      next: () => {
        this.draftConversations = this.draftConversations.filter((c) => c.conversation_id !== convId);
        if (this.currentConversationId === convId) {
          this.currentConversationId = null;
        }
      },
      error: (err) => {
        this.error = extractErrorDetail(err, 'Failed to delete draft');
      },
    });
  }

  onConversationLoaded(conversationId: string): void {
    this.currentConversationId = conversationId;
  }

  showJobs(): void {
    this.clearSelection();
    this.activeView = 'jobs';
    this.loadDraftConversations();
  }

  getTimeAgo(createdAt?: string): string {
    if (!createdAt) return '';
    const created = new Date(createdAt);
    const now = new Date();
    const diffMs = now.getTime() - created.getTime();
    const diffMins = Math.floor(diffMs / 60000);
    if (diffMins < 1) return 'Just now';
    if (diffMins < 60) return `${diffMins}m ago`;
    const diffHours = Math.floor(diffMins / 60);
    if (diffHours < 24) return `${diffHours}h ago`;
    const diffDays = Math.floor(diffHours / 24);
    return `${diffDays}d ago`;
  }

  cancelJobFromList(jobId: string): void {
    this.api.cancelJob(jobId).subscribe({
      next: () => { this.triggerJobsRefresh(); },
      error: (err) => { this.error = extractErrorDetail(err, 'Failed to cancel job'); },
    });
  }

  deleteJobFromList(jobId: string): void {
    if (!confirm('Delete this job? This cannot be undone.')) return;
    this.api.deleteJob(jobId).subscribe({
      next: () => {
        if (this.selectedBlogJob?.job_id === jobId) this.clearSelection();
        this.triggerJobsRefresh();
      },
      error: (err) => { this.error = extractErrorDetail(err, 'Failed to delete job'); },
    });
  }

  /**
   * Handle a launch that went through the backend `/assistant/launch` endpoint.
   * The backend already linked the conversation to the job; we just navigate
   * to the jobs view, auto-select the new job, and refresh the list once the
   * job row has had time to persist.
   */
  onWorkflowLaunched(event: { job_id: string | null; conversation_id: string }): void {
    if (!event.job_id) return;
    // The conversation is no longer a draft — backend already linked it.
    this.draftConversations = this.draftConversations.filter(
      (c) => c.conversation_id !== event.conversation_id,
    );
    this.pendingJobId = event.job_id;
    this.activeView = 'jobs';
    // Small delay so the backend has time to persist the new job record
    setTimeout(() => this.triggerJobsRefresh(), 500);
  }

  private clearSelection(): void {
    this.selectedBlogJob = null;
    this.selectedJobStatus = null;
    this.currentConversationId = null;
    this.stopJobStreaming();
    this.selectedJobArtifacts = [];
    this.artifactContent = {};
    this.artifactContentLoading = {};
    this.artifactsError = null;
    this.collaborationError = null;
    this.storyResponseText = '';
    this.qaAnswers = {};
    this.draftFeedbackText = '';
  }

  /** Immediately refresh the jobs list and reset the polling timer. */
  private triggerJobsRefresh(): void {
    this.refreshTrigger$.next();
  }

  /** Tear down both SSE and polling subscriptions. */
  private stopJobStreaming(): void {
    this.sseSub?.unsubscribe();
    this.sseSub = null;
    this.statusPollSub?.unsubscribe();
    this.statusPollSub = null;
  }

  /** Fall back to the original timer-based polling for a job. */
  private startPollingFallback(jobId: string): void {
    this.statusPollSub?.unsubscribe();
    let lastPhase: string | undefined;
    this.statusPollSub = timer(0, POLL_STATUS_MS).pipe(
      switchMap(() => this.api.getJobStatus(jobId))
    ).subscribe({
      next: (status) => {
        this.selectedJobStatus = status;
        // Refresh artifacts when phase changes or job finishes
        if (status.phase !== lastPhase || this.isTerminalStatus(status.status)) {
          lastPhase = status.phase;
          if (this.selectedBlogJob?.job_id === jobId) {
            this.loadArtifactsList(jobId);
          }
        }
      },
    });
  }

  selectJob(job: BlogJobListItem): void {
    this.activeView = 'job-detail';
    this.selectedBlogJob = job;
    this.selectedJobArtifacts = [];
    this.artifactContent = {};
    this.artifactContentLoading = {};
    this.artifactsError = null;
    this.currentConversationId = null;
    this.stopJobStreaming();

    // Load associated conversation for this job
    this.assistantApi.getConversationByJob(BloggingDashboardComponent.ASSISTANT_URL, job.job_id).subscribe({
      next: (state) => { this.currentConversationId = state.conversation_id; },
      error: () => { this.currentConversationId = null; }, // No conversation for this job
    });

    // Always load artifacts immediately (they accumulate as the pipeline runs)
    this.loadArtifactsList(job.job_id);

    // For terminal jobs, just fetch once — no need for streaming or polling.
    if (this.isTerminalStatus(job.status)) {
      this.api.getJobStatus(job.job_id).subscribe({
        next: (status) => { this.selectedJobStatus = status; },
      });
      return;
    }

    // Try SSE first, fall back to polling on error.
    this.sseSub = this.api.streamJobStatus(job.job_id).subscribe({
      next: (event: BlogJobStreamEvent) => {
        if (this.selectedBlogJob?.job_id !== job.job_id) return;
        this.applyStreamEvent(event, job.job_id);
      },
      error: () => {
        // SSE failed — fall back to polling
        if (this.selectedBlogJob?.job_id === job.job_id) {
          this.startPollingFallback(job.job_id);
        }
      },
    });
  }

  /** Apply a single SSE event to the component state. */
  private applyStreamEvent(event: BlogJobStreamEvent, jobId: string): void {
    if (event.type === 'snapshot') {
      this.selectedJobStatus = event as unknown as BlogJobStatusResponse;
      this.loadArtifactsList(jobId);
      return;
    }

    if (event.type === 'done') {
      this.api.getJobStatus(jobId).subscribe({
        next: (status) => {
          if (this.selectedBlogJob?.job_id === jobId) {
            this.selectedJobStatus = status;
            this.loadArtifactsList(jobId);
          }
        },
      });
      return;
    }

    if (event.type === 'update' && this.selectedJobStatus) {
      const patch: Record<string, unknown> = { ...event };
      delete patch['type'];
      delete patch['ts'];
      Object.assign(this.selectedJobStatus, patch);

      // Refresh artifacts when the phase changes (new artifacts may have been created)
      if (event.phase) {
        this.loadArtifactsList(jobId);
      }
    }
  }

  private _artifactsLoadPending = false;

  private loadArtifactsList(jobId: string): void {
    if (this.selectedBlogJob?.job_id !== jobId) return;
    // Debounce: skip if a load is already in flight
    if (this._artifactsLoadPending) return;
    this._artifactsLoadPending = true;
    // Only show loading spinner if we have no artifacts yet
    if (!this.selectedJobArtifacts.length) {
      this.artifactsLoading = true;
    }
    this.artifactsError = null;
    this.api.getJobArtifacts(jobId).subscribe({
      next: (res) => {
        this._artifactsLoadPending = false;
        if (this.selectedBlogJob?.job_id === jobId) {
          this.selectedJobArtifacts = res.artifacts ?? [];
          this.artifactsLoading = false;
        }
      },
      error: (err) => {
        this._artifactsLoadPending = false;
        if (this.selectedBlogJob?.job_id === jobId) {
          this.artifactsError = extractErrorDetail(err, 'Failed to load artifacts');
          this.artifactsLoading = false;
        }
      },
    });
  }

  loadArtifactContent(artifactName: string): void {
    const jobId = this.selectedBlogJob?.job_id;
    if (!jobId || this.artifactContent[artifactName] !== undefined) return;
    this.artifactContentLoading[artifactName] = true;
    this.api.getJobArtifactContent(jobId, artifactName).subscribe({
      next: (res) => {
        this.artifactContent[artifactName] = res.content;
        this.artifactContentLoading[artifactName] = false;
      },
      error: () => {
        this.artifactContentLoading[artifactName] = false;
      },
    });
  }

  getArtifactContentDisplay(name: string): string {
    const content = this.artifactContent[name];
    if (content === undefined) return '';
    if (typeof content === 'string') return content;
    return JSON.stringify(content, null, 2);
  }

  isArtifactJson(name: string): boolean {
    return name.endsWith('.json');
  }

  isArtifactMarkdown(name: string): boolean {
    return name.endsWith('.md');
  }

  isArtifactYaml(name: string): boolean {
    return name.endsWith('.yaml') || name.endsWith('.yml');
  }

  openAssetInNewTab(artifactName: string): void {
    const jobId = this.selectedBlogJob?.job_id;
    if (!jobId) return;
    const url = this.router.serializeUrl(
      this.router.createUrlTree(['/blogging/jobs', jobId, 'artifacts', artifactName])
    );
    window.open(url, '_blank', 'noopener');
  }

  getArtifactDownloadUrl(artifactName: string): string {
    const jobId = this.selectedBlogJob?.job_id;
    if (!jobId) return '#';
    return this.api.getJobArtifactDownloadUrl(jobId, artifactName);
  }

  openViewModal(artifactName: string): void {
    const jobId = this.selectedBlogJob?.job_id;
    if (!jobId) return;
    this.viewArtifactModal = null;
    this.viewArtifactError = null;
    this.viewArtifactLoading = true;
    this.api.getJobArtifactContent(jobId, artifactName).subscribe({
      next: (res) => {
        this.viewArtifactModal = { name: res.name, content: res.content };
        this.viewArtifactLoading = false;
      },
      error: (err) => {
        this.viewArtifactError = extractErrorDetail(err, 'Failed to load artifact');
        this.viewArtifactLoading = false;
      },
    });
  }

  closeViewModal(): void {
    this.viewArtifactModal = null;
    this.viewArtifactError = null;
  }

  getViewModalDisplayContent(): string {
    if (!this.viewArtifactModal) return '';
    const content = this.viewArtifactModal.content;
    if (typeof content === 'string') return content;
    return JSON.stringify(content, null, 2);
  }

  getViewModalMarkdownHtml(): SafeHtml {
    if (!this.viewArtifactModal || !this.isArtifactMarkdown(this.viewArtifactModal.name)) return '';
    const content = this.viewArtifactModal.content;
    const text = typeof content === 'string' ? content : JSON.stringify(content, null, 2);
    return this.markdownRenderer.renderToSafeHtml(text);
  }

  jobApprovedLabel(): string {
    if (!this.selectedJobStatus?.approved_at) return 'No';
    return 'Yes';
  }

  canApproveJob(): boolean {
    const status = this.selectedJobStatus?.status ?? this.selectedBlogJob?.status;
    return status === 'completed' || status === 'needs_human_review';
  }

  canUnapproveJob(): boolean {
    return !!this.selectedJobStatus?.approved_at;
  }

  approveSelectedJob(): void {
    if (!this.selectedBlogJob) return;
    this.api.approveJob(this.selectedBlogJob.job_id).subscribe({
      next: (status) => {
        this.selectedJobStatus = status;
        this.error = null;
        this.triggerJobsRefresh();
      },
      error: (err) => {
        this.error = extractErrorDetail(err, 'Failed to approve job');
      },
    });
  }

  unapproveSelectedJob(): void {
    if (!this.selectedBlogJob) return;
    this.api.unapproveJob(this.selectedBlogJob.job_id).subscribe({
      next: (status) => {
        this.selectedJobStatus = status;
        this.error = null;
        this.triggerJobsRefresh();
      },
      error: (err) => {
        this.error = extractErrorDetail(err, 'Failed to unapprove job');
      },
    });
  }

  // ---------------------------------------------------------------------------
  // Collaboration: title selection (dislike / like / love)
  // ---------------------------------------------------------------------------

  titleRatings: Record<string, 'dislike' | 'like' | 'love'> = {};
  titleRatingSubmitting = false;

  rateTitle(title: string, rating: 'dislike' | 'like' | 'love'): void {
    this.titleRatings[title] = rating;
    if (rating === 'love') {
      this.selectTitle(title);
    } else {
      this.submitSingleTitleRating(title, rating);
    }
  }

  getTitleRating(title: string): 'dislike' | 'like' | 'love' | undefined {
    return this.titleRatings[title];
  }

  /** Submit a single like/dislike rating. The pipeline generates a replacement title;
   *  SSE/polling will deliver the updated title_choices. Titles stay visible. */
  submitSingleTitleRating(title: string, rating: 'dislike' | 'like'): void {
    const jobId = this.selectedBlogJob?.job_id;
    if (!jobId || this.titleRatingSubmitting) return;
    this.titleRatingSubmitting = true;
    this.collaborationError = null;
    this.api.rateTitles(jobId, [{ title, rating }]).subscribe({
      next: () => {
        // Don't replace selectedJobStatus — titles stay visible.
        // The pipeline will generate a replacement and push updated
        // title_choices via SSE/polling.
        this.titleRatingSubmitting = false;
      },
      error: (err) => {
        this.collaborationError = extractErrorDetail(err, 'Failed to submit title rating');
        this.titleRatingSubmitting = false;
      },
    });
  }

  /** Select a loved title directly, advancing the pipeline. */
  selectTitle(title: string): void {
    const jobId = this.selectedBlogJob?.job_id;
    if (!jobId || this.titleRatingSubmitting) return;
    this.titleRatingSubmitting = true;
    this.collaborationError = null;
    this.api.selectTitle(jobId, title).subscribe({
      next: (status) => {
        this.selectedJobStatus = status;
        this.titleRatings = {};
        this.titleRatingSubmitting = false;
      },
      error: (err) => {
        this.collaborationError = extractErrorDetail(err, 'Failed to submit title rating');
        this.titleRatingSubmitting = false;
      },
    });
  }

  // ---------------------------------------------------------------------------
  // Collaboration: story elicitation
  // ---------------------------------------------------------------------------

  submitStoryResponse(): void {
    const jobId = this.selectedBlogJob?.job_id;
    const msg = this.storyResponseText.trim();
    if (!jobId || !msg) return;
    this.storySubmitting = true;
    this.collaborationError = null;
    this.api.submitStoryResponse(jobId, msg).subscribe({
      next: (status) => {
        this.selectedJobStatus = status;
        this.storyResponseText = '';
        this.storySubmitting = false;
      },
      error: (err) => {
        this.collaborationError = extractErrorDetail(err, 'Failed to submit story response');
        this.storySubmitting = false;
      },
    });
  }

  skipStoryGap(): void {
    const jobId = this.selectedBlogJob?.job_id;
    if (!jobId) return;
    this.storySubmitting = true;
    this.collaborationError = null;
    this.api.skipStoryGap(jobId).subscribe({
      next: () => {
        // Don't replace selectedJobStatus — the pipeline will present the
        // next story gap (setting waiting_for_story_input back to true) and
        // SSE/polling will deliver the updated state with the new gap.
        this.storyResponseText = '';
        this.storySubmitting = false;
      },
      error: (err) => {
        this.collaborationError = extractErrorDetail(err, 'Failed to skip story gap');
        this.storySubmitting = false;
      },
    });
  }

  getCurrentStoryGap() {
    const status = this.selectedJobStatus;
    if (!status?.story_gaps?.length) return null;
    const idx = status.current_story_gap_index ?? 0;
    return status.story_gaps[idx] ?? null;
  }

  // NB: not identity-memoized on selectedJobStatus — the SSE 'update' handler
  // mutates that object in place (Object.assign), so its reference is stable while
  // story_chat_history / current_gap_round change. Recompute each call; the chat
  // list is small, and a stale cache would hide newly-arrived agent messages.
  getStoryAgentMessages(): NonNullable<BlogJobStatusResponse['story_chat_history']> {
    const status = this.selectedJobStatus;
    const round = status?.current_gap_round ?? 0;
    return (status?.story_chat_history ?? []).filter(
      (m) => m.gap_round === round || m.gap_round === undefined
    );
  }

  // ---------------------------------------------------------------------------
  // Collaboration: Q&A
  // ---------------------------------------------------------------------------

  submitQaAnswers(): void {
    const jobId = this.selectedBlogJob?.job_id;
    const questions = this.selectedJobStatus?.pending_questions ?? [];
    if (!jobId || !questions.length) return;
    this.qaSubmitting = true;
    this.collaborationError = null;
    const answers = questions.map((q) => ({
      question_id: q.id,
      selected_answer: this.qaAnswers[q.id] ?? '',
    }));
    this.api.submitBlogAnswers(jobId, answers).subscribe({
      next: (status) => {
        this.selectedJobStatus = status;
        this.qaAnswers = {};
        this.qaSubmitting = false;
      },
      error: (err) => {
        this.collaborationError = extractErrorDetail(err, 'Failed to submit answers');
        this.qaSubmitting = false;
      },
    });
  }

  allQaAnswered(): boolean {
    const questions = this.selectedJobStatus?.pending_questions ?? [];
    return questions.every((q) => !q.required || (this.qaAnswers[q.id] ?? '').trim().length > 0);
  }

  // ---------------------------------------------------------------------------
  // Collaboration: draft review
  // ---------------------------------------------------------------------------

  submitDraftFeedback(approved: boolean): void {
    const jobId = this.selectedBlogJob?.job_id;
    if (!jobId) return;
    this.draftFeedbackSubmitting = true;
    this.collaborationError = null;
    this.api.submitDraftFeedback(jobId, this.draftFeedbackText.trim(), approved).subscribe({
      next: (status) => {
        this.selectedJobStatus = status;
        this.draftFeedbackText = '';
        this.draftFeedbackSubmitting = false;
      },
      error: (err) => {
        this.collaborationError = extractErrorDetail(err, 'Failed to submit draft feedback');
        this.draftFeedbackSubmitting = false;
      },
    });
  }

}
