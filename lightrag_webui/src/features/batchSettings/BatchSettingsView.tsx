import { useCallback, useEffect, useRef, useState } from 'react'
import { useTranslation } from 'react-i18next'
import Button from '@/components/ui/Button'
import Input from '@/components/ui/Input'
import {
  requestId,
  type BatchClient,
  type BatchConfiguration,
  type BatchHttpError,
  type BatchRecord,
  type BatchSettings,
  type Copies,
  type CopyGroup,
  type DraftRequest,
  type Preview
} from '@/api/batchControl'
import type { WorkflowStage } from '@/api/dataWorkflow'
import ConfigurationFields from './ConfigurationFields'
import { sections, type Section } from './sections'
import { useBatchContext } from '@/stores/batchContext'
import ApplicationProgress from './ApplicationProgress'
import IndexRebuildAction from './IndexRebuildAction'
import BatchIndexProgress from './BatchIndexProgress'

export default function BatchSettingsView({ client, onEnter, initialBatch }: { client: BatchClient; onEnter?: (batch: BatchRecord) => void | Promise<void>; initialBatch?: string }) {
  const { t } = useTranslation()
  const [batches, setBatches] = useState<BatchRecord[]>([])
  const [batch, setBatch] = useState(initialBatch ?? '')
  const [snapshot, setSnapshot] = useState<BatchSettings | null>(null)
  const [draft, setDraft] = useState<BatchConfiguration | null>(null)
  const [prompts, setPrompts] = useState<Record<WorkflowStage, string> | null>(null)
  const [copies, setCopies] = useState<Copies>({})
  const [source, setSource] = useState('')
  const [groups, setGroups] = useState<CopyGroup[]>([])
  const [name, setName] = useState('')
  const [section, setSection] = useState<Section>('rag')
  const [review, setReview] = useState<{ result: Preview; request: DraftRequest } | null>(null)
  const [error, setError] = useState('')
  const [notice, setNotice] = useState('')
  const [loading, setLoading] = useState(true)
  const [busy, setBusy] = useState(false)
  const [stale, setStale] = useState(false)
  const [invalid, setInvalid] = useState(false)
  const [refresh, setRefresh] = useState(0)
  const acting = useRef(false)
  const mounted = useRef(true)
  const selected = useRef(batch)
  useEffect(() => { selected.current = batch }, [batch])
  const form = useRef<HTMLFormElement>(null)
  const createAttempt = useRef<{ body: string; id: string } | null>(null)
  const dirty =
    !!snapshot &&
    !!draft &&
    (invalid || JSON.stringify(draft) !== JSON.stringify(snapshot.configuration) ||
      JSON.stringify(prompts) !== JSON.stringify(snapshot.prompts))
  const pending = Boolean(snapshot?.batch.pending)
  const disabled = busy || loading || pending || !snapshot?.permissions.edit || stale
  useEffect(() => {
    useBatchContext.setState({ unsaved: dirty })
    return () => { useBatchContext.setState({ unsaved: false }) }
  }, [dirty])

  useEffect(() => {
    mounted.current = true
    return () => {
      mounted.current = false
    }
  }, [])
  useEffect(() => {
    const controller = new AbortController()
    void client
      .list(controller.signal)
      .then((response) => {
        if (controller.signal.aborted) return
        setBatches(response.items)
        setBatch((current) => current || response.items[0]?.id || '')
        setError('')
        setLoading(false)
      })
      .catch((failure) => {
        if (!controller.signal.aborted) {
          setError(
            (failure as BatchHttpError).status === 404
              ? t('batchSettings.unavailable')
              : String(failure)
          )
          setLoading(false)
        }
      })
    return () => controller.abort()
  }, [client, t])
  useEffect(() => {
    if (!batch) return
    const controller = new AbortController()
    void Promise.resolve()
      .then(() => {
        if (controller.signal.aborted) return
        setLoading(true)
        setReview(null)
        setStale(false)
        setInvalid(false)
        return client.settings(batch, controller.signal)
      })
      .then((result) => {
        if (!result || controller.signal.aborted) return
        setSnapshot(result)
        setDraft(structuredClone(result.configuration))
        setPrompts({ ...result.prompts })
        setCopies({})
        setError('')
        setBatches((current) => current.map((item) => (item.id === batch ? result.batch : item)))
      })
      .catch((failure) => {
        if (!controller.signal.aborted) setError(String(failure))
      })
      .finally(() => {
        if (!controller.signal.aborted) setLoading(false)
      })
    return () => controller.abort()
  }, [batch, client, refresh])
  const reload = useCallback(() => setRefresh((value) => value + 1), [])

  function change(next: BatchConfiguration) {
    if (!disabled) {
      setDraft(next)
      setReview(null)
      setNotice('')
    }
  }
  function prompt(stage: WorkflowStage, content: string) {
    if (!disabled && prompts) {
      setPrompts({ ...prompts, [stage]: content })
      setReview(null)
      setNotice('')
    }
  }

  async function act(action: () => Promise<void>) {
    if (acting.current) return
    acting.current = true
    setBusy(true)
    setError('')
    try {
      await action()
    } catch (failure) {
      if (mounted.current) {
        setError(String(failure))
        if ((failure as BatchHttpError).status === 409) {
          setStale(true)
          setReview(null)
        }
      }
    } finally {
      acting.current = false
      if (mounted.current) setBusy(false)
    }
  }
  const selectedCopies = (): Copies => {
    const origin = batches.find((item) => item.id === source)
    return origin
      ? Object.fromEntries(
        groups.map((group) => [group, { batch: origin.id, revision: origin.active }])
      )
      : {}
  }
  async function create() {
    if (!name.trim() || dirty) return
    const selection = selectedCopies()
    const body = JSON.stringify({ name: name.trim(), copies: selection })
    if (createAttempt.current?.body !== body) createAttempt.current = { body, id: requestId() }
    await act(async () => {
      const result = await client.create(
        name.trim(),
        createAttempt.current!.id,
        Object.keys(selection).length ? selection : undefined
      )
      if (!mounted.current) return
      setBatches((current) => [...current.filter((item) => item.id !== result.id), result])
      setBatch(result.id)
      setName('')
      setGroups([])
      setSource('')
      setNotice(t('batchSettings.created'))
      createAttempt.current = null
    })
  }
  async function preview() {
    if (!snapshot || !draft || !prompts || disabled || invalid || !form.current?.reportValidity()) return
    const id = batch
    const contents = Object.fromEntries(
      (Object.keys(prompts) as WorkflowStage[])
        .filter((stage) => prompts[stage] !== snapshot.prompts[stage])
        .map((stage) => [stage, prompts[stage]])
    )
    const request: DraftRequest = {
      expected_revision: snapshot.batch.active,
      request_id: requestId(),
      configuration: structuredClone(draft),
      ...(Object.keys(copies).length ? { copies } : {}),
      ...(Object.keys(contents).length ? { prompt_contents: contents } : {})
    }
    await act(async () => {
      const result = await client.preview(id, request)
      if (mounted.current && selected.current === id) setReview({ result, request })
    })
  }
  async function save() {
    if (!review || disabled) return
    const id = batch
    await act(async () => {
      const result = await client.save(id, review.request)
      if (!mounted.current || selected.current !== id) return
      setNotice(
        t(
          result.status === 'pending_restart'
            ? 'batchSettings.savedPending'
            : result.status === 'unchanged'
              ? 'batchSettings.unchanged'
              : 'batchSettings.saved'
        )
      )
      setReview(null)
      reload()
    })
  }
  async function copy() {
    if (!snapshot || dirty || disabled || !source || !groups.length) return
    const id = batch,
      selection = selectedCopies()
    await act(async () => {
      const result = await client.copy(id, snapshot.batch.active, selection)
      if (!mounted.current || selected.current !== id) return
      setDraft(result.draft)
      setPrompts(result.prompts)
      setCopies(selection)
      setReview(null)
      setNotice(t('batchSettings.copyDraft'))
    })
  }

  return (
    <div className="mx-auto max-w-6xl space-y-5 p-4 sm:p-6">
      <header className="space-y-1">
        <h1 className="text-xl font-semibold">{t('batchSettings.title')}</h1>
        <p className="text-muted-foreground text-sm">{t('batchSettings.subtitle')}</p>
        {onEnter && snapshot && <Button disabled={loading || busy || dirty || pending} onClick={() => void act(async () => { await onEnter(snapshot.batch) })}>{t('batchWorkbench.enter')}</Button>}
      </header>
      {error && (
        <p
          role="alert"
          className="text-destructive rounded-md border p-3 text-sm whitespace-pre-wrap"
        >
          {error}
        </p>
      )}
      {notice && (
        <p role="status" className="rounded-md border p-3 text-sm">
          {notice}
        </p>
      )}
      <div className="flex flex-wrap items-end gap-3">
        <label className="min-w-56 flex-1 space-y-1 text-sm">
          <span className="block">{t('batchSettings.batch')}</span>
          <select
            aria-label={t('batchSettings.batch')}
            className="bg-background w-full rounded-md border p-2"
            value={batch}
            disabled={dirty || busy || loading}
            onChange={(event) => {
              if (!dirty && !acting.current) {
                setSnapshot(null)
                setDraft(null)
                setPrompts(null)
                setBatch(event.target.value)
                setNotice('')
              }
            }}
          >
            {!batch && <option value="">{t('batchSettings.noBatches')}</option>}
            {batches.map((item) => (
              <option key={item.id} value={item.id}>
                {item.name} · {item.id}
              </option>
            ))}
          </select>
        </label>
        <label className="space-y-1 text-sm">
          <span className="block">{t('batchSettings.newName')}</span>
          <Input
            value={name}
            aria-label={t('batchSettings.newName')}
            maxLength={256}
            disabled={dirty || busy}
            onChange={(event) => setName(event.target.value)}
          />
        </label>
        <Button disabled={!name.trim() || dirty || busy} onClick={() => void create()}>
          {t('batchSettings.create')}
        </Button>
      </div>
      {dirty && (
        <p role="status" className="text-sm text-amber-600">
          {t('batchSettings.unsaved')}
        </p>
      )}
      <details className="rounded-lg border p-4">
        <summary className="cursor-pointer font-medium">{t('batchSettings.copyTitle')}</summary>
        <div className="mt-3 space-y-3">
          <p className="text-muted-foreground text-sm">{t('batchSettings.copyHint')}</p>
          <select
            aria-label={t('batchSettings.copySource')}
            className="bg-background w-full rounded-md border p-2"
            value={source}
            disabled={dirty || busy || pending}
            onChange={(event) => setSource(event.target.value)}
          >
            <option value="">{t('batchSettings.chooseSource')}</option>
            {batches
              .filter((item) => item.id !== batch)
              .map((item) => (
                <option key={item.id} value={item.id}>
                  {item.name} · {item.active}
                </option>
              ))}
          </select>
          <div className="flex flex-wrap gap-4">
            {(['rag', 'recipe', 'bridge'] as CopyGroup[]).map((group) => (
              <label key={group} className="flex items-center gap-2 text-sm">
                <input
                  type="checkbox"
                  disabled={dirty || busy || pending}
                  checked={groups.includes(group)}
                  onChange={(event) =>
                    setGroups((current) =>
                      event.target.checked
                        ? [...current, group]
                        : current.filter((item) => item !== group)
                    )
                  }
                />
                {t(`batchSettings.sections.${group}`)}
              </label>
            ))}
          </div>
          <p className="text-muted-foreground text-sm">{t('batchSettings.retrievalNotCopied')}</p>
          <Button
            variant="outline"
            disabled={!source || !groups.length || dirty || disabled}
            onClick={() => void copy()}
          >
            {t('batchSettings.copyPreview')}
          </Button>
        </div>
      </details>
      {loading && <p role="status">{t('batchSettings.loading')}</p>}
      {snapshot && draft && prompts && !loading && (
        <>
          <div className="space-y-1 rounded-lg border p-3 text-sm">
            <p>
              {t('batchSettings.activeVersion')}:{' '}
              <code className="break-all">{snapshot.batch.active}</code>
            </p>
            {snapshot.batch.desired && (
              <p>
                {t('batchSettings.pendingVersion')}:{' '}
                <code className="break-all">{snapshot.batch.desired}</code>
              </p>
            )}
            <p>
              {t('batchSettings.workspace')}:{' '}
              <code className="break-all">{snapshot.batch.workspace}</code>
            </p>
          </div>
          {snapshot.application && (
            <ApplicationProgress
              key={`${batch}:${snapshot.application.operation_id}:${refresh}`}
              client={client}
              batch={batch}
              initial={snapshot.application}
              canApply={
                snapshot.permissions.edit &&
                snapshot.permissions.apply_config &&
                snapshot.permissions.run_runtime &&
                snapshot.permissions.stop_runtime
              }
              onFinished={reload}
            />
          )}
          <IndexRebuildAction client={client} batch={batch} revision={snapshot.batch.active} activeProfile={snapshot.active_configuration.rag.embedding_profile} profiles={snapshot.options.profiles} allowed={Boolean(snapshot.permissions.rebuild_index && snapshot.permissions.edit && snapshot.permissions.run_runtime && snapshot.permissions.stop_runtime)} disabled={disabled || dirty} onStarted={reload} />
          {snapshot.index_rebuild && <BatchIndexProgress key={batch} client={client} batch={batch} initial={snapshot.index_rebuild} onFinished={reload} />}
          {stale && <p role="alert">{t('batchSettings.stale')}</p>}
          <div
            role="tablist"
            aria-label={t('batchSettings.sectionsLabel')}
            className="flex flex-wrap gap-2"
          >
            {sections.map((item) => (
              <Button
                key={item}
                role="tab"
                aria-selected={section === item}
                aria-controls={`batch-settings-${item}`}
                variant={section === item ? 'default' : 'outline'}
                onClick={() => setSection(item)}
              >
                {t(`batchSettings.sections.${item}`)}
              </Button>
            ))}
          </div>
          <form
            ref={form}
            onChange={(event) => {
              const controls = Array.from(event.currentTarget.elements)
              const next = controls.some(
                (control) =>
                  'willValidate' in control && control.willValidate &&
                  'validity' in control && !(control.validity as ValidityState).valid
              )
              setInvalid(next)
              if (next) setReview(null)
            }}
            onSubmit={(event) => {
              event.preventDefault()
              void preview()
            }}
          >
            <fieldset disabled={disabled} className="rounded-lg border p-4">
              {sections.map((item) => (
                <div
                  key={`${batch}:${refresh}:${item}`}
                  id={`batch-settings-${item}`}
                  role="tabpanel"
                  aria-label={t(`batchSettings.sections.${item}`)}
                  hidden={section !== item}
                >
                  <ConfigurationFields
                    snapshot={snapshot}
                    value={draft}
                    onChange={change}
                    prompts={prompts}
                    onPrompt={prompt}
                    section={item}
                  />
                </div>
              ))}
            </fieldset>
            {invalid && <p role="alert" className="mt-3 text-destructive text-sm">{t('batchSettings.invalid')}</p>}
            <div className="mt-4 flex flex-wrap gap-2">
              <Button type="submit" disabled={disabled || invalid || !dirty}>
                {t('batchSettings.preview')}
              </Button>
              <Button
                type="button"
                variant="outline"
                disabled={busy || (!dirty && !stale)}
                onClick={reload}
              >
                {t('batchSettings.reload')}
              </Button>
            </div>
          </form>
          {review && (
            <section
              aria-label={t('batchSettings.review')}
              className="space-y-3 rounded-lg border p-4"
            >
              <h2 className="font-medium">{t('batchSettings.review')}</h2>
              <p>
                {t(
                  review.result.requires_restart
                    ? 'batchSettings.willRestart'
                    : 'batchSettings.willApply'
                )}
              </p>
              <div className="overflow-x-auto">
                <table className="w-full text-sm">
                  <thead>
                    <tr>
                      <th className="p-2 text-left">{t('batchSettings.field')}</th>
                      <th className="p-2 text-left">{t('batchSettings.before')}</th>
                      <th className="p-2 text-left">{t('batchSettings.after')}</th>
                    </tr>
                  </thead>
                  <tbody>
                    {review.result.differences.map((row) => (
                      <tr key={row.field} className="border-t">
                        <td className="p-2">{row.field}</td>
                        <td className="max-w-72 p-2 break-all">{JSON.stringify(row.before)}</td>
                        <td className="max-w-72 p-2 break-all">{JSON.stringify(row.after)}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
              <Button disabled={disabled} onClick={() => void save()}>
                {t('batchSettings.save')}
              </Button>
            </section>
          )}
        </>
      )}
    </div>
  )
}
