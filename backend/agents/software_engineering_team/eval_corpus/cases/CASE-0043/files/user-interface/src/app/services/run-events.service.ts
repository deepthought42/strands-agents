import { Injectable } from '@angular/core';

/** A run has started. */
export interface RunStarted {
  type: 'started';
  runId: string;
}

/** A run has failed. */
export interface RunFailed {
  type: 'failed';
  runId: string;
  error: string;
}

export type RunEvent = RunStarted | RunFailed;

@Injectable({ providedIn: 'root' })
export class RunEventsService {
  describe(event: RunEvent): string {
    return `${event.runId}: ${event.error}`;
  }
}
