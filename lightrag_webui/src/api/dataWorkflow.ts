import { lightragAxios } from './lightrag'

const base = '/integrations/data-work-flow'

export type WorkflowJob = {
  schema_version: 1
  job_id: string
  request_id: string
  source: { type: 'directory' | 'batch'; batch_id?: string | null }
  status: 'preparing' | 'ready' | 'starting' | 'running' | 'completed' | 'completed_with_errors' | 'failed' | 'interrupted'
  phase: string
  terminal: boolean
  can_start: boolean
  poll_after_ms: number
  created_at: string
  updated_at: string
  started_at: string | null
  finished_at: string | null
  error: string | null
  progress: {
    unit: 'sources'
    percent: number | null
    seen_sources: number
    accepted_sources: number
    rejected_sources: number
    processed_sources: number | null
    committed_batch: number | null
  }
  result: { run_id: string; directory: string; status: string; summary: Record<string, unknown> } | null
  artifacts: Record<string, string>
  links: Record<string, string>
}

export type WorkflowConfiguration = { ready: boolean; issues: string[] }
export type WorkflowLogs = { text: string; offset: number; next_offset: number; has_more: boolean }

export const getWorkflowConfiguration = async (signal?: AbortSignal): Promise<WorkflowConfiguration> =>
  (await lightragAxios.get(`${base}/config`, { signal, timeout: 10000 })).data

export const submitBatchWorkflow = async (batchId: string, signal?: AbortSignal): Promise<WorkflowJob> =>
  (await lightragAxios.post(`${base}/submit`, { source: { type: 'batch', batch_id: batchId }, start: true }, { signal, timeout: 15000 })).data

export const getWorkflowJob = async (jobId: string, signal?: AbortSignal): Promise<WorkflowJob> =>
  (await lightragAxios.get(`${base}/jobs/${encodeURIComponent(jobId)}`, { signal, timeout: 15000 })).data

export const findBatchWorkflow = async (batchId: string, signal?: AbortSignal): Promise<WorkflowJob | null> => {
  const response = await lightragAxios.get(`${base}/jobs`, { params: { source_batch_id: batchId, limit: 1 }, signal, timeout: 15000 })
  return response.data.items[0] ?? null
}

export const getWorkflowLogs = async (jobId: string, offset: number, signal?: AbortSignal): Promise<WorkflowLogs> =>
  (await lightragAxios.get(`${base}/jobs/${encodeURIComponent(jobId)}/logs`, { params: { stream: 'stderr', offset, limit: 8192 }, signal, timeout: 15000 })).data
