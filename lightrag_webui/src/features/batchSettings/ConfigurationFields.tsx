import { useState } from 'react'
import { useTranslation } from 'react-i18next'
import Input from '@/components/ui/Input'
import Button from '@/components/ui/Button'
import RecipeModelSettings from '@/features/distillation/RecipeModelSettings'
import {
  recipeValuesOf,
  withRecipeValues,
  type BatchConfiguration,
  type BatchSettings,
  type Role
} from '@/api/batchControl'
import type { WorkflowStage } from '@/api/dataWorkflow'

import type { Section } from './sections'

function PathField({
  label,
  value,
  onChange
}: {
  label: string
  value: (string | number)[] | null
  onChange: (value: (string | number)[] | null) => void
}) {
  const serialized = JSON.stringify(value)
  const [previous, setPrevious] = useState(serialized)
  const [text, setText] = useState(serialized)
  if (previous !== serialized) {
    setPrevious(serialized)
    setText(serialized)
  }
  return (
    <label className="space-y-1 text-sm">
      <span className="block">{label}</span>
      <Input
        value={text}
        className="w-full"
        onChange={(event) => {
          const next = event.target.value
          setText(next)
          try {
            const parsed: unknown = JSON.parse(next)
            if (
              parsed !== null &&
              (!Array.isArray(parsed) ||
                parsed.some((item) => typeof item !== 'string' && !Number.isInteger(item)))
            )
              throw new Error('Invalid path')
            event.target.setCustomValidity('')
            onChange(parsed as (string | number)[] | null)
          } catch {
            event.target.setCustomValidity(label)
          }
        }}
      />
    </label>
  )
}

