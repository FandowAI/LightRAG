import { mock } from 'bun:test'
import { recipeSettingsFixture } from './dataWorkflow'
import { recipeValuesOf, type Application, type BatchConfiguration, type BatchSettings, type BatchTask, type BatchTaskCommand, type Copies, type DraftRequest, type Preview, type TrainingExport, type TrainingFormat, type TrainingSelection, type TrainingFile, type IndexRebuild } from '@/api/batchControl'

export function indexRebuildFixture(id = 'batch-a', state = 'building'): IndexRebuild {
  return { operation_id: 'index-' + id, data_batch_id: id, config_revision_id: 'rev-index', previous_revision: 'rev-a', state, version: 1, terminal: ['applied', 'rolled_back'].includes(state), cancel_requested: false, error: null, rollback_error: null, waiting_reason: null, progress: { state: 'building', total: 4, written: 2, verified: 1 }, provider_admission: { items: [] }, target: { embedding_profile: 'embed-v1', embedding_model: 'Test embed', dimensions: 8, vector_workspace: 'idx-example', collections: {} }, binding_state: 'declared', can_cancel: !state.startsWith('rollback_') && state !== 'applied', can_recover: state === 'rollback_failed', urls: {} }
}

export function batchSettingsFixture(id = 'batch-a'): BatchSettings {
  const recipe = recipeSettingsFixture()
  const configuration: BatchConfiguration = {
    schema_version: 1,
    rag: { parse_engine: 'native', chunking_strategy: 'token', chunk_size: 1200, chunk_overlap_size: 100, max_parallel_insert: 3, max_parallel_parse_native: 5, embedding_profile: 'embed-v1', model_profiles: { primary: 'chat-v1' }, role_profiles: { extract: 'primary' }, role_concurrency: {} },
    retrieval: { ordinary: { mode: 'mix', top_k: 40 }, batch: { graph_depth: 2, rag_top_k: 10 } },
    recipe: { schema_version: 3, models: recipe.settings.models, runtime: { batch_size: 1, max_inflight_requests: 2 }, workflow: { name: 'difficulty_routed_qa', parameters: { ...Object.fromEntries(['questions_per_source', 'scenario_instructions', 'classification_instructions', 'inspection_instructions', 'seed_question_path'].map(key => [key, recipe.settings[key as keyof typeof recipe.settings]])), ...recipe.settings.stages } } },
    bridge: { environment_id: 'workflow-v1', credential_id: null, input_ids: ['documents'], records_path: [], max_input_bytes: 67108864, max_records: 10000, max_active_jobs: 1, timeout_seconds: null }
  }
  return {
    schema_version: 1, batch: { id, name: id === 'batch-a' ? 'Batch A' : 'Batch B', active: `rev-${id.slice(-1)}`, desired: null, pending: null, workspace: id + '-workspace', namespace: 'namespace', index_state: 'unprovisioned', lifecycle: 'registered' },
    revision: `rev-${id.slice(-1)}`, configuration, active_configuration: structuredClone(configuration),
    prompts: { scenario: 'scenario: $payload', qa: 'qa: $payload', classification: 'classification: $payload', inspection: 'inspection: $payload' },
    active_prompts: { scenario: 'scenario: $payload', qa: 'qa: $payload', classification: 'classification: $payload', inspection: 'inspection: $payload' },
    recipe_values: recipe.settings, model_schema: recipe.model_schema, fields: [],
    options: { profiles: [{ id: 'chat-v1', model_name: 'Test chat', dimensions: null }, { id: 'embed-v1', model_name: 'Test embed', dimensions: 8 }], environments: ['workflow-v1'], credentials: ['workflow-keys'], inputs: ['documents'], parse_engines: ['native'], chunking_strategies: ['token', 'fixed_token'] },
    permissions: { edit: true, apply_config: true, run_runtime: true, stop_runtime: true, audit: true }, embedding_editable: true, application: null
  }
}

