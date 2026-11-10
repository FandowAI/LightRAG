import { backendBaseUrl } from '@/lib/constants'
import type { ModelFieldSchema, ModelValues, RecipeValues, WorkflowStage } from './dataWorkflow'

export type Role = 'extract' | 'keyword' | 'query' | 'vlm'
export type CopyGroup = 'rag' | 'recipe' | 'bridge'
export type BatchRecord = {
  id: string
  name: string
  active: string
  desired: string | null
  pending: string | null
  workspace: string
  namespace: string
  index_state: string
  lifecycle: string
}
export type BatchConfiguration = {
  schema_version: 1
  rag: {
    parse_engine: string
    chunking_strategy: string
    chunk_size: number
    chunk_overlap_size: number
    max_parallel_insert: number
    max_parallel_parse_native: number
    embedding_profile: string
    model_profiles: Record<string, string>
    role_profiles: Partial<Record<Role, string>>
    role_concurrency: Partial<Record<Role, number>>
  }
  retrieval: {
    ordinary?: {
      mode?: string
      top_k?: number
      chunk_top_k?: number
      max_entity_tokens?: number
      max_relation_tokens?: number
      max_total_tokens?: number
    }
    batch?: {
      graph_depth?: number
      rag_top_k?: number
      near_dedup?: boolean
      near_threshold?: number
    }
  }
  recipe: {
    schema_version: number
    runtime?: Record<string, unknown>
    models: Record<string, ModelValues>
    workflow: { name: string; parameters: Record<string, unknown> }
    [key: string]: unknown
  }
  bridge: {
    environment_id: string
    credential_id: string | null
    input_ids: string[]
    records_path: (string | number)[]
    max_input_bytes: number
    max_records: number
    max_active_jobs: number
    timeout_seconds: number | null
  }
}
export type Copies = Partial<Record<CopyGroup, { batch: string; revision: string }>>
export type Application = {
  operation_id: string
  data_batch_id: string
  config_revision_id: string
  state:
    | 'awaiting_action'
    | 'queued'
    | 'draining'
    | 'stopping'
    | 'starting'
    | 'verifying'
    | 'rollback_stopping'
    | 'rollback_starting'
    | 'rollback_verifying'
    | 'rolled_back'
    | 'rollback_failed'
    | 'applied'
    | 'failed'
    | 'discarded'
  terminal: boolean
  error?: string | null
  rollback_error?: string | null
  waiting_reason?: string | null
  attempt?: number
}
export type IndexRebuild = {
  operation_id: string
  data_batch_id: string
  config_revision_id: string
  previous_revision: string
  state: string
  version: number
  terminal: boolean
  cancel_requested: boolean
  error: string | null
  rollback_error: string | null
  waiting_reason: string | null
  progress: { state: string; total: number; written: number; verified: number } | null
  provider_admission: { items: { queued: number; active: number }[] }
  target: { embedding_profile: string; embedding_model: string; dimensions: number; vector_workspace: string; collections: Record<string, string> }
  binding_state: string
  can_cancel: boolean
  can_recover: boolean
  urls: Record<string, string>
}
export type BatchSettings = {
  schema_version: 1
  batch: BatchRecord
  revision: string
  configuration: BatchConfiguration
  active_configuration: BatchConfiguration
  prompts: Record<WorkflowStage, string>
  active_prompts: Record<WorkflowStage, string>
  recipe_values: RecipeValues
  model_schema: { properties?: Record<string, ModelFieldSchema>; required?: string[] }
  fields: { key: string; label: string; effect: string; reusable: boolean }[]
  options: {
    profiles: { id: string; model_name: string; dimensions: number | null }[]
    environments: string[]
    credentials: string[]
    inputs: string[]
    parse_engines: string[]
    chunking_strategies: string[]
  }
  permissions: Record<'edit' | 'apply_config' | 'run_runtime' | 'stop_runtime' | 'audit', boolean> & { rebuild_index?: boolean }
  embedding_editable: boolean
  application: Application | null
  index_rebuild?: IndexRebuild | null
}
export type DraftRequest = {
  expected_revision: string
  request_id: string
  configuration: BatchConfiguration
  copies?: Copies
  prompt_contents?: Partial<Record<WorkflowStage, string>>
}
export type Preview = {
  draft: BatchConfiguration
  prompts: Record<WorkflowStage, string>
  expected_revision: string
  differences: { field: string; before: unknown; after: unknown }[]
  requires_restart: boolean
}
export type SavedConfiguration = {
  batch: string
  revision: string
  operation: string | null
  status: 'applied' | 'pending_restart' | 'unchanged'
  requires_restart: boolean
}
export type BatchHttpError = Error & { status?: number }
export type BatchArtifact = 'retrieval-packages' | 'retrieval-sources' | 'retrieval-manifest' | 'workflow-input' | 'workflow-review' | 'workflow-run'
export type BatchArtifactInfo = { artifact: BatchArtifact; name: string; bytes: number }
export type BatchLog = { text: string; offset: number; next_offset: number; has_more: boolean }
export type BatchLogStream = 'stdout' | 'stderr' | 'worker'
export type TrainingFormat = 'messages' | 'alpaca' | 'sharegpt'
export type TrainingSelection = 'processed' | 'machine_passed'
export type TrainingFile = 'dataset' | 'provenance' | 'manifest' | 'log'
export type TrainingExport = {
  export_id: string
  data_batch_id: string
  task_id: string
  config_revision_id: string
  format: TrainingFormat
  selection: TrainingSelection
  state: 'queued' | 'running' | 'completed' | 'failed' | 'interrupted'
  counts: Record<string, number>
  error: string | null
  created_at: number
  updated_at: number
}

