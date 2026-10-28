import { useEffect, useRef, useState } from 'react'
import { useTranslation } from 'react-i18next'
import Button from '@/components/ui/Button'
import Input from '@/components/ui/Input'
import {
  listPromptFiles,
  readPromptFile,
  savePromptFile,
  previewPrompt,
  workflowStages,
  type PromptFileList,
  type RecipeValues,
  type WorkflowStage
} from '@/api/dataWorkflow'
import { errorMessage } from '@/lib/utils'

export default function WorkflowPromptEditor({
  values,
  onChange,
  onDirtyChange,
  onBusyChange
}: {
  values: RecipeValues
  onChange: (values: RecipeValues) => void
  onDirtyChange: (dirty: boolean) => void
  onBusyChange: (busy: boolean) => void
}) {
  const { t } = useTranslation()
  const [stage, setStage] = useState<WorkflowStage>('scenario')
  const [files, setFiles] = useState<PromptFileList>({ directory: '', items: [] })
  const [content, setContent] = useState('')
  const [original, setOriginal] = useState('')
  const [preview, setPreview] = useState('')
  const [variables, setVariables] = useState<string[]>([])
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const [notice, setNotice] = useState('')
  const sequence = useRef(0)
  const controller = useRef<AbortController | null>(null)
  const upload = useRef<HTMLInputElement | null>(null)
  const label = (key: string) => t(`distillation.settings.${key}`)
  const task = values.stages[stage]
  const path = task?.prompt_file
  const dirty = content !== original

  useEffect(() => {
    onDirtyChange(dirty)
  }, [dirty, onDirtyChange])
  useEffect(() => {
    onBusyChange(busy)
  }, [busy, onBusyChange])

  useEffect(() => {
    const request = new AbortController()
    controller.current?.abort()
    controller.current = request
    const id = ++sequence.current
    void Promise.resolve()
      .then(() => {
        if (request.signal.aborted) return
        setBusy(true)
        setError('')
        return Promise.all([
          listPromptFiles(stage, request.signal),
          readPromptFile(stage, path ?? '', request.signal)
        ])
      })
      .then((result) => {
        if (!result) return
        const [list, document] = result
        if (request.signal.aborted || id !== sequence.current) return
        setFiles(list)
        setContent(document?.content ?? '')
        setOriginal(document?.content ?? '')
        setPreview(document?.preview ?? '')
        setVariables(document?.variables ?? [])
      })
      .catch((failure) => {
        if (!request.signal.aborted) setError(errorMessage(failure))
      })
      .finally(() => {
        if (!request.signal.aborted) setBusy(false)
      })
    return () => {
      request.abort()
    }
  }, [stage, path])

  async function act(action: () => Promise<void>) {
    if (busy) return
    setBusy(true)
    setError('')
    setNotice('')
    try {
      await action()
    } catch (failure) {
      setError(errorMessage(failure))
    } finally {
      setBusy(false)
    }
  }

  function updateTask(key: 'model' | 'prompt' | 'parser' | 'prompt_file', value: string | null) {
    setNotice('')
    onChange({ ...values, stages: { ...values.stages, [stage]: { ...task, [key]: value } } })
  }

  return (
    <div className="space-y-4">
      <p className="text-muted-foreground text-sm">{label('promptHint')}</p>
      <div className="flex flex-wrap gap-2" role="group" aria-label={label('stages')}>
        {workflowStages.map((name) => (
          <Button
            key={name}
            type="button"
            variant={stage === name ? 'default' : 'outline'}
            size="sm"
            disabled={busy || dirty}
            onClick={() => {
              setNotice('')
              setStage(name)
            }}
          >
            {label(`stages_${name}`)}
          </Button>
        ))}
      </div>
      {task && (
        <>
          <div className="grid gap-3 sm:grid-cols-3">
            <label className="space-y-1 text-sm">
              <span className="block">{label('stageModel')} (model)</span>
              <select
                className="bg-background w-full rounded-md border p-2"
                value={task.model}
                disabled={busy}
                onChange={(e) => updateTask('model', e.target.value)}
              >
                {Object.keys(values.models).map((name) => (
                  <option key={name}>{name}</option>
                ))}
              </select>
            </label>
            {(['prompt', 'parser'] as const).map((key) => (
              <label key={key} className="space-y-1 text-sm">
                <span className="block">
                  {label(`stage_${key}`)} ({key})
                </span>
                <Input
                  required
                  value={task[key]}
                  disabled={busy}
                  onChange={(e) => updateTask(key, e.target.value)}
                />
              </label>
            ))}
          </div>
          <label className="block space-y-1 text-sm">
            <span className="block">{label('promptFile')} (prompt_file)</span>
            <select
              className="bg-background w-full rounded-md border p-2"
              value={path ?? ''}
              disabled={busy || dirty}
              onChange={(e) => updateTask('prompt_file', e.target.value || null)}
            >
              <option value="">{label('builtinPrompt')}</option>
              {path && !files.items.some((item) => item.path === path) && (
                <option value={path}>{path}</option>
              )}
              {files.items.map((item) => (
                <option key={item.path} value={item.path}>
                  {item.filename}
                </option>
              ))}
            </select>
          </label>
          <p className="text-muted-foreground text-xs break-all">
            {path || label('builtinPrompt')}
            <br />
            {files.directory}
          </p>
          <div className="flex flex-wrap gap-2">
            <input
              ref={upload}
              type="file"
              accept=".md,.txt,text/plain,text/markdown"
              className="hidden"
              aria-label={label('uploadPrompt')}
              disabled={busy || dirty}
              onChange={(e) => {
                const file = e.target.files?.[0]
                e.target.value = ''
                if (!file) return
                void act(async () => {
                  if (file.size > 256 * 1024) throw new Error(label('promptTooLarge'))
                  const text = new TextDecoder('utf-8', { fatal: true }).decode(
                    await file.arrayBuffer()
                  )
                  const document = await savePromptFile(stage, file.name, text)
                  setOriginal(document.content)
                  setContent(document.content)
                  updateTask('prompt_file', document.path)
                  setNotice(label('promptSaved'))
                })
              }}
            />
            <Button
              type="button"
              variant="outline"
              disabled={busy || dirty}
              onClick={() => upload.current?.click()}
            >
              {label('uploadPrompt')}
            </Button>
            <Button
              type="button"
              variant="outline"
              disabled={busy || !content.trim()}
              onClick={() =>
                void act(async () => setPreview((await previewPrompt(stage, content)).preview))
              }
            >
              {label('previewPrompt')}
            </Button>
            <Button
              type="button"
              disabled={busy || !dirty || !content.trim()}
              onClick={() =>
                void act(async () => {
                  const document = await savePromptFile(stage, `${stage}.md`, content)
                  setOriginal(document.content)
                  setContent(document.content)
                  updateTask('prompt_file', document.path)
                  setNotice(label('promptSaved'))
                })
              }
            >
              {label('savePrompt')}
            </Button>
            {dirty && (
              <Button
                type="button"
                variant="outline"
                disabled={busy}
                onClick={() => {
                  setContent(original)
                  setPreview('')
                }}
              >
                {label('discardPrompt')}
              </Button>
            )}
          </div>
          {variables.length > 0 && (
            <p className="text-muted-foreground text-xs">
              {label('promptVariables')}: {variables.map((name) => `$${name}`).join(', ')} · $$ → $
            </p>
          )}
          <label className="block space-y-1 text-sm">
            <span>{label('promptContent')}</span>
            <textarea
              className="bg-background w-full rounded-md border p-2 font-mono text-sm"
              rows={14}
              value={content}
              disabled={busy}
              onChange={(e) => {
                setContent(e.target.value)
                setPreview('')
                setNotice('')
              }}
            />
          </label>
          {dirty && (
            <p role="status" className="text-sm text-amber-600">
              {label('promptDirty')}
            </p>
          )}
          {preview && (
            <div>
              <p className="text-sm">{label('previewSample')}</p>
              <pre className="bg-muted max-h-80 overflow-auto rounded-md p-3 text-xs break-words whitespace-pre-wrap">
                {preview}
              </pre>
            </div>
          )}
        </>
      )}
      {busy && <p role="status">{label('loading')}</p>}
      {notice && (
        <p role="status" className="text-sm text-emerald-600">
          {notice}
        </p>
      )}
      {error && (
        <p role="alert" className="text-destructive text-sm whitespace-pre-wrap">
          {error}
        </p>
      )}
    </div>
  )
}
