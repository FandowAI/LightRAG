import type * as workflowApi from '@/api/dataWorkflow'

export function workflowFixture(
  status: workflowApi.WorkflowJob['status'] = 'running'
): workflowApi.WorkflowJob {
  return {
    schema_version: 1,
    job_id: 'distill-test',
    request_id: 'batch:test-batch',
    source: { type: 'batch', batch_id: 'test-batch' },
    status,
    phase: 'processing',
    terminal: ['completed', 'completed_with_errors', 'failed', 'interrupted'].includes(status),
    can_start: status === 'ready',
    poll_after_ms: status === 'running' ? 10 : 0,
    created_at: '2026-10-22',
    updated_at: '2026-10-22',
    started_at: null,
    finished_at: null,
    error: null,
    progress: {
      unit: 'sources',
      percent: null,
      seen_sources: 3,
      accepted_sources: 2,
      rejected_sources: 1,
      processed_sources: null,
      committed_batch: null
    },
    result: null,
    artifacts: {},
    links: {}
  }
}

export function recipeSettingsFixture(): workflowApi.RecipeSettingsSnapshot {
  return {
    schema_version: 1,
    revision: 'a'.repeat(64),
    editable: true,
    recipe_schema_version: 3,
    installed_schema_version: 3,
    installed_commit: '236e685',
    workflow_name: 'difficulty_routed_qa',
    settings: {
      questions_per_source: 3,
      deduplicate_qa: false,
      scenario_instructions: 'Synthetic scenarios',
      classification_instructions: 'Classify importance and difficulty',
      batch_size: 1,
      max_inflight_requests: 2,
      max_prompt_chars: null,
      inspection_instructions: 'Inspect the QA against the source',
      seed_question_path: null,
      log_level: 'INFO',
      models: {
        generator: {
          api_url: 'https://test.invalid/v1/chat/completions',
          model_name: 'test-model',
          key_name_of_api_key: 'TEST_KEY'
        }
      },
      stages: {
        scenario: {
          model: 'generator',
          prompt: 'ScenarioPrompt',
          parser: 'scenarios_json.v1',
          prompt_file: '/config/prompts/defaults/scenario.md'
        },
        qa: {
          model: 'generator',
          prompt: 'GeneratePrompt',
          parser: 'qa_json.v1',
          prompt_file: null
        },
        classification: {
          model: 'generator',
          prompt: 'QAClassificationPrompt',
          parser: 'classification_json.v1',
          prompt_file: null
        },
        inspection: {
          model: 'generator',
          prompt: 'InspectAndRevisePrompt',
          parser: 'inspection_revision_json.v1',
          prompt_file: null
        }
      }
    },
    recipe_file: '/config/job.yaml',
    settings_file: '/workspace/recipe-settings.json',
    models: { generator: 'test-model' },
    model_schema: {
      properties: {
        api_url: { type: 'string' },
        model_name: { type: 'string' },
        max_workers: { type: 'integer', default: 10, exclusiveMinimum: 0 }
      },
      required: ['api_url', 'model_name']
    },
    task_schema: {}
  }
}
