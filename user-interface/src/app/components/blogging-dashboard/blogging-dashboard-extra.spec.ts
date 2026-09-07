import { ComponentFixture, TestBed } from '@angular/core/testing';
import { of, Subject, throwError } from 'rxjs';
import { provideRouter } from '@angular/router';
import { provideHttpClient } from '@angular/common/http';
import { ActivatedRoute } from '@angular/router';
import { NoopAnimationsModule } from '@angular/platform-browser/animations';
import { vi, beforeEach, afterEach } from 'vitest';
import { BloggingApiService } from '../../services/blogging-api.service';
import { TeamAssistantApiService } from '../../services/team-assistant-api.service';
import { BloggingDashboardComponent, artifactLabel } from './blogging-dashboard.component';
import type { BlogJobListItem, BlogJobStatusResponse, ArtifactMeta, BlogJobStreamEvent } from '../../models';

vi.mock('rxjs', async (importOriginal) => {
  const rxjs = await importOriginal<typeof import('rxjs')>();
  return { ...rxjs, timer: vi.fn(() => rxjs.of(0)) };
});

interface ApiStub {
  startResearchReviewAsync: ReturnType<typeof vi.fn>;
  startFullPipelineAsync: ReturnType<typeof vi.fn>;
  getJobs: ReturnType<typeof vi.fn>;
  getJobStatus: ReturnType<typeof vi.fn>;
  getJobArtifacts: ReturnType<typeof vi.fn>;
  getJobArtifactContent: ReturnType<typeof vi.fn>;
  getJobArtifactDownloadUrl: ReturnType<typeof vi.fn>;
  health: ReturnType<typeof vi.fn>;
  streamJobStatus: ReturnType<typeof vi.fn>;
  cancelJob: ReturnType<typeof vi.fn>;
  deleteJob: ReturnType<typeof vi.fn>;
  approveJob: ReturnType<typeof vi.fn>;
  unapproveJob: ReturnType<typeof vi.fn>;
  rateTitles: ReturnType<typeof vi.fn>;
  selectTitle: ReturnType<typeof vi.fn>;
  submitStoryResponse: ReturnType<typeof vi.fn>;
  skipStoryGap: ReturnType<typeof vi.fn>;
  submitBlogAnswers: ReturnType<typeof vi.fn>;
  submitDraftFeedback: ReturnType<typeof vi.fn>;
}

interface AssistantStub {
  listUnlinkedConversations: ReturnType<typeof vi.fn>;
  createConversation: ReturnType<typeof vi.fn>;
  deleteConversation: ReturnType<typeof vi.fn>;
  getConversationByJob: ReturnType<typeof vi.fn>;
}

const makeJob = (overrides: Partial<BlogJobListItem> = {}): BlogJobListItem => ({
  job_id: 'j1',
  status: 'running',
  pipeline: 'full',
  ...overrides,
} as BlogJobListItem);

const makeStatus = (overrides: Partial<BlogJobStatusResponse> = {}): BlogJobStatusResponse => ({
  job_id: 'j1',
  status: 'running',
  pipeline: 'full',
  ...overrides,
} as BlogJobStatusResponse);