export function batchClientFixture() {
  const rebuilds: Record<string, IndexRebuild[]> = {}
  const exportsByTask: Record<string, TrainingExport[]> = {}
  const tasks: Record<string, BatchTask[]> = { 'batch-a': [], 'batch-b': [] }
  const state = { 'batch-a': batchSettingsFixture(), 'batch-b': batchSettingsFixture('batch-b') } as Record<string, BatchSettings>
  state['batch-b'].configuration.rag.max_parallel_insert = 11
  state['batch-b'].configuration.retrieval.batch!.rag_top_k = 66
  const preview = (id: string, body: DraftRequest): Preview => ({ expected_revision: body.expected_revision, draft: body.configuration, prompts: { ...state[id].prompts, ...body.prompt_contents }, differences: [{ field: 'rag.max_parallel_insert', before: 3, after: body.configuration.rag.max_parallel_insert }], requires_restart: JSON.stringify(body.configuration.rag) !== JSON.stringify(state[id].active_configuration.rag) })
  const client = {
    workbenchHealth: mock(async (id: string, signal?: AbortSignal) => { void signal; return { configuration: { workspace: state[id].batch.workspace } } }),
    tasks: mock(async (id: string, signal?: AbortSignal) => { void signal; return { items: structuredClone(tasks[id] ?? []) } }),
    taskResult: mock(async (id: string, task: string, signal?: AbortSignal): Promise<Record<string, unknown>> => { void signal; return { task_id: task, data_batch_id: id } }),
    taskLogs: mock(async (id: string, task: string, stream: import('@/api/batchControl').BatchLogStream, offset = 0, signal?: AbortSignal) => { void id; void task; void stream; void signal; return { text: '', offset, next_offset: offset, has_more: false } }),
    taskArtifacts: mock(async (id: string, task: string, signal?: AbortSignal): Promise<{ items: import('@/api/batchControl').BatchArtifactInfo[] }> => { void id; void task; void signal; return { items: [] } }),
    taskDownload: mock(async (id: string, task: string, artifact: import('@/api/batchControl').BatchArtifact, signal?: AbortSignal) => { void id; void task; void artifact; void signal; return new Blob(['{}']) }),
    trainingExports: mock(async (id: string, task: string, signal?: AbortSignal): Promise<{ items: TrainingExport[] }> => { void signal; return { items: structuredClone(exportsByTask[`${id}:${task}`] ?? []) } }),
    createTrainingExport: mock(async (id: string, task: string, format: TrainingFormat, selection: TrainingSelection, request: string, signal?: AbortSignal): Promise<TrainingExport> => {
      void signal
      const item: TrainingExport = { export_id: `export-${request}`, data_batch_id: id, task_id: task, config_revision_id: 'rev-a', format, selection, state: 'completed', counts: { samples: 3 }, error: null, created_at: 1, updated_at: 2 }
      ;(exportsByTask[`${id}:${task}`] ??= []).push(item)
      return structuredClone(item)
    }),
    trainingExportStatus: mock(async (id: string, task: string, exported: string, signal?: AbortSignal): Promise<TrainingExport> => { void signal; return structuredClone(exportsByTask[`${id}:${task}`].find((item) => item.export_id === exported)!) }),
    trainingExportDownload: mock(async (id: string, task: string, exported: string, artifact: TrainingFile, signal?: AbortSignal) => { void id; void task; void exported; void artifact; void signal; return new Blob(['{}']) }),
    submit: mock(async (id: string, command: BatchTaskCommand): Promise<BatchTask> => {
      const task: BatchTask = { task_id: 'task-' + command.request_id, data_batch_id: id, config_revision_id: command.expected_revision, kind: command.kind, state: 'queued', phase: 'queued', terminal: false, cancel_requested: false, progress: null, result: null, error: null, waiting_reason: null, created_at: Date.now() / 1000 }
      ;(tasks[id] ??= []).push(task)
      return structuredClone(task)
    }),
    cancelTask: mock(async (id: string, taskId: string): Promise<BatchTask> => { const task = tasks[id].find((item) => item.task_id === taskId)!; Object.assign(task, { state: 'cancelled', terminal: true, cancel_requested: true }); return structuredClone(task) }),
    retryTask: mock(async (id: string, taskId: string, revision: string, request: string): Promise<BatchTask> => { void request; const original = tasks[id].find((item) => item.task_id === taskId)!; const task = { ...original, task_id: taskId + '-retry', config_revision_id: revision, state: 'queued', terminal: false }; tasks[id].push(task); return structuredClone(task) }),
    list: mock(async () => ({ items: Object.values(state).map(item => ({ ...item.batch })) })),
    create: mock(async (name: string, _request: string, _copies?: Copies) => { void _request; void _copies; return { ...state['batch-b'].batch, id: 'batch-new', name } }),
    settings: mock(async (id: string, signal?: AbortSignal) => { void signal; return structuredClone(state[id]) }),
    indexRebuilds: mock(async (id: string, signal?: AbortSignal): Promise<{ items: IndexRebuild[] }> => { void signal; return { items: structuredClone(rebuilds[id] ?? []) } }),
    startIndexRebuild: mock(async (id: string, revision: string, profile: string, request: string, signal?: AbortSignal): Promise<IndexRebuild> => { void request; void signal; const item = indexRebuildFixture(id, 'draining'); item.previous_revision = revision; item.target.embedding_profile = profile; rebuilds[id] = [item]; state[id].index_rebuild = item; state[id].batch.pending = item.operation_id; state[id].batch.desired = item.config_revision_id; return structuredClone(item) }),
    cancelIndexRebuild: mock(async (id: string, operation: string, signal?: AbortSignal): Promise<IndexRebuild> => { void signal; const item = rebuilds[id]?.find((value) => value.operation_id === operation) ?? indexRebuildFixture(id); const next = { ...item, cancel_requested: true, version: item.version + 1 }; rebuilds[id] = [next]; return structuredClone(next) }),
    recoverIndexRebuild: mock(async (id: string, operation: string, version: number, signal?: AbortSignal): Promise<IndexRebuild> => { void signal; const item = rebuilds[id]?.find((value) => value.operation_id === operation) ?? indexRebuildFixture(id); const next = { ...item, state: 'rollback_stopping', version: version + 1, can_cancel: false, can_recover: false }; rebuilds[id] = [next]; return structuredClone(next) }),
    preview: mock(async (id: string, body: DraftRequest) => preview(id, body)),
    save: mock(async (id: string, body: DraftRequest) => {
      const restart = preview(id, body).requires_restart
      state[id].configuration = structuredClone(body.configuration)
      state[id].prompts = { ...state[id].prompts, ...body.prompt_contents }
      state[id].recipe_values = recipeValuesOf(body.configuration, state[id].recipe_values)
      state[id].revision = 'rev-new'
      if (restart) {
        state[id].batch.desired = 'rev-new'; state[id].batch.pending = 'op-new'
        state[id].application = { operation_id: 'op-new', data_batch_id: id, config_revision_id: 'rev-new', state: 'awaiting_action', terminal: false }
      } else { state[id].batch.active = 'rev-new'; state[id].active_configuration = structuredClone(body.configuration) }
      return { batch: id, revision: 'rev-new', operation: 'op-new', status: restart ? 'pending_restart' as const : 'applied' as const, requires_restart: restart }
    }),
    copy: mock(async (id: string, revision: string, copies: Copies) => {
      const draft = structuredClone(state[id].configuration)
      for (const [group, source] of Object.entries(copies)) {
        Object.assign(draft, { [group]: structuredClone(state[source!.batch].configuration[group as keyof BatchConfiguration]) })
      }
      return preview(id, { expected_revision: revision, request_id: 'copy', configuration: draft })
    }),
    application: mock(async (id: string): Promise<Application> => state[id].application!),
    apply: mock(async (id: string, operation: string, revision: string, _request: string): Promise<Application> => { void _request; return { operation_id: operation, data_batch_id: id, config_revision_id: revision, state: 'queued', terminal: false } }),
    discard: mock(async (id: string, operation: string): Promise<Application> => ({ operation_id: operation, data_batch_id: id, config_revision_id: 'rev-new', state: 'discarded', terminal: true }))
  }
  return { client, state, tasks, exportsByTask }
}
