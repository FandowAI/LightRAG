import type * as workflowApi from '@/api/dataWorkflow'

export function workflowFixture(status: workflowApi.WorkflowJob['status'] = 'running'): workflowApi.WorkflowJob {
  return {
    schema_version: 1, job_id: 'distill-test', request_id: 'batch:test-batch',
    source: { type: 'batch', batch_id: 'test-batch' }, status, phase: 'processing',
    terminal: ['completed', 'completed_with_errors', 'failed', 'interrupted'].includes(status),
    can_start: status === 'ready', poll_after_ms: status === 'running' ? 10 : 0,
    created_at: '2026-10-22', updated_at: '2026-10-22', started_at: null, finished_at: null,
    error: null, progress: { unit: 'sources', percent: null, seen_sources: 3, accepted_sources: 2, rejected_sources: 1, processed_sources: null, committed_batch: null },
    result: null, artifacts: {}, links: {}
  }
}