export default function ConfigurationFields({
  snapshot,
  value,
  onChange,
  prompts,
  onPrompt,
  section
}: {
  snapshot: BatchSettings
  value: BatchConfiguration
  onChange: (value: BatchConfiguration) => void
  prompts: Record<WorkflowStage, string>
  onPrompt: (stage: WorkflowStage, content: string) => void
  section: Section
}) {
  const { t } = useTranslation()
  const label = (key: string) => t(`batchSettings.fields.${key}`, key)
  const rag = value.rag
  const [newAlias, setNewAlias] = useState('')
  const [promptStage, setPromptStage] = useState<WorkflowStage>('scenario')
  const recipe = recipeValuesOf(value, snapshot.recipe_values)
  const number = (
    key: string,
    current: number | null | undefined,
    set: (value: number | null) => void,
    min = 1,
    max?: number,
    nullable = false,
    step: number | 'any' = 1
  ) => (
    <label key={key} className="space-y-1 text-sm">
      <span className="block">{label(key)}</span>
      <Input
        aria-label={label(key)}
        className="w-full"
        type="number"
        required={!nullable}
        min={min}
        max={max}
        step={step}
        value={current ?? ''}
        onChange={(event) =>
          set(event.target.value === '' && nullable ? null : Number(event.target.value))
        }
      />
    </label>
  )
  const select = (
    key: string,
    current: string,
    options: { value: string; name?: string }[],
    set: (value: string) => void
  ) => (
    <label key={key} className="space-y-1 text-sm">
      <span className="block">{label(key)}</span>
      <select
        aria-label={label(key)}
        className="bg-background w-full rounded-md border p-2"
        value={current}
        onChange={(event) => set(event.target.value)}
      >
        {!options.some((option) => option.value === current) && (
          <option value={current} disabled>{current}</option>
        )}
        {options.map((option) => (
          <option key={option.value} value={option.value}>
            {option.name ?? option.value}
          </option>
        ))}
      </select>
    </label>
  )
  const strings = (values: string[]) => values.map((item) => ({ value: item }))

  if (section === 'rag')
    return (
      <div className="space-y-5">
        <p className="text-muted-foreground text-sm">{t('batchSettings.restartHint')}</p>
        <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-3">
          {select(
            'parse_engine',
            rag.parse_engine,
            strings(snapshot.options.parse_engines),
            (next) => onChange({ ...value, rag: { ...rag, parse_engine: next } })
          )}
          {select(
            'chunking_strategy',
            rag.chunking_strategy,
            strings(snapshot.options.chunking_strategies),
            (next) => onChange({ ...value, rag: { ...rag, chunking_strategy: next } })
          )}
          {(
            [
              'chunk_size',
              'chunk_overlap_size',
              'max_parallel_insert',
              'max_parallel_parse_native'
            ] as const
          ).map((key) =>
            number(
              key,
              rag[key],
              (next) => onChange({ ...value, rag: { ...rag, [key]: next } }),
              key === 'chunk_overlap_size' ? 0 : 1
            )
          )}
        </div>
        <label className="block space-y-1 text-sm">
          <span>{label('embedding_profile')}</span>
          <select
            aria-label={label('embedding_profile')}
            className="bg-background w-full rounded-md border p-2"
            disabled={!snapshot.embedding_editable}
            value={rag.embedding_profile}
            onChange={(event) =>
              onChange({ ...value, rag: { ...rag, embedding_profile: event.target.value } })
            }
          >
            {snapshot.options.profiles
              .filter((profile) => profile.dimensions !== null)
              .map((profile) => (
                <option key={profile.id} value={profile.id}>
                  {profile.id} · {profile.model_name} · {profile.dimensions}
                </option>
              ))}
          </select>
          <span className="text-muted-foreground block">{t('batchSettings.embeddingHint')}</span>
        </label>
        <div className="space-y-3 rounded-md border p-3">
          <h3 className="font-medium">{label('model_profiles')}</h3>
          {Object.entries(rag.model_profiles).map(([alias, reference]) => (
            <div key={alias} className="flex items-end gap-2">
              {select(
                alias,
                reference,
                snapshot.options.profiles
                  .filter((profile) => profile.dimensions === null)
                  .map((profile) => ({
                    value: profile.id,
                    name: `${profile.id} · ${profile.model_name}`
                  })),
                (next) =>
                  onChange({
                    ...value,
                    rag: { ...rag, model_profiles: { ...rag.model_profiles, [alias]: next } }
                  })
              )}
              <Button
                type="button"
                size="sm"
                variant="outline"
                disabled={
                  Object.values(rag.role_profiles).includes(alias) ||
                  Object.keys(rag.model_profiles).length === 1
                }
                onClick={() => {
                  const profiles = { ...rag.model_profiles }
                  delete profiles[alias]
                  onChange({ ...value, rag: { ...rag, model_profiles: profiles } })
                }}
              >
                {t('batchSettings.remove')}
              </Button>
            </div>
          ))}
          <div className="flex gap-2">
            <Input
              aria-label={t('batchSettings.alias')}
              value={newAlias}
              onChange={(event) => setNewAlias(event.target.value)}
            />
            <Button
              type="button"
              variant="outline"
              disabled={
                !/^[A-Za-z0-9][A-Za-z0-9_.-]*$/.test(newAlias) ||
                newAlias in rag.model_profiles ||
                !snapshot.options.profiles.some((profile) => profile.dimensions === null)
              }
              onClick={() => {
                onChange({
                  ...value,
                  rag: {
                    ...rag,
                    model_profiles: {
                      ...rag.model_profiles,
                      [newAlias]: snapshot.options.profiles.find(
                        (profile) => profile.dimensions === null
                      )!.id
                    }
                  }
                })
                setNewAlias('')
              }}
            >
              {t('batchSettings.add')}
            </Button>
          </div>
        </div>
        <div className="grid gap-3 sm:grid-cols-2">
          {(['extract', 'keyword', 'query', 'vlm'] as Role[]).map((role) => (
            <div key={role} className="space-y-3 rounded-md border p-3">
              <h3 className="font-medium">{t(`batchSettings.roles.${role}`)}</h3>
              {select(
                `role_${role}`,
                rag.role_profiles[role] ?? '',
                [
                  { value: '', name: t('batchSettings.inherit') },
                  ...strings(Object.keys(rag.model_profiles))
                ],
                (next) => {
                  const roles = { ...rag.role_profiles }
                  if (next) roles[role] = next
                  else delete roles[role]
                  onChange({ ...value, rag: { ...rag, role_profiles: roles } })
                }
              )}
              {number(
                `concurrency_${role}`,
                rag.role_concurrency[role],
                (next) => {
                  const counts = { ...rag.role_concurrency }
                  if (next === null) delete counts[role]
                  else counts[role] = next
                  onChange({ ...value, rag: { ...rag, role_concurrency: counts } })
                },
                1,
                undefined,
                true
              )}
            </div>
          ))}
        </div>
      </div>
    )

  if (section === 'retrieval') {
    const ordinary = value.retrieval.ordinary ?? {}
    const batch = value.retrieval.batch ?? {}
    return (
      <div className="space-y-4">
        <p className="text-muted-foreground text-sm">{t('batchSettings.retrievalHint')}</p>
        <h3 className="font-medium">{t('batchSettings.ordinary')}</h3>
        <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-3">
          {select(
            'mode',
            ordinary.mode ?? 'mix',
            strings(['local', 'global', 'hybrid', 'mix', 'naive']),
            (next) =>
              onChange({
                ...value,
                retrieval: { ...value.retrieval, ordinary: { ...ordinary, mode: next } }
              })
          )}
          {(
            [
              'top_k',
              'chunk_top_k',
              'max_entity_tokens',
              'max_relation_tokens',
              'max_total_tokens'
            ] as const
          ).map((key) =>
            number(
              key,
              ordinary[key] ??
                {
                  top_k: 40,
                  chunk_top_k: 20,
                  max_entity_tokens: 6000,
                  max_relation_tokens: 8000,
                  max_total_tokens: 30000
                }[key],
              (next) =>
                onChange({
                  ...value,
                  retrieval: { ...value.retrieval, ordinary: { ...ordinary, [key]: next } }
                })
            )
          )}
        </div>
        <h3 className="font-medium">{t('batchSettings.evidence')}</h3>
        <div className="grid gap-3 sm:grid-cols-2">
          {number(
            'graph_depth',
            batch.graph_depth ?? 2,
            (next) =>
              onChange({
                ...value,
                retrieval: { ...value.retrieval, batch: { ...batch, graph_depth: next! } }
              }),
            0,
            5
          )}
          {number(
            'rag_top_k',
            batch.rag_top_k ?? 10,
            (next) =>
              onChange({
                ...value,
                retrieval: { ...value.retrieval, batch: { ...batch, rag_top_k: next! } }
              }),
            1,
            100
          )}
          {number(
            'near_threshold',
            batch.near_threshold ?? 0.995,
            (next) =>
              onChange({
                ...value,
                retrieval: { ...value.retrieval, batch: { ...batch, near_threshold: next! } }
              }),
            0.8,
            1,
            false,
            'any'
          )}
          <label className="flex items-center gap-2">
            <input
              type="checkbox"
              checked={batch.near_dedup ?? true}
              onChange={(event) =>
                onChange({
                  ...value,
                  retrieval: {
                    ...value.retrieval,
                    batch: { ...batch, near_dedup: event.target.checked }
                  }
                })
              }
            />
            {label('near_dedup')}
          </label>
        </div>
      </div>
    )
  }
  if (section === 'recipe')
    return (
      <div className="space-y-4">
        <p className="text-muted-foreground text-sm">{t('batchSettings.newTaskHint')}</p>
        <div className="grid gap-3 sm:grid-cols-2">
          {(['questions_per_source', 'batch_size', 'max_inflight_requests'] as const).map((key) =>
            number(key, recipe[key], (next) =>
              onChange(withRecipeValues(value, { ...recipe, [key]: next }))
            )
          )}
          {number(
            'max_prompt_chars',
            recipe.max_prompt_chars,
            (next) => onChange(withRecipeValues(value, { ...recipe, max_prompt_chars: next })),
            1,
            undefined,
            true
          )}
          {select(
            'log_level',
            recipe.log_level,
            strings(['DEBUG', 'INFO', 'WARNING', 'ERROR', 'CRITICAL']),
            (next) =>
              onChange(
                withRecipeValues(value, { ...recipe, log_level: next as typeof recipe.log_level })
              )
          )}
          <PathField
            label={label('seed_question_path')}
            value={recipe.seed_question_path}
            onChange={(next) =>
              onChange(withRecipeValues(value, { ...recipe, seed_question_path: next }))
            }
          />
        </div>
        {(
          [
            'scenario_instructions',
            'classification_instructions',
            'inspection_instructions'
          ] as const
        ).map((key) => (
          <label key={key} className="block space-y-1 text-sm">
            <span>{label(key)}</span>
            <textarea
              required
              className="bg-background min-h-24 w-full rounded-md border p-2"
              value={recipe[key]}
              onChange={(event) =>
                onChange(withRecipeValues(value, { ...recipe, [key]: event.target.value }))
              }
            />
          </label>
        ))}
        <RecipeModelSettings
          snapshot={{ model_schema: snapshot.model_schema }}
          values={recipe}
          onChange={(next) => onChange(withRecipeValues(value, next))}
        />
        <div className="grid gap-3 sm:grid-cols-2">
          {(['scenario', 'qa', 'classification', 'inspection'] as WorkflowStage[]).map((stage) => (
            <div key={stage} className="space-y-2 rounded-md border p-3">
              <h3>{t(`batchSettings.stages.${stage}`)}</h3>
              {select(
                `stage_${stage}`,
                recipe.stages[stage].model,
                strings(Object.keys(recipe.models)),
                (next) =>
                  onChange(
                    withRecipeValues(value, {
                      ...recipe,
                      stages: {
                        ...recipe.stages,
                        [stage]: { ...recipe.stages[stage], model: next }
                      }
                    })
                  )
              )}
              <p className="text-muted-foreground text-xs">
                {recipe.stages[stage].prompt} · {recipe.stages[stage].parser}
              </p>
            </div>
          ))}
        </div>
      </div>
    )

  if (section === 'prompts')
    return (
      <div className="space-y-3">
        <p className="text-muted-foreground text-sm">{t('batchSettings.promptHint')}</p>
        {select(
          'prompt_stage',
          promptStage,
          ['scenario', 'qa', 'classification', 'inspection'].map((stage) => ({
            value: stage,
            name: t(`batchSettings.stages.${stage}`)
          })),
          (next) => setPromptStage(next as WorkflowStage)
        )}
        <label className="block space-y-1">
          <span>{t('batchSettings.promptContent')}</span>
          <textarea
            aria-label={t('batchSettings.promptContent')}
            spellCheck={false}
            className="bg-background min-h-80 w-full rounded-md border p-3 font-mono text-sm"
            value={prompts[promptStage]}
            onChange={(event) => onPrompt(promptStage, event.target.value)}
          />
        </label>
      </div>
    )

  const bridge = value.bridge
  return (
    <div className="space-y-4">
      <p className="text-muted-foreground text-sm">{t('batchSettings.bridgeHint')}</p>
      <div className="grid gap-3 sm:grid-cols-2">
        {select(
          'environment_id',
          bridge.environment_id,
          strings(snapshot.options.environments),
          (next) => onChange({ ...value, bridge: { ...bridge, environment_id: next } })
        )}
        {select(
          'credential_id',
          bridge.credential_id ?? '',
          [{ value: '', name: t('batchSettings.none') }, ...strings(snapshot.options.credentials)],
          (next) => onChange({ ...value, bridge: { ...bridge, credential_id: next || null } })
        )}
        {(['max_input_bytes', 'max_records', 'max_active_jobs'] as const).map((key) =>
          number(key, bridge[key], (next) =>
            onChange({ ...value, bridge: { ...bridge, [key]: next } })
          )
        )}
        {number(
          'timeout_seconds',
          bridge.timeout_seconds,
          (next) => onChange({ ...value, bridge: { ...bridge, timeout_seconds: next } }),
          0.001,
          undefined,
          true,
          'any'
        )}
        <PathField
          label={label('records_path')}
          value={bridge.records_path}
          onChange={(next) =>
            onChange({ ...value, bridge: { ...bridge, records_path: next ?? [] } })
          }
        />
      </div>
      <fieldset className="space-y-2">
        <legend className="mb-2 text-sm font-medium">{label('input_ids')}</legend>
        {snapshot.options.inputs.map((reference) => (
          <label key={reference} className="flex items-center gap-2 text-sm">
            <input
              type="checkbox"
              checked={bridge.input_ids.includes(reference)}
              onChange={(event) =>
                onChange({
                  ...value,
                  bridge: {
                    ...bridge,
                    input_ids: event.target.checked
                      ? [...bridge.input_ids, reference]
                      : bridge.input_ids.filter((item) => item !== reference)
                  }
                })
              }
            />
            {reference}
          </label>
        ))}
      </fieldset>
    </div>
  )
}
