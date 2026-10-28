import { lightragAxios } from './lightrag'

const base = '/integrations/data-work-flow'

export type WorkflowJob = {
  schema_version: 1
  job_id: string
  request_id: string
  source: { type: 'directory' | 'batch'; batch_id?: string | null }
  status:
    | 'preparing'
    | 'ready'
    | 'starting'
    | 'running'
    | 'completed'
    | 'completed_with_errors'
    | 'failed'
    | 'interrupted'
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
  result: {
    run_id: string
    directory: string
    status: string
    summary: Record<string, unknown>
  } | null
  artifacts: Record<string, string>
  links: Record<string, string>
}

export type WorkflowConfiguration = { ready: boolean; issues: string[] }
export type WorkflowLogs = { text: string; offset: number; next_offset: number; has_more: boolean }
export type WorkflowStage = 'scenario' | 'qa' | 'classification' | 'inspection'
export const workflowStages: WorkflowStage[] = ['scenario', 'qa', 'classification', 'inspection']
export type StageValues = {
  model: string
  prompt: string
  parser: string
  prompt_file: string | null
}
export type ModelValues = Record<string, string | number | null>
export type ModelFieldSchema = {
  type?: string
  default?: string | number | null
  enum?: string[]
  minimum?: number
  maximum?: number
  exclusiveMinimum?: number
  anyOf?: ModelFieldSchema[]
}
export type RecipeValues = {
  questions_per_source: number
  deduplicate_qa: boolean
  scenario_instructions: string
  classification_instructions: string
  inspection_instructions: string
  seed_question_path: (string | number)[] | null
  batch_size: number
  max_inflight_requests: number
  max_prompt_chars: number | null
  log_level: 'DEBUG' | 'INFO' | 'WARNING' | 'ERROR' | 'CRITICAL'
  models: Record<string, ModelValues>
  stages: Record<WorkflowStage, StageValues>
}
export type RecipeSettingsSnapshot = {
  schema_version: number
  revision: string
  editable: boolean
  recipe_schema_version: number
  installed_schema_version: number
  installed_commit: string | null
  workflow_name: string
  settings: RecipeValues
  recipe_file: string
  settings_file: string
  models: Record<string, string>
  model_schema: { properties?: Record<string, ModelFieldSchema>; required?: string[] }
  task_schema: Record<string, unknown>
}
export type PromptDocument = {
  stage: WorkflowStage
  path: string
  filename: string
  sha256: string
  content: string
  preview: string
  variables: string[]
}
export type PromptFileList = { directory: string; items: { path: string; filename: string }[] }

export const listPromptFiles = async (
  stage: WorkflowStage,
  signal?: AbortSignal
): Promise<PromptFileList> =>
  (await lightragAxios.get(`${base}/prompts`, { params: { stage }, signal })).data

export const readPromptFile = async (
  stage: WorkflowStage,
  path: string,
  signal?: AbortSignal
): Promise<PromptDocument> =>
  (await lightragAxios.get(`${base}/prompts/${stage}`, { params: { path }, signal })).data

export const savePromptFile = async (
  stage: WorkflowStage,
  filename: string,
  content: string,
  signal?: AbortSignal
): Promise<PromptDocument> =>
  (await lightragAxios.post(`${base}/prompts`, { stage, filename, content }, { signal })).data

export const previewPrompt = async (
  stage: WorkflowStage,
  content: string,
  signal?: AbortSignal
): Promise<{ preview: string }> =>
  (await lightragAxios.post(`${base}/prompts/preview`, { stage, content }, { signal })).data

export const getRecipeSettings = async (signal?: AbortSignal): Promise<RecipeSettingsSnapshot> =>
  (await lightragAxios.get(`${base}/recipe-settings`, { signal, timeout: 20000 })).data

export const saveRecipeSettings = async (
  revision: string,
  settings: RecipeValues,
  signal?: AbortSignal
): Promise<RecipeSettingsSnapshot> =>
  (
    await lightragAxios.put(
      `${base}/recipe-settings`,
      { revision, settings },
      { signal, timeout: 40000 }
    )
  ).data

export const retryWorkflowJob = async (
  jobId: string,
  requestId: string,
  signal?: AbortSignal
): Promise<WorkflowJob> =>
  (
    await lightragAxios.post(
      `${base}/jobs/${encodeURIComponent(jobId)}/retry`,
      { request_id: requestId },
      { signal, timeout: 15000 }
    )
  ).data

export const startWorkflowJob = async (jobId: string, signal?: AbortSignal): Promise<WorkflowJob> =>
  (
    await lightragAxios.post(
      `${base}/jobs/${encodeURIComponent(jobId)}/start`,
      {},
      { signal, timeout: 15000 }
    )
  ).data

export const getWorkflowConfiguration = async (
  signal?: AbortSignal
): Promise<WorkflowConfiguration> =>
  (await lightragAxios.get(`${base}/config`, { signal, timeout: 10000 })).data

export const submitBatchWorkflow = async (
  batchId: string,
  signal?: AbortSignal
): Promise<WorkflowJob> =>
  (
    await lightragAxios.post(
      `${base}/submit`,
      { source: { type: 'batch', batch_id: batchId }, start: true },
      { signal, timeout: 15000 }
    )
  ).data

export const getWorkflowJob = async (jobId: string, signal?: AbortSignal): Promise<WorkflowJob> =>
  (await lightragAxios.get(`${base}/jobs/${encodeURIComponent(jobId)}`, { signal, timeout: 15000 }))
    .data

export const findBatchWorkflow = async (
  batchId: string,
  signal?: AbortSignal
): Promise<WorkflowJob | null> => {
  const response = await lightragAxios.get(`${base}/jobs`, {
    params: { source_batch_id: batchId, limit: 1 },
    signal,
    timeout: 15000
  })
  return response.data.items[0] ?? null
}

export const getWorkflowLogs = async (
  jobId: string,
  offset: number,
  signal?: AbortSignal
): Promise<WorkflowLogs> =>
  (
    await lightragAxios.get(`${base}/jobs/${encodeURIComponent(jobId)}/logs`, {
      params: { stream: 'stderr', offset, limit: 8192 },
      signal,
      timeout: 15000
    })
  ).data