export type BatchTask = {
  task_id: string
  data_batch_id: string
  config_revision_id: string
  kind: 'ingest' | 'centers' | 'retrieval' | 'distillation' | 'chain' | 'query'
  state: string
  phase: string
  terminal: boolean
  cancel_requested: boolean
  progress: Record<string, string | number | null> | null
  provider_admission?: { queued: number; active: number }
  can_export_training?: boolean
  result: {
    centers?: { candidates: { name: string; tier: string }[] }
    retrieval?: { job_id: string }
    distillation?: { result?: { summary: Record<string, unknown> }; progress?: Record<string, unknown> }
    [key: string]: unknown
  } | null
  error: string | null
  waiting_reason: string | null
  created_at: number
}

export type BatchTaskCommand = {
  expected_revision: string
  request_id: string
  kind: BatchTask['kind']
  source: { data_batch_id: string; type: 'directory' | 'index' | 'retrieval'; input_ids?: string[]; retrieval_job_id?: string }
  centers?: string[]
  retrieval?: { graph_depth: number; rag_top_k: number; near_dedup: boolean; near_threshold: number }
}

export function createBatchClient(apiKey: string | null = null) {
  async function request(
    path: string,
    method = 'GET',
    body?: unknown,
    signal?: AbortSignal
  ): Promise<Response> {
    const token = localStorage.getItem('LIGHTRAG-API-TOKEN')
    const response = await fetch(`${backendBaseUrl}/data-batches${path}`, {
      method,
      signal,
      headers: {
        'Content-Type': 'application/json',
        ...(apiKey ? { 'X-API-Key': apiKey } : token ? { Authorization: `Bearer ${token}` } : {})
      },
      ...(body === undefined ? {} : { body: JSON.stringify(body) })
    })
    if (!response.ok) {
      const data = await response.json().catch(() => null)
      const error: BatchHttpError = new Error(
        `${response.status}: ${typeof data?.detail === 'string' ? data.detail : JSON.stringify(data?.detail ?? response.statusText)}`
      )
      error.status = response.status
      throw error
    }
    // A key-authenticated session does not need to import an unrelated JWT.
    const renewed = response.headers.get('X-New-Token')
    if (!apiKey && renewed) localStorage.setItem('LIGHTRAG-API-TOKEN', renewed)
    return response
  }
  async function send<T>(path: string, method = 'GET', body?: unknown, signal?: AbortSignal): Promise<T> {
    return (await request(path, method, body, signal)).json()
  }
  const batchPath = (id: string) => `/${encodeURIComponent(id)}`
  const taskPath = (id: string, task: string) => `${batchPath(id)}/tasks/${encodeURIComponent(task)}`
  return {
    list: (signal?: AbortSignal) => send<{ items: BatchRecord[] }>('', 'GET', undefined, signal),
    workbenchHealth: (id: string, signal?: AbortSignal) => send<Record<string, unknown>>(`${batchPath(id)}/api/health`, 'GET', undefined, signal),
    tasks: (id: string, signal?: AbortSignal) => send<{ items: BatchTask[] }>(`${batchPath(id)}/tasks`, 'GET', undefined, signal),
    submit: (id: string, command: BatchTaskCommand) => send<BatchTask>(`${batchPath(id)}/tasks`, 'POST', command),
    cancelTask: (id: string, task: string) => send<BatchTask>(`${batchPath(id)}/tasks/${encodeURIComponent(task)}/cancel`, 'POST'),
    retryTask: (id: string, task: string, expected_revision: string, request_id: string) => send<BatchTask>(`${batchPath(id)}/tasks/${encodeURIComponent(task)}/retry`, 'POST', { expected_revision, request_id }),
    taskResult: (id: string, task: string, signal?: AbortSignal) => send<Record<string, unknown>>(`${taskPath(id, task)}/result`, 'GET', undefined, signal),
    taskLogs: (id: string, task: string, stream: BatchLogStream, offset = 0, signal?: AbortSignal) => send<BatchLog>(`${taskPath(id, task)}/logs?stream=${stream}&offset=${offset}`, 'GET', undefined, signal),
    taskArtifacts: (id: string, task: string, signal?: AbortSignal) => send<{ items: BatchArtifactInfo[] }>(`${taskPath(id, task)}/artifacts`, 'GET', undefined, signal),
    taskDownload: async (id: string, task: string, artifact: BatchArtifact, signal?: AbortSignal) => (await request(`${taskPath(id, task)}/artifacts/${artifact}`, 'GET', undefined, signal)).blob(),
    trainingExports: (id: string, task: string, signal?: AbortSignal) => send<{ items: TrainingExport[] }>(`${taskPath(id, task)}/training-exports`, 'GET', undefined, signal),
    createTrainingExport: (id: string, task: string, format: TrainingFormat, selection: TrainingSelection, request_id: string, signal?: AbortSignal) => send<TrainingExport>(`${taskPath(id, task)}/training-exports`, 'POST', { format, selection, request_id }, signal),
    trainingExportStatus: (id: string, task: string, exported: string, signal?: AbortSignal) => send<TrainingExport>(`${taskPath(id, task)}/training-exports/${encodeURIComponent(exported)}`, 'GET', undefined, signal),
    trainingExportDownload: async (id: string, task: string, exported: string, artifact: TrainingFile, signal?: AbortSignal) => (await request(`${taskPath(id, task)}/training-exports/${encodeURIComponent(exported)}/files/${artifact}`, 'GET', undefined, signal)).blob(),
    create: (name: string, request_id: string, copies?: Copies) =>
      send<BatchRecord>('', 'POST', { name, request_id, ...(copies ? { copies } : {}) }),
    settings: (id: string, signal?: AbortSignal) =>
      send<BatchSettings>(`${batchPath(id)}/settings`, 'GET', undefined, signal),
    indexRebuilds: (id: string, signal?: AbortSignal) => send<{ items: IndexRebuild[] }>(`${batchPath(id)}/index-rebuilds`, 'GET', undefined, signal),
    startIndexRebuild: (id: string, expected_revision: string, embedding_profile: string, request_id: string, signal?: AbortSignal) => send<IndexRebuild>(`${batchPath(id)}/index-rebuilds`, 'POST', { expected_revision, embedding_profile, request_id }, signal),
    cancelIndexRebuild: (id: string, operation: string, signal?: AbortSignal) => send<IndexRebuild>(`${batchPath(id)}/index-rebuilds/${encodeURIComponent(operation)}/cancel`, 'POST', undefined, signal),
    recoverIndexRebuild: (id: string, operation: string, expected_version: number, signal?: AbortSignal) => send<IndexRebuild>(`${batchPath(id)}/index-rebuilds/${encodeURIComponent(operation)}/recover`, 'POST', { expected_version }, signal),
    preview: (id: string, body: DraftRequest) =>
      send<Preview>(`${batchPath(id)}/settings/preview`, 'POST', body),
    save: (id: string, body: DraftRequest) =>
      send<SavedConfiguration>(`${batchPath(id)}/settings`, 'PUT', body),
    copy: (id: string, expected_revision: string, copies: Copies) =>
      send<Preview>(`${batchPath(id)}/settings/copy-preview`, 'POST', {
        expected_revision,
        copies
      }),
    application: (id: string, operation: string, signal?: AbortSignal) =>
      send<Application>(
        `${batchPath(id)}/applications/${encodeURIComponent(operation)}`,
        'GET',
        undefined,
        signal
      ),
    apply: (id: string, operation: string, expected_revision: string, request_id: string) =>
      send<Application>(
        `${batchPath(id)}/applications/${encodeURIComponent(operation)}/start`,
        'POST',
        { expected_revision, request_id }
      ),
    discard: (id: string, operation: string) =>
      send<Application>(
        `${batchPath(id)}/applications/${encodeURIComponent(operation)}/discard`,
        'POST'
      )
  }
}
export type BatchClient = ReturnType<typeof createBatchClient>