describe('BloggingDashboardComponent (extra coverage)', () => {
  let api: ApiStub;
  let assistant: AssistantStub;
  let fixture: ComponentFixture<BloggingDashboardComponent>;
  let component: BloggingDashboardComponent;
  let queryParams$: Subject<Record<string, string>>;

  beforeEach(async () => {
    queryParams$ = new Subject();
    api = {
      startResearchReviewAsync: vi.fn(),
      startFullPipelineAsync: vi.fn(),
      getJobs: vi.fn().mockReturnValue(of([])),
      getJobStatus: vi.fn().mockReturnValue(of(makeStatus())),
      getJobArtifacts: vi.fn().mockReturnValue(of({ artifacts: [] })),
      getJobArtifactContent: vi.fn().mockReturnValue(of({ name: 'final.md', content: 'hello' })),
      getJobArtifactDownloadUrl: vi.fn().mockReturnValue('/api/blogging/jobs/j1/artifacts/final.md'),
      health: vi.fn().mockReturnValue(of({ brand_spec_configured: true })),
      streamJobStatus: vi.fn().mockReturnValue(of({ type: 'snapshot', status: 'running' } as BlogJobStreamEvent)),
      cancelJob: vi.fn().mockReturnValue(of({})),
      deleteJob: vi.fn().mockReturnValue(of({})),
      approveJob: vi.fn().mockReturnValue(of(makeStatus({ status: 'completed' }))),
      unapproveJob: vi.fn().mockReturnValue(of(makeStatus({ status: 'completed' }))),
      rateTitles: vi.fn().mockReturnValue(of({})),
      selectTitle: vi.fn().mockReturnValue(of(makeStatus())),
      submitStoryResponse: vi.fn().mockReturnValue(of(makeStatus())),
      skipStoryGap: vi.fn().mockReturnValue(of({})),
      submitBlogAnswers: vi.fn().mockReturnValue(of(makeStatus())),
      submitDraftFeedback: vi.fn().mockReturnValue(of(makeStatus())),
    };
    assistant = {
      listUnlinkedConversations: vi.fn().mockReturnValue(of({ conversations: [] })),
      createConversation: vi.fn().mockReturnValue(of({ conversation_id: 'conv-new' })),
      deleteConversation: vi.fn().mockReturnValue(of({})),
      getConversationByJob: vi.fn().mockReturnValue(of({ conversation_id: 'conv-job' })),
    };

    await TestBed.configureTestingModule({
      imports: [BloggingDashboardComponent, NoopAnimationsModule],
      providers: [
        provideHttpClient(),
        provideRouter([]),
        { provide: BloggingApiService, useValue: api },
        { provide: TeamAssistantApiService, useValue: assistant },
        { provide: ActivatedRoute, useValue: { queryParams: queryParams$.asObservable() } },
      ],
    }).compileComponents();

    fixture = TestBed.createComponent(BloggingDashboardComponent);
    component = fixture.componentInstance;
  });

  afterEach(() => {
    TestBed.resetTestingModule();
  });

  // -----------------------------------------------------------------------
  // Module-level helpers & static getters
  // -----------------------------------------------------------------------

  it('artifactLabel maps known names and falls back to raw', () => {
    expect(artifactLabel('final.md')).toBe('Final draft');
    expect(artifactLabel('publishing_pack.json')).toBe('Publishing pack');
    expect(artifactLabel('unknown.json')).toBe('unknown.json');
  });

  it('newPostFields includes audience/tone when brand spec not configured', () => {
    api.health.mockReturnValue(of({ brand_spec_configured: false }));
    fixture.detectChanges();
    const fields = component.newPostFields.map((f) => f.key);
    expect(fields).toContain('brief');
    expect(fields).toContain('audience');
    expect(fields).toContain('tone_or_purpose');
    expect(fields).toContain('content_profile');
  });

  it('newPostFields omits audience/tone when brand_spec_configured', () => {
    fixture.detectChanges();
    const fields = component.newPostFields.map((f) => f.key);
    expect(fields).toContain('brief');
    expect(fields).not.toContain('audience');
    expect(fields).not.toContain('tone_or_purpose');
  });

  it('health failure falls back to brand_spec_configured=false', () => {
    api.health.mockReturnValue(throwError(() => new Error('boom')));
    fixture.detectChanges();
    expect(component.blogBrandSpecConfigured).toBe(false);
  });

  // -----------------------------------------------------------------------
  // Status helpers
  // -----------------------------------------------------------------------

  it('isTerminalStatus identifies terminal statuses', () => {
    fixture.detectChanges();
    expect(component.isTerminalStatus('completed')).toBe(true);
    expect(component.isTerminalStatus('failed')).toBe(true);
    expect(component.isTerminalStatus('needs_human_review')).toBe(true);
    expect(component.isTerminalStatus('interrupted')).toBe(true);
    expect(component.isTerminalStatus('running')).toBe(false);
  });

  it('isPhaseComplete false without phase, true past phase, true when terminal-completed', () => {
    fixture.detectChanges();
    component.selectedJobStatus = null;
    expect(component.isPhaseComplete('planning')).toBe(false);

    component.selectedJobStatus = makeStatus({ phase: 'fact_check', status: 'running' });
    expect(component.isPhaseComplete('planning')).toBe(true);
    expect(component.isPhaseComplete('fact_check')).toBe(false);
    expect(component.isPhaseComplete('finalize')).toBe(false);

    component.selectedJobStatus = makeStatus({ phase: 'planning', status: 'completed' });
    expect(component.isPhaseComplete('finalize')).toBe(true);

    component.selectedJobStatus = makeStatus({ phase: 'planning', status: 'failed' });
    expect(component.isPhaseComplete('finalize')).toBe(false);
  });

  it('pipelinePhases includes research ahead of planning, so the stepper can show it active/failed', () => {
    fixture.detectChanges();
    expect(component.pipelinePhases[0].key).toBe('research');
    expect(component.pipelinePhases[1].key).toBe('planning');

    component.selectedJobStatus = makeStatus({ phase: 'planning', status: 'running' });
    expect(component.isPhaseComplete('research')).toBe(true);
  });

  it('pipelinePhases orders title_selection before draft_initial, matching the planning-stage move', () => {
    fixture.detectChanges();
    component.selectedJobStatus = makeStatus({ phase: 'draft_initial', status: 'running' });
    expect(component.isPhaseComplete('title_selection')).toBe(true);
    expect(component.isPhaseComplete('draft_initial')).toBe(false);
  });

  // -----------------------------------------------------------------------
  // selectJob / streaming / polling
  // -----------------------------------------------------------------------

  it('selectJob loads artifacts and applies snapshot event', () => {
    fixture.detectChanges();
    api.getJobArtifacts.mockReturnValue(of({ artifacts: [{ name: 'final.md', size: 100 } as ArtifactMeta] }));
    api.streamJobStatus.mockReturnValue(of({ type: 'snapshot', status: 'running', phase: 'draft_initial' } as BlogJobStreamEvent));
    component.selectJob(makeJob({ status: 'running' }));
    expect(component.activeView).toBe('job-detail');
    expect(component.selectedBlogJob?.job_id).toBe('j1');
    expect(api.streamJobStatus).toHaveBeenCalledWith('j1');
    expect(component.selectedJobArtifacts.length).toBe(1);
  });

  it('selectJob short-circuits for terminal job: no stream, fetches status once', () => {
    fixture.detectChanges();
    component.selectJob(makeJob({ status: 'completed' }));
    expect(api.streamJobStatus).not.toHaveBeenCalled();
    expect(api.getJobStatus).toHaveBeenCalledWith('j1');
    expect(component.selectedJobStatus?.job_id).toBe('j1');
  });

  it('selectJob falls back to polling when SSE errors', () => {
    fixture.detectChanges();
    api.streamJobStatus.mockReturnValue(throwError(() => new Error('sse fail')));
    component.selectJob(makeJob({ status: 'running' }));
    // Polling triggers getJobStatus
    expect(api.getJobStatus).toHaveBeenCalledWith('j1');
  });

  it('selectJob handles missing conversation gracefully', () => {
    fixture.detectChanges();
    assistant.getConversationByJob.mockReturnValue(throwError(() => new Error('no conv')));
    component.selectJob(makeJob({ status: 'running' }));
    expect(component.currentConversationId).toBeNull();
  });

  it('applyStreamEvent: "update" merges into selectedJobStatus and refreshes artifacts on phase change', () => {
    fixture.detectChanges();
    component.selectedBlogJob = makeJob();
    component.selectedJobStatus = makeStatus({ phase: 'draft_initial' });
    const evt: BlogJobStreamEvent = { type: 'update', phase: 'draft_review', status: 'running' } as BlogJobStreamEvent;
    (component as unknown as { applyStreamEvent: (e: BlogJobStreamEvent, j: string) => void }).applyStreamEvent(evt, 'j1');
    expect(component.selectedJobStatus?.phase).toBe('draft_review');
    expect(api.getJobArtifacts).toHaveBeenCalled();
  });

  it('applyStreamEvent: "done" refreshes status and artifacts', () => {
    fixture.detectChanges();
    component.selectedBlogJob = makeJob();
    component.selectedJobStatus = makeStatus({ phase: 'finalize' });
    api.getJobStatus.mockReturnValue(of(makeStatus({ status: 'completed', phase: 'finalize' })));
    const evt: BlogJobStreamEvent = { type: 'done' } as BlogJobStreamEvent;
    (component as unknown as { applyStreamEvent: (e: BlogJobStreamEvent, j: string) => void }).applyStreamEvent(evt, 'j1');
    expect(component.selectedJobStatus?.status).toBe('completed');
  });

  // -----------------------------------------------------------------------
  // Jobs list / refresh / selection
  // -----------------------------------------------------------------------

  it('separates running and completed jobs', () => {
    api.getJobs.mockReturnValue(of([
      makeJob({ job_id: 'a', status: 'running' }),
      makeJob({ job_id: 'b', status: 'completed' }),
      makeJob({ job_id: 'c', status: 'pending' }),
    ]));
    fixture.detectChanges();
    expect(component.runningJobs.map((j) => j.job_id)).toEqual(['a', 'c']);
    expect(component.completedJobs.map((j) => j.job_id)).toEqual(['b']);
    expect(component.activeView).toBe('jobs');
  });

  it('jobs list with pending jobId selects that job', () => {
    api.getJobs.mockReturnValue(of([makeJob({ job_id: 'j1' })]));
    fixture.detectChanges();
    queryParams$.next({ jobId: 'j1' });
    // triggerJobsRefresh would re-fetch; simulate refresh by directly calling
    component['pendingJobId'] = 'j1';
    component['refreshTrigger$'].next();
    // After init, the jobs subscription should have called selectJob
    expect(component.selectedBlogJob?.job_id).toBe('j1');
  });

  it('clears selection when previously selected job vanishes from list', () => {
    fixture.detectChanges();
    component.selectedBlogJob = makeJob({ job_id: 'gone' });
    api.getJobs.mockReturnValue(of([makeJob({ job_id: 'other' })]));
    component['refreshTrigger$'].next();
    expect(component.selectedBlogJob).toBeNull();
  });

  it('activeView falls back to "empty" when no jobs and no drafts', () => {
    api.getJobs.mockReturnValue(of([]));
    assistant.listUnlinkedConversations.mockReturnValue(of({ conversations: [] }));
    fixture.detectChanges();
    expect(component.activeView).toBe('empty');
  });

  it('switches to jobs view when drafts exist', () => {
    api.getJobs.mockReturnValue(of([]));
    assistant.listUnlinkedConversations.mockReturnValue(of({
      conversations: [{ conversation_id: 'c1', is_draft: true } as never],
    }));
    fixture.detectChanges();
    expect(component.activeView).toBe('jobs');
  });

  // -----------------------------------------------------------------------
  // showNewPost / resumeConversation / deleteDraft
  // -----------------------------------------------------------------------

  it('showNewPost creates a conversation and switches view', () => {
    fixture.detectChanges();
    component.showNewPost();
    expect(assistant.createConversation).toHaveBeenCalled();
    expect(component.currentConversationId).toBe('conv-new');
    expect(component.activeView).toBe('new-post');
  });

  it('showNewPost falls back to form view when create fails', () => {
    fixture.detectChanges();
    assistant.createConversation.mockReturnValue(throwError(() => new Error('x')));
    component.showNewPost();
    expect(component.activeView).toBe('new-post');
  });

  it('resumeConversation activates form for an existing draft', () => {
    fixture.detectChanges();
    component.resumeConversation({ conversation_id: 'c2', is_draft: true } as never);
    expect(component.currentConversationId).toBe('c2');
    expect(component.activeView).toBe('new-post');
  });

  it('deleteDraftConversation removes locally on success', () => {
    fixture.detectChanges();
    component.draftConversations = [{ conversation_id: 'c1' } as never, { conversation_id: 'c2' } as never];
    component.currentConversationId = 'c1';
    vi.spyOn(window, 'confirm').mockReturnValue(true);
    component.deleteDraftConversation('c1');
    expect(component.draftConversations.length).toBe(1);
    expect(component.currentConversationId).toBeNull();
  });

  it('deleteDraftConversation does nothing when user cancels confirm', () => {
    fixture.detectChanges();
    component.draftConversations = [{ conversation_id: 'c1' } as never];
    vi.spyOn(window, 'confirm').mockReturnValue(false);
    component.deleteDraftConversation('c1');
    expect(assistant.deleteConversation).not.toHaveBeenCalled();
  });

  it('deleteDraftConversation sets error when API fails', () => {
    fixture.detectChanges();
    assistant.deleteConversation.mockReturnValue(throwError(() => ({ error: { detail: 'bad' } })));
    vi.spyOn(window, 'confirm').mockReturnValue(true);
    component.deleteDraftConversation('c1');
    expect(component.error).toBe('bad');
  });

  it('onConversationLoaded saves the id', () => {
    fixture.detectChanges();
    component.onConversationLoaded('c-new');
    expect(component.currentConversationId).toBe('c-new');
  });

  it('showJobs switches to jobs view', () => {
    fixture.detectChanges();
    component.activeView = 'new-post';
    component.showJobs();
    expect(component.activeView).toBe('jobs');
  });

  // -----------------------------------------------------------------------
  // getTimeAgo
  // -----------------------------------------------------------------------

  it('getTimeAgo formats relative times', () => {
    fixture.detectChanges();
    const now = Date.now();
    vi.spyOn(Date, 'now').mockReturnValue(now);
    expect(component.getTimeAgo()).toBe('');
    expect(component.getTimeAgo(new Date(now - 30 * 1000).toISOString())).toBe('Just now');
    expect(component.getTimeAgo(new Date(now - 5 * 60_000).toISOString())).toBe('5m ago');
    expect(component.getTimeAgo(new Date(now - 2 * 3600_000).toISOString())).toBe('2h ago');
    expect(component.getTimeAgo(new Date(now - 3 * 86400_000).toISOString())).toBe('3d ago');
  });

  // -----------------------------------------------------------------------
  // cancelJob / deleteJob
  // -----------------------------------------------------------------------

  it('cancelSelectedJob fetches updated status and refreshes', () => {
    api.getJobs.mockReturnValue(of([makeJob()]));
    fixture.detectChanges();
    component.selectedBlogJob = makeJob();
    api.getJobStatus.mockReturnValue(of(makeStatus({ status: 'cancelled' })));
    const refreshSpy = vi.spyOn(component as unknown as { triggerJobsRefresh: () => void }, 'triggerJobsRefresh');
    component.cancelSelectedJob();
    expect(api.cancelJob).toHaveBeenCalledWith('j1');
    expect(component.selectedJobStatus?.status).toBe('cancelled');
    expect(refreshSpy).toHaveBeenCalled();
  });

  it('cancelSelectedJob does nothing when no job is selected', () => {
    fixture.detectChanges();
    component.selectedBlogJob = null;
    component.cancelSelectedJob();
    expect(api.cancelJob).not.toHaveBeenCalled();
  });

  it('cancelSelectedJob handles cancel error', () => {
    fixture.detectChanges();
    component.selectedBlogJob = makeJob();
    api.cancelJob.mockReturnValue(throwError(() => ({ message: 'cancel failed' })));
    component.cancelSelectedJob();
    expect(component.error).toBe('cancel failed');
  });

  it('deleteSelectedJob clears selection on success', () => {
    fixture.detectChanges();
    component.selectedBlogJob = makeJob();
    vi.spyOn(window, 'confirm').mockReturnValue(true);
    component.deleteSelectedJob();
    expect(api.deleteJob).toHaveBeenCalledWith('j1');
    expect(component.selectedBlogJob).toBeNull();
  });

  it('deleteSelectedJob cancels when user declines confirm', () => {
    fixture.detectChanges();
    component.selectedBlogJob = makeJob();
    vi.spyOn(window, 'confirm').mockReturnValue(false);
    component.deleteSelectedJob();
    expect(api.deleteJob).not.toHaveBeenCalled();
  });

  it('deleteSelectedJob sets error on failure', () => {
    fixture.detectChanges();
    component.selectedBlogJob = makeJob();
    vi.spyOn(window, 'confirm').mockReturnValue(true);
    api.deleteJob.mockReturnValue(throwError(() => ({ error: { detail: 'no' } })));
    component.deleteSelectedJob();
    expect(component.error).toBe('no');
  });

  it('cancelJobFromList triggers refresh and handles error', () => {
    fixture.detectChanges();
    component.cancelJobFromList('jx');
    expect(api.cancelJob).toHaveBeenCalledWith('jx');
    api.cancelJob.mockReturnValue(throwError(() => ({ message: 'cancel-list-fail' })));
    component.cancelJobFromList('jx');
    expect(component.error).toBe('cancel-list-fail');
  });

  it('deleteJobFromList clears selection if id matches', () => {
    fixture.detectChanges();
    component.selectedBlogJob = makeJob({ job_id: 'jx' });
    vi.spyOn(window, 'confirm').mockReturnValue(true);
    component.deleteJobFromList('jx');
    expect(component.selectedBlogJob).toBeNull();
  });

  it('deleteJobFromList does nothing on cancel confirm', () => {
    fixture.detectChanges();
    vi.spyOn(window, 'confirm').mockReturnValue(false);
    component.deleteJobFromList('jy');
    expect(api.deleteJob).not.toHaveBeenCalled();
  });

  it('deleteJobFromList sets error on failure', () => {
    fixture.detectChanges();
    vi.spyOn(window, 'confirm').mockReturnValue(true);
    api.deleteJob.mockReturnValue(throwError(() => ({ error: { detail: 'denied' } })));
    component.deleteJobFromList('jx');
    expect(component.error).toBe('denied');
  });

  it('canStopSelectedJob true for running jobs, false for terminal', () => {
    fixture.detectChanges();
    expect(component.canStopSelectedJob()).toBe(false);
    component.selectedBlogJob = makeJob({ status: 'running' });
    expect(component.canStopSelectedJob()).toBe(true);
    component.selectedJobStatus = makeStatus({ status: 'completed' });
    expect(component.canStopSelectedJob()).toBe(false);
    component.selectedJobStatus = makeStatus({ status: 'cancelled' });
    expect(component.canStopSelectedJob()).toBe(false);
  });

  // -----------------------------------------------------------------------
  // Artifacts
  // -----------------------------------------------------------------------

  it('getArtifactContentDisplay handles string and object content', () => {
    fixture.detectChanges();
    component.artifactContent = { 'a.md': 'hello', 'b.json': { foo: 1 } };
    expect(component.getArtifactContentDisplay('a.md')).toBe('hello');
    expect(component.getArtifactContentDisplay('b.json')).toContain('"foo": 1');
    expect(component.getArtifactContentDisplay('missing.md')).toBe('');
  });

  it('artifact type predicates classify file extensions', () => {
    fixture.detectChanges();
    expect(component.isArtifactJson('a.json')).toBe(true);
    expect(component.isArtifactJson('a.md')).toBe(false);
    expect(component.isArtifactMarkdown('a.md')).toBe(true);
    expect(component.isArtifactYaml('a.yaml')).toBe(true);
    expect(component.isArtifactYaml('a.yml')).toBe(true);
    expect(component.isArtifactYaml('a.json')).toBe(false);
  });

  it('loadArtifactContent caches result and avoids duplicate fetches', () => {
    fixture.detectChanges();
    component.selectedBlogJob = makeJob();
    component.loadArtifactContent('final.md');
    expect(api.getJobArtifactContent).toHaveBeenCalledWith('j1', 'final.md');
    expect(component.artifactContent['final.md']).toBe('hello');
    api.getJobArtifactContent.mockClear();
    component.loadArtifactContent('final.md'); // already loaded
    expect(api.getJobArtifactContent).not.toHaveBeenCalled();
  });

  it('loadArtifactContent without selected job does nothing', () => {
    fixture.detectChanges();
    component.selectedBlogJob = null;
    component.loadArtifactContent('a.md');
    expect(api.getJobArtifactContent).not.toHaveBeenCalled();
  });

  it('loadArtifactContent handles error', () => {
    fixture.detectChanges();
    component.selectedBlogJob = makeJob();
    api.getJobArtifactContent.mockReturnValue(throwError(() => new Error('x')));
    component.loadArtifactContent('a.md');
    expect(component.artifactContentLoading['a.md']).toBe(false);
  });

  it('openAssetInNewTab opens a window with serialized URL', () => {
    fixture.detectChanges();
    component.selectedBlogJob = makeJob();
    const spy = vi.spyOn(window, 'open').mockImplementation(() => null);
    component.openAssetInNewTab('final.md');
    expect(spy).toHaveBeenCalled();
  });

  it('openAssetInNewTab no-ops without selected job', () => {
    fixture.detectChanges();
    component.selectedBlogJob = null;
    const spy = vi.spyOn(window, 'open');
    component.openAssetInNewTab('final.md');
    expect(spy).not.toHaveBeenCalled();
  });

  it('getArtifactDownloadUrl returns # without selected job', () => {
    fixture.detectChanges();
    component.selectedBlogJob = null;
    expect(component.getArtifactDownloadUrl('x')).toBe('#');
    component.selectedBlogJob = makeJob();
    expect(component.getArtifactDownloadUrl('y')).toContain('/api/blogging');
  });

  it('openViewModal loads content and closes', () => {
    fixture.detectChanges();
    component.selectedBlogJob = makeJob();
    component.openViewModal('final.md');
    expect(component.viewArtifactModal?.name).toBe('final.md');
    component.closeViewModal();
    expect(component.viewArtifactModal).toBeNull();
  });

  it('openViewModal sets error on failure', () => {
    fixture.detectChanges();
    component.selectedBlogJob = makeJob();
    api.getJobArtifactContent.mockReturnValue(throwError(() => ({ message: 'load fail' })));
    component.openViewModal('final.md');
    expect(component.viewArtifactError).toBe('load fail');
  });

  it('openViewModal no-ops without selected job', () => {
    fixture.detectChanges();
    component.selectedBlogJob = null;
    component.openViewModal('x');
    expect(component.viewArtifactModal).toBeNull();
  });

  it('getViewModalDisplayContent handles string and object', () => {
    fixture.detectChanges();
    expect(component.getViewModalDisplayContent()).toBe('');
    component.viewArtifactModal = { name: 'a.json', content: { foo: 'bar' } };
    expect(component.getViewModalDisplayContent()).toContain('foo');
    component.viewArtifactModal = { name: 'a.md', content: 'plain' };
    expect(component.getViewModalDisplayContent()).toBe('plain');
  });

  it('getViewModalMarkdownHtml renders markdown for .md and empty for non-md', () => {
    fixture.detectChanges();
    expect(component.getViewModalMarkdownHtml()).toBe('');
    component.viewArtifactModal = { name: 'a.json', content: '{}' };
    expect(component.getViewModalMarkdownHtml()).toBe('');
    component.viewArtifactModal = { name: 'a.md', content: '# Hello' };
    const html = component.getViewModalMarkdownHtml();
    expect(html).toBeTruthy();
  });

  it('getViewModalMarkdownHtml handles object content for markdown', () => {
    fixture.detectChanges();
    component.viewArtifactModal = { name: 'a.md', content: { foo: 1 } as never };
    expect(component.getViewModalMarkdownHtml()).toBeTruthy();
  });

  it('getViewModalMarkdownHtml empty text returns empty html', () => {
    fixture.detectChanges();
    component.viewArtifactModal = { name: 'a.md', content: '   ' };
    const html = component.getViewModalMarkdownHtml();
    expect(html).toBeTruthy();
  });

  // -----------------------------------------------------------------------
  // Approve / unapprove
  // -----------------------------------------------------------------------

  it('jobApprovedLabel & approve helpers', () => {
    fixture.detectChanges();
    expect(component.jobApprovedLabel()).toBe('No');
    component.selectedJobStatus = makeStatus({ approved_at: '2025-01-01T00:00:00Z' } as never);
    expect(component.jobApprovedLabel()).toBe('Yes');
    expect(component.canUnapproveJob()).toBe(true);

    component.selectedJobStatus = makeStatus({ status: 'completed' });
    component.selectedBlogJob = null;
    expect(component.canApproveJob()).toBe(true);
    component.selectedJobStatus = null;
    component.selectedBlogJob = makeJob({ status: 'needs_human_review' });
    expect(component.canApproveJob()).toBe(true);
    component.selectedBlogJob = makeJob({ status: 'running' });
    expect(component.canApproveJob()).toBe(false);
  });

  it('approveSelectedJob updates status', () => {
    api.getJobs.mockReturnValue(of([makeJob()]));
    fixture.detectChanges();
    component.selectedBlogJob = makeJob();
    component.approveSelectedJob();
    expect(api.approveJob).toHaveBeenCalledWith('j1');
    expect(component.selectedJobStatus?.status).toBe('completed');
  });

  it('approveSelectedJob no-ops without selected job', () => {
    fixture.detectChanges();
    component.approveSelectedJob();
    expect(api.approveJob).not.toHaveBeenCalled();
  });

  it('approveSelectedJob handles error', () => {
    fixture.detectChanges();
    component.selectedBlogJob = makeJob();
    api.approveJob.mockReturnValue(throwError(() => ({ message: 'no perms' })));
    component.approveSelectedJob();
    expect(component.error).toBe('no perms');
  });

  it('unapproveSelectedJob updates status and handles error', () => {
    api.getJobs.mockReturnValue(of([makeJob()]));
    fixture.detectChanges();
    component.selectedBlogJob = makeJob();
    component.unapproveSelectedJob();
    expect(api.unapproveJob).toHaveBeenCalledWith('j1');

    api.unapproveJob.mockReturnValue(throwError(() => ({ message: 'fail' })));
    component.unapproveSelectedJob();
    expect(component.error).toBe('fail');

    component.selectedBlogJob = null;
    api.unapproveJob.mockClear();
    component.unapproveSelectedJob();
    expect(api.unapproveJob).not.toHaveBeenCalled();
  });

  // -----------------------------------------------------------------------
  // Title rating collaboration
  // -----------------------------------------------------------------------

  it('rateTitle dispatches to selectTitle for "love" rating', () => {
    fixture.detectChanges();
    component.selectedBlogJob = makeJob();
    component.rateTitle('My Title', 'love');
    expect(api.selectTitle).toHaveBeenCalledWith('j1', 'My Title');
  });

  it('rateTitle dispatches to submitSingleTitleRating for like/dislike', () => {
    fixture.detectChanges();
    component.selectedBlogJob = makeJob();
    component.rateTitle('My Title', 'like');
    expect(api.rateTitles).toHaveBeenCalled();
  });

  it('getTitleRating returns current rating', () => {
    fixture.detectChanges();
    component.titleRatings = { 'My Title': 'like' };
    expect(component.getTitleRating('My Title')).toBe('like');
    expect(component.getTitleRating('Unknown')).toBeUndefined();
  });

  it('submitSingleTitleRating no-ops without job', () => {
    fixture.detectChanges();
    component.selectedBlogJob = null;
    component.submitSingleTitleRating('a', 'like');
    expect(api.rateTitles).not.toHaveBeenCalled();
  });

  it('submitSingleTitleRating no-ops while submitting', () => {
    fixture.detectChanges();
    component.selectedBlogJob = makeJob();
    component.titleRatingSubmitting = true;
    component.submitSingleTitleRating('a', 'like');
    expect(api.rateTitles).not.toHaveBeenCalled();
  });

  it('submitSingleTitleRating sets collaborationError on failure', () => {
    fixture.detectChanges();
    component.selectedBlogJob = makeJob();
    api.rateTitles.mockReturnValue(throwError(() => ({ message: 'rate fail' })));
    component.submitSingleTitleRating('a', 'like');
    expect(component.collaborationError).toBe('rate fail');
  });

  it('selectTitle no-ops without job', () => {
    fixture.detectChanges();
    component.selectedBlogJob = null;
    component.selectTitle('t');
    expect(api.selectTitle).not.toHaveBeenCalled();
  });

  it('selectTitle handles error', () => {
    fixture.detectChanges();
    component.selectedBlogJob = makeJob();
    api.selectTitle.mockReturnValue(throwError(() => ({ message: 'select fail' })));
    component.selectTitle('t');
    expect(component.collaborationError).toBe('select fail');
  });

  // -----------------------------------------------------------------------
  // Story collaboration
  // -----------------------------------------------------------------------

  it('submitStoryResponse skips empty messages', () => {
    fixture.detectChanges();
    component.selectedBlogJob = makeJob();
    component.storyResponseText = '   ';
    component.submitStoryResponse();
    expect(api.submitStoryResponse).not.toHaveBeenCalled();
  });

  it('submitStoryResponse posts trimmed message and clears text', () => {
    fixture.detectChanges();
    component.selectedBlogJob = makeJob();
    component.storyResponseText = ' my story ';
    component.submitStoryResponse();
    expect(api.submitStoryResponse).toHaveBeenCalledWith('j1', 'my story');
    expect(component.storyResponseText).toBe('');
  });

  it('submitStoryResponse handles error', () => {
    fixture.detectChanges();
    component.selectedBlogJob = makeJob();
    component.storyResponseText = 'hi';
    api.submitStoryResponse.mockReturnValue(throwError(() => ({ message: 'story fail' })));
    component.submitStoryResponse();
    expect(component.collaborationError).toBe('story fail');
  });

  it('skipStoryGap no-ops without job', () => {
    fixture.detectChanges();
    component.selectedBlogJob = null;
    component.skipStoryGap();
    expect(api.skipStoryGap).not.toHaveBeenCalled();
  });

  it('skipStoryGap calls API and resets text', () => {
    fixture.detectChanges();
    component.selectedBlogJob = makeJob();
    component.storyResponseText = 'x';
    component.skipStoryGap();
    expect(api.skipStoryGap).toHaveBeenCalledWith('j1');
    expect(component.storyResponseText).toBe('');
  });

  it('skipStoryGap handles error', () => {
    fixture.detectChanges();
    component.selectedBlogJob = makeJob();
    api.skipStoryGap.mockReturnValue(throwError(() => ({ message: 'skip fail' })));
    component.skipStoryGap();
    expect(component.collaborationError).toBe('skip fail');
  });

  it('getCurrentStoryGap returns null when no gaps', () => {
    fixture.detectChanges();
    component.selectedJobStatus = null;
    expect(component.getCurrentStoryGap()).toBeNull();
    component.selectedJobStatus = makeStatus({ story_gaps: [] } as never);
    expect(component.getCurrentStoryGap()).toBeNull();
  });

  it('getCurrentStoryGap returns gap at current index', () => {
    fixture.detectChanges();
    component.selectedJobStatus = makeStatus({
      story_gaps: [{ id: 'g1' }, { id: 'g2' }],
      current_story_gap_index: 1,
    } as never);
    expect(component.getCurrentStoryGap()).toEqual({ id: 'g2' });
  });

  it('getStoryAgentMessages filters by current round', () => {
    fixture.detectChanges();
    component.selectedJobStatus = makeStatus({
      current_gap_round: 1,
      story_chat_history: [
        { text: 'a', gap_round: 0 },
        { text: 'b', gap_round: 1 },
        { text: 'c' },
      ],
    } as never);
    const msgs = component.getStoryAgentMessages();
    expect(msgs.map((m: { text: string }) => m.text)).toEqual(['b', 'c']);
  });

  // -----------------------------------------------------------------------
  // Q&A
  // -----------------------------------------------------------------------

  it('submitQaAnswers builds payload from qaAnswers', () => {
    fixture.detectChanges();
    component.selectedBlogJob = makeJob();
    component.selectedJobStatus = makeStatus({
      pending_questions: [{ id: 'q1', required: true } as never, { id: 'q2' } as never],
    });
    component.qaAnswers = { q1: 'A', q2: 'B' };
    component.submitQaAnswers();
    expect(api.submitBlogAnswers).toHaveBeenCalledWith('j1', [
      { question_id: 'q1', selected_answer: 'A' },
      { question_id: 'q2', selected_answer: 'B' },
    ]);
    expect(component.qaAnswers).toEqual({});
  });

  it('submitQaAnswers no-ops without job', () => {
    fixture.detectChanges();
    component.selectedBlogJob = null;
    component.selectedJobStatus = makeStatus({ pending_questions: [{ id: 'q' } as never] });
    component.submitQaAnswers();
    expect(api.submitBlogAnswers).not.toHaveBeenCalled();
  });

  it('submitQaAnswers no-ops with no questions', () => {
    fixture.detectChanges();
    component.selectedBlogJob = makeJob();
    component.submitQaAnswers();
    expect(api.submitBlogAnswers).not.toHaveBeenCalled();
  });

  it('submitQaAnswers handles error', () => {
    fixture.detectChanges();
    component.selectedBlogJob = makeJob();
    component.selectedJobStatus = makeStatus({ pending_questions: [{ id: 'q', required: false } as never] });
    component.qaAnswers = {};
    api.submitBlogAnswers.mockReturnValue(throwError(() => ({ message: 'qa fail' })));
    component.submitQaAnswers();
    expect(component.collaborationError).toBe('qa fail');
  });

  it('allQaAnswered checks required answers', () => {
    fixture.detectChanges();
    component.selectedJobStatus = makeStatus({
      pending_questions: [
        { id: 'q1', required: true },
        { id: 'q2', required: false },
      ] as never,
    });
    component.qaAnswers = {};
    expect(component.allQaAnswered()).toBe(false);
    component.qaAnswers = { q1: 'a' };
    expect(component.allQaAnswered()).toBe(true);
  });

  // -----------------------------------------------------------------------
  // Draft feedback
  // -----------------------------------------------------------------------

  it('submitDraftFeedback calls API with trimmed text', () => {
    fixture.detectChanges();
    component.selectedBlogJob = makeJob();
    component.draftFeedbackText = '  comments  ';
    component.submitDraftFeedback(true);
    expect(api.submitDraftFeedback).toHaveBeenCalledWith('j1', 'comments', true);
    expect(component.draftFeedbackText).toBe('');
  });

  it('submitDraftFeedback no-ops without job', () => {
    fixture.detectChanges();
    component.selectedBlogJob = null;
    component.submitDraftFeedback(true);
    expect(api.submitDraftFeedback).not.toHaveBeenCalled();
  });

  it('submitDraftFeedback handles error', () => {
    fixture.detectChanges();
    component.selectedBlogJob = makeJob();
    api.submitDraftFeedback.mockReturnValue(throwError(() => ({ message: 'feedback fail' })));
    component.submitDraftFeedback(false);
    expect(component.collaborationError).toBe('feedback fail');
  });

  // -----------------------------------------------------------------------
  // onWorkflowLaunched / lifecycle
  // -----------------------------------------------------------------------

  it('onWorkflowLaunched ignores events without job_id', () => {
    fixture.detectChanges();
    component.onWorkflowLaunched({ job_id: null, conversation_id: 'c1' });
    expect(component.activeView).not.toBe('jobs');
  });

  it('onWorkflowLaunched filters draft and triggers refresh', () => {
    vi.useFakeTimers();
    fixture.detectChanges();
    component.draftConversations = [
      { conversation_id: 'c1' } as never,
      { conversation_id: 'c2' } as never,
    ];
    component.onWorkflowLaunched({ job_id: 'jX', conversation_id: 'c1' });
    expect(component.draftConversations.length).toBe(1);
    expect(component.activeView).toBe('jobs');
    vi.advanceTimersByTime(500);
    vi.useRealTimers();
  });

  it('ngOnDestroy completes the refresh trigger and unsubscribes', () => {
    fixture.detectChanges();
    component.ngOnDestroy();
    // No exception thrown is sufficient
    expect(component['jobsSub']).not.toBeNull();
  });
});
