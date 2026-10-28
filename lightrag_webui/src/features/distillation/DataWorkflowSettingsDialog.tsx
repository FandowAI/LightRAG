import { useEffect, useRef, useState } from 'react'
import { useTranslation } from 'react-i18next'
import { Settings2Icon } from 'lucide-react'
import Button from '@/components/ui/Button'
import Input from '@/components/ui/Input'
import {
  Dialog,
  DialogClose,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
  DialogTrigger
} from '@/components/ui/Dialog'
import {
  getRecipeSettings,
  saveRecipeSettings,
  type RecipeSettingsSnapshot,
  type RecipeValues
} from '@/api/dataWorkflow'
import { errorMessage } from '@/lib/utils'
import RecipeModelSettings from './RecipeModelSettings'
import WorkflowPromptEditor from './WorkflowPromptEditor'

export default function DataWorkflowSettingsDialog({
  onSaved,
  disabled
}: {
  onSaved: () => void
  disabled?: boolean
}) {
  const { t } = useTranslation()
  const [open, setOpen] = useState(false)
  const [snapshot, setSnapshot] = useState<RecipeSettingsSnapshot | null>(null)
  const [values, setValues] = useState<RecipeValues | null>(null)
  const [loading, setLoading] = useState(false)
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState('')
  const [saved, setSaved] = useState(false)
  const [tab, setTab] = useState<'parameters' | 'models' | 'prompts'>('parameters')
  const [promptDirty, setPromptDirty] = useState(false)
  const [promptBusy, setPromptBusy] = useState(false)
  const [seedError, setSeedError] = useState(false)
  const savingRef = useRef(false)
  const request = useRef<AbortController | null>(null)
  const label = (key: string) => t(`distillation.settings.${key}`)

  async function load() {
    request.current?.abort()
    const controller = new AbortController()
    request.current = controller
    setLoading(true)
    setError('')
    setSaved(false)
    setSeedError(false)
    try {
      const result = await getRecipeSettings(controller.signal)
      if (!controller.signal.aborted) {
        setSnapshot(result)
        setValues(result.settings)
      }
    } catch (failure) {
      if (!controller.signal.aborted) setError(errorMessage(failure))
    } finally {
      if (!controller.signal.aborted) setLoading(false)
    }
  }

  useEffect(() => () => request.current?.abort(), [])

  async function save(event: React.FormEvent) {
    event.preventDefault()
    if (!snapshot || !values || savingRef.current || promptDirty || promptBusy || seedError) return
    savingRef.current = true
    setSaving(true)
    setError('')
    setSaved(false)
    const controller = new AbortController()
    request.current = controller
    try {
      const result = await saveRecipeSettings(snapshot.revision, values, controller.signal)
      if (!controller.signal.aborted) {
        setSnapshot(result)
        setValues(result.settings)
        setSaved(true)
        onSaved()
      }
    } catch (failure) {
      if (!controller.signal.aborted) setError(errorMessage(failure))
    } finally {
      savingRef.current = false
      if (!controller.signal.aborted) setSaving(false)
    }
  }

  const numeric = (
    key: 'questions_per_source' | 'batch_size' | 'max_inflight_requests',
    min: number
  ) => (
    <label className="space-y-1 text-sm">
      <span className="block">{label(key)}</span>
      <Input
        type="number"
        min={min}
        step={1}
        value={values?.[key] ?? ''}
        disabled={loading || saving}
        onChange={(event) =>
          setValues((current) => current && { ...current, [key]: Number(event.target.value) })
        }
      />
    </label>
  )
  const valid =
    snapshot?.editable &&
    values &&
    !promptDirty &&
    !promptBusy &&
    !seedError &&
    values.scenario_instructions.trim() &&
    values.classification_instructions.trim() &&
    values.inspection_instructions.trim() &&
    [values.questions_per_source, values.batch_size, values.max_inflight_requests].every(
      (value) => Number.isInteger(value) && value > 0
    ) &&
    (values.max_prompt_chars === null ||
      (Number.isInteger(values.max_prompt_chars) && values.max_prompt_chars > 0))

  return (
    <Dialog
      open={open}
      onOpenChange={(next) => {
        if (savingRef.current || promptDirty || promptBusy) return
        setOpen(next)
        if (next) void load()
        else {
          request.current?.abort()
          setLoading(false)
        }
      }}
    >
      <DialogTrigger asChild>
        <Button type="button" size="sm" variant="outline" disabled={disabled}>
          <Settings2Icon />
          {label('button')}
        </Button>
      </DialogTrigger>
      <DialogContent className="max-h-[90dvh] overflow-y-auto sm:max-w-4xl">
        <DialogHeader>
          <DialogTitle>{label('title')}</DialogTitle>
          <DialogDescription>{label('description')}</DialogDescription>
        </DialogHeader>
        {loading && <p role="status">{label('loading')}</p>}
        {snapshot && values && (
          <form onSubmit={(event) => void save(event)} className="space-y-5">
            <div className="bg-muted/50 space-y-1 rounded-md p-3 text-sm">
              <p>
                {t('distillation.settings.version', {
                  recipe: snapshot.recipe_schema_version,
                  installed: snapshot.installed_schema_version,
                  workflow: snapshot.workflow_name
                })}
              </p>
              {snapshot.installed_commit && (
                <p className="text-muted-foreground text-xs">
                  {label('installedCommit')}: {snapshot.installed_commit}
                </p>
              )}
              <p className="text-muted-foreground text-xs">
                {Object.entries(snapshot.models)
                  .map(([name, model]) => `${name}: ${model}`)
                  .join(' · ')}
              </p>
            </div>
            <details className="text-muted-foreground text-xs break-all">
              <summary>{label('configLocation')}</summary>
              <p>{snapshot.recipe_file}</p>
              <p>{snapshot.settings_file}</p>
            </details>
            {!snapshot.editable && (
              <p role="alert" className="text-destructive text-sm">
                {label('incompatible')}
              </p>
            )}
            <div role="tablist" aria-label={label('tabs')} className="flex flex-wrap gap-2">
              {(['parameters', 'models', 'prompts'] as const).map((name) => (
                <Button
                  key={name}
                  type="button"
                  role="tab"
                  aria-selected={tab === name}
                  disabled={saving || promptDirty || promptBusy}
                  variant={tab === name ? 'default' : 'outline'}
                  onClick={() => setTab(name)}
                >
                  {label(`tab_${name}`)}
                </Button>
              ))}
            </div>
            <fieldset disabled={saving || loading || !snapshot.editable} className="space-y-4">
              {tab === 'parameters' && (
                <div role="tabpanel" aria-label={label('tab_parameters')} className="space-y-4">
                  <div className="grid gap-4 sm:grid-cols-2">
                    {numeric('questions_per_source', 1)}
                    {numeric('batch_size', 1)}
                    {numeric('max_inflight_requests', 1)}
                    <label className="space-y-1 text-sm">
                      <span className="block">{label('max_prompt_chars')}</span>
                      <Input
                        type="number"
                        min={1}
                        step={1}
                        value={values.max_prompt_chars ?? ''}
                        placeholder={label('unlimited')}
                        onChange={(event) =>
                          setValues({
                            ...values,
                            max_prompt_chars:
                              event.target.value === '' ? null : Number(event.target.value)
                          })
                        }
                      />
                    </label>
                    <label className="space-y-1 text-sm">
                      <span className="block">{label('log_level')} (log_level)</span>
                      <select
                        className="bg-background w-full rounded-md border p-2"
                        value={values.log_level}
                        onChange={(event) =>
                          setValues({
                            ...values,
                            log_level: event.target.value as RecipeValues['log_level']
                          })
                        }
                      >
                        {['DEBUG', 'INFO', 'WARNING', 'ERROR', 'CRITICAL'].map((level) => (
                          <option key={level}>{level}</option>
                        ))}
                      </select>
                    </label>
                  </div>
                  <label className="flex items-center gap-2 text-sm">
                    <input
                      type="checkbox"
                      checked={values.deduplicate_qa}
                      onChange={(event) =>
                        setValues({ ...values, deduplicate_qa: event.target.checked })
                      }
                    />
                    {label('deduplicate_qa')}
                  </label>
                  {(
                    [
                      'scenario_instructions',
                      'classification_instructions',
                      'inspection_instructions'
                    ] as const
                  ).map((key) => (
                    <label key={key} className="block space-y-1 text-sm">
                      <span className="block">
                        {label(key)} ({key})
                      </span>
                      <textarea
                        required
                        rows={3}
                        className="border-input bg-background w-full rounded-md border p-2"
                        value={values[key]}
                        onChange={(event) => setValues({ ...values, [key]: event.target.value })}
                      />
                    </label>
                  ))}
                  <label className="block space-y-1 text-sm">
                    <span className="block">
                      {label('seed_question_path')} (seed_question_path)
                    </span>
                    <Input
                      key={snapshot.revision}
                      defaultValue={
                        values.seed_question_path === null
                          ? ''
                          : JSON.stringify(values.seed_question_path)
                      }
                      placeholder='["question"]'
                      onChange={(event) => {
                        try {
                          const path: unknown = event.target.value.trim()
                            ? JSON.parse(event.target.value)
                            : null
                          if (
                            path !== null &&
                            (!Array.isArray(path) ||
                              !path.every(
                                (part: unknown) =>
                                  typeof part === 'string' ||
                                  (typeof part === 'number' && Number.isInteger(part))
                              ))
                          )
                            throw new Error('Invalid path')
                          setValues({
                            ...values,
                            seed_question_path: path as RecipeValues['seed_question_path']
                          })
                          setSeedError(false)
                        } catch {
                          setSeedError(true)
                        }
                      }}
                    />
                    <span className="text-muted-foreground text-xs">{label('seedHint')}</span>
                    {seedError && (
                      <span role="alert" className="text-destructive text-xs">
                        {label('invalidSeed')}
                      </span>
                    )}
                  </label>
                </div>
              )}
              {tab === 'models' && (
                <div role="tabpanel" aria-label={label('tab_models')}>
                  <RecipeModelSettings snapshot={snapshot} values={values} onChange={setValues} />
                </div>
              )}
              {tab === 'prompts' && (
                <div role="tabpanel" aria-label={label('tab_prompts')}>
                  <WorkflowPromptEditor
                    values={values}
                    onChange={setValues}
                    onDirtyChange={setPromptDirty}
                    onBusyChange={setPromptBusy}
                  />
                </div>
              )}
            </fieldset>
            {saved && (
              <p role="status" className="text-sm text-emerald-600">
                {label('saved')}
              </p>
            )}
            {error && (
              <p role="alert" className="text-destructive text-sm whitespace-pre-wrap">
                {error}
              </p>
            )}
            <DialogFooter>
              <Button
                type="button"
                variant="outline"
                disabled={saving || loading || promptDirty || promptBusy}
                onClick={() => void load()}
              >
                {label('reload')}
              </Button>
              <DialogClose asChild>
                <Button
                  type="button"
                  variant="outline"
                  disabled={saving || promptDirty || promptBusy}
                >
                  {label('close')}
                </Button>
              </DialogClose>
              <Button type="submit" disabled={!valid || saving || loading}>
                {label(saving ? 'saving' : 'save')}
              </Button>
            </DialogFooter>
          </form>
        )}
        {!snapshot && error && (
          <div>
            <p role="alert" className="text-destructive text-sm">
              {error}
            </p>
            <Button type="button" variant="outline" onClick={() => void load()}>
              {label('reload')}
            </Button>
          </div>
        )}
      </DialogContent>
    </Dialog>
  )
}