export function requestId() {
  return `settings-${Date.now().toString(36)}-${Math.random().toString(16).slice(2)}`
}

const parameterKeys = [
  'questions_per_source',
  'scenario_instructions',
  'classification_instructions',
  'inspection_instructions',
  'seed_question_path'
] as const
const runtimeKeys = [
  'batch_size',
  'max_inflight_requests',
  'max_prompt_chars',
  'log_level'
] as const
const stages = ['scenario', 'qa', 'classification', 'inspection'] as const

export function withRecipeValues(
  configuration: BatchConfiguration,
  values: RecipeValues
): BatchConfiguration {
  return {
    ...configuration,
    recipe: {
      ...configuration.recipe,
      models: values.models,
      runtime: {
        ...configuration.recipe.runtime,
        ...Object.fromEntries(runtimeKeys.map((key) => [key, values[key]]))
      },
      workflow: {
        ...configuration.recipe.workflow,
        parameters: {
          ...configuration.recipe.workflow.parameters,
          ...Object.fromEntries(parameterKeys.map((key) => [key, values[key]])),
          ...values.stages
        }
      }
    }
  }
}

export function recipeValuesOf(
  configuration: BatchConfiguration,
  fallback: RecipeValues
): RecipeValues {
  const params = configuration.recipe.workflow.parameters
  return {
    ...fallback,
    models: configuration.recipe.models,
    ...Object.fromEntries(
      parameterKeys.filter((key) => key in params).map((key) => [key, params[key]])
    ),
    ...Object.fromEntries(
      runtimeKeys
        .filter((key) => key in (configuration.recipe.runtime ?? {}))
        .map((key) => [key, configuration.recipe.runtime?.[key]])
    ),
    stages: Object.fromEntries(stages.map((key) => [key, params[key]])) as RecipeValues['stages']
  }
}
