import { lazy, Suspense, useEffect, useId, useRef, useState } from 'react'
import { useTranslation } from 'react-i18next'
import { queryEntityCenters, type EntityCenterCandidate } from '@/api/lightrag'
import Button from '@/components/ui/Button'
import Input from '@/components/ui/Input'
import { batchIsActive, useBatchRetrieval } from './retrieval/useBatchRetrieval'
import { useDataWorkflow } from './distillation/useDataWorkflow'
import DataWorkflowProgress from './distillation/DataWorkflowProgress'
import DataWorkflowSettingsDialog from './distillation/DataWorkflowSettingsDialog'
import DistillationStartButton from './distillation/DistillationStartButton'
import RetrievalProgress from './retrieval/RetrievalProgress'
import RetrievalSettingsDialog, { type RetrievalSettings } from './retrieval/RetrievalSettingsDialog'
const PackagePreview = lazy(() => import('./retrieval/PackagePreview'))
import { useBatchContext } from '@/stores/batchContext'
import { useAuthStore } from '@/stores/state'
import { backendBaseUrl } from '@/lib/constants'

function readPreferences(key: string): RetrievalSettings {
  const defaults = { min_neighbors: 3, min_documents: 2, graph_depth: 2, rag_top_k: 10 }
  try {
    const saved = JSON.parse(localStorage.getItem(key) || 'null')
    if (saved && Object.entries({ min_neighbors: [1, 1000], min_documents: [1, 100], graph_depth: [0, 5], rag_top_k: [1, 100] }).every(([field, [min, max]]) => Number.isInteger(saved[field]) && saved[field] >= min && saved[field] <= max)) return saved
  } catch { /* Browser preferences are optional. */ }
  return defaults
}

export default function BatchRetrievalView() {
  const { t } = useTranslation()
  const id = useId()
  const [candidates, setCandidates] = useState<EntityCenterCandidate[] | null>(null)
  const [selected, setSelected] = useState<Set<string>>(new Set())
  const [centers, setCenters] = useState<string[]>([])
  const [finding, setFinding] = useState(false)
  const [candidateError, setCandidateError] = useState(false)
  const request = useRef<AbortController | null>(null)
  const workspace = useBatchContext(state => state.batch?.id) || ''
  const username = useAuthStore(state => state.username) || 'guest'
  const preferenceKey = `lightrag-retrieval-settings:${backendBaseUrl}:${username}:${workspace}`
  const [thresholds, setThresholds] = useState(() => { const value = readPreferences(preferenceKey); return { min_neighbors: value.min_neighbors, min_documents: value.min_documents } })
  const [settings, setSettings] = useState(() => { const value = readPreferences(preferenceKey); return { graph_depth: value.graph_depth, rag_top_k: value.rag_top_k } })
  const [step, setStep] = useState(1)
  const [routed, setRouted] = useState<string | null>(null)
  const batch = useBatchRetrieval()
  const active = batchIsActive(batch.job)
  const workflow = useDataWorkflow(batch.job?.id ?? null)
  const distilling = !!workflow.job && !workflow.job.terminal
  const packagesReady = !!batch.job && ['completed', 'completed_with_errors'].includes(batch.job.status) && !!batch.job.package_count && !!batch.job.export_path
  if (batch.job && routed !== batch.job.id) { setRouted(batch.job.id); setStep(2) }
  const saveSettings = (value: RetrievalSettings) => {
    setThresholds({ min_neighbors: value.min_neighbors, min_documents: value.min_documents })
    setSettings({ graph_depth: value.graph_depth, rag_top_k: value.rag_top_k })
    try { localStorage.setItem(preferenceKey, JSON.stringify(value)) } catch { /* Browser preferences are optional. */ }
  }
  useEffect(() => () => request.current?.abort(), [])

  const find = async (event: React.FormEvent) => {
    event.preventDefault()
    if (finding) return
    const controller = new AbortController()
    request.current = controller
    setFinding(true); setCandidateError(false); setCandidates(null); setSelected(new Set()); setCenters([])
    try {
      const result = await queryEntityCenters(thresholds, controller.signal)
      if (controller.signal.aborted) return
      const recommended = result.candidates.filter(row => row.tier === 'recommended')
      setCandidates(recommended); setSelected(new Set(recommended.map(row => row.name)))
    } catch { if (!controller.signal.aborted) setCandidateError(true) }
    finally { if (!controller.signal.aborted) setFinding(false) }
  }
  const toggle = (name: string) => setSelected(previous => {
    const next = new Set(previous)
    if (next.has(name)) next.delete(name); else next.add(name)
    return next
  })
  const configure = () => {
    setCenters((candidates ?? []).filter(row => selected.has(row.name)).map(row => row.name))
    setStep(2)
  }
  const reset = () => { if (!active && !distilling) { batch.reset(); setCenters([]); setStep(1) } }
  const chosen = batch.job ? batch.job.total : centers.length

  return <div className="size-full overflow-y-auto">
    <main className="mx-auto max-w-[1600px] space-y-5 px-4 py-6 pb-12 sm:px-7">
      <header className="flex flex-wrap items-start justify-between gap-4">
        <div><h1 className="text-2xl font-semibold tracking-tight">{t('batch.title')}</h1><p className="mt-2 text-sm text-muted-foreground">{t('batchFlow.subtitle')}</p></div>
        <div className="flex flex-wrap gap-2"><RetrievalSettingsDialog value={{ ...thresholds, ...settings }} onSave={saveSettings} /><DataWorkflowSettingsDialog onSaved={workflow.refreshConfiguration} /></div>
      </header>
      <p className="text-xs text-muted-foreground">{t('batchFlow.settingsHint')}</p>
      <nav aria-label={t('batchFlow.route')} className="grid grid-cols-2 gap-2 lg:grid-cols-4">
        {[1, 2, 3, 4].map(value => <button key={value} type="button" aria-current={step === value ? 'step' : undefined} onClick={() => setStep(value)} className={`flex items-center gap-3 rounded-lg border p-3 text-left text-sm transition-colors ${step === value ? 'border-primary bg-primary/5' : 'hover:bg-muted/50'}`}>
          <span className={`flex size-7 shrink-0 items-center justify-center rounded-full text-xs font-semibold ${step === value ? 'bg-primary text-primary-foreground' : 'bg-muted text-muted-foreground'}`}>{value}</span><span>{t(`batchFlow.steps.${value}`)}</span>
        </button>)}
      </nav>
      <div className="grid items-start gap-5 xl:grid-cols-[minmax(0,1fr)_minmax(360px,440px)]">
        <div className="min-w-0 space-y-4 rounded-xl border bg-card p-4 sm:p-6">
          {batch.restoring ? <p role="status" className="text-sm text-muted-foreground">{t('batch.restoring')}</p> : <>
            {step === 1 && <>
              <section className="space-y-4" aria-labelledby={`${id}-select`}>
                <div className="flex items-center justify-between gap-3">
                  <h2 id={`${id}-select`} className="font-medium">{t('batch.step_select')}</h2>
                  {(centers.length > 0 || batch.job) && <>
                    <span className="text-sm text-muted-foreground">{t('batch.chosen', { count: chosen })}</span>
                    {!active && !distilling && <Button type="button" variant="ghost" disabled={batch.busy} onClick={reset}>{t('batch.reselect')}</Button>}
                  </>}
                </div>
                {!centers.length && !batch.job && <>
                  <form onSubmit={find} aria-label={t('batch.candidate_form')} className="flex flex-wrap items-end gap-3">
                    {(['min_neighbors', 'min_documents'] as const).map(key => <div key={key} className="w-36 space-y-1">
                      <label htmlFor={`${id}-${key}`} className="text-sm text-muted-foreground">{t(`batch.${key}`)}</label>
                      <Input id={`${id}-${key}`} className="w-full" type="number" min={1} max={key === 'min_neighbors' ? 1000 : 100}
                        required value={thresholds[key]} disabled={finding} onChange={event => setThresholds(previous => ({ ...previous, [key]: Number(event.target.value) }))} />
                    </div>)}
                    <Button type="submit" disabled={finding}>{t(finding ? 'batch.finding' : 'batch.find')}</Button>
                  </form>
                  {candidateError && <p role="alert" className="text-sm text-destructive">{t('batch.errors.candidates')}</p>}
                  {candidates && <>
                    <div className="max-h-64 overflow-auto rounded-md border">
                      <table className="w-full text-left text-sm">
                        <caption className="sr-only">{t('batch.candidates')}</caption>
                        <thead className="sticky top-0 bg-background"><tr className="border-b">
                          <th className="w-10 p-3"><input type="checkbox" aria-label={t('batch.select_all')} disabled={!candidates.length}
                            checked={candidates.length > 0 && selected.size === candidates.length}
                            onChange={event => setSelected(event.target.checked ? new Set(candidates.map(row => row.name)) : new Set())} /></th>
                          <th scope="col" className="py-3">{t('batch.entity')}</th>
                          <th scope="col" className="p-3 text-right font-normal text-muted-foreground">{t('batch.neighbors')}</th>
                          <th scope="col" className="p-3 text-right font-normal text-muted-foreground">{t('batch.documents')}</th>
                        </tr></thead>
                        <tbody>{candidates.map(row => <tr key={row.name} className="border-b last:border-0">
                          <td className="p-3"><input type="checkbox" aria-label={t('batch.select_entity', { name: row.name })}
                            checked={selected.has(row.name)} onChange={() => toggle(row.name)} /></td>
                          <th scope="row" className="py-2 font-normal">{row.name}</th>
                          <td className="p-3 text-right tabular-nums text-muted-foreground">{row.neighbor_count}</td>
                          <td className="p-3 text-right tabular-nums text-muted-foreground">{row.document_count}</td>
                        </tr>)}</tbody>
                      </table>
                      {!candidates.length && <p className="p-3 text-sm text-muted-foreground">{t('batch.no_candidates')}</p>}
                    </div>
                    <div className="flex flex-wrap items-center justify-between gap-3">
                      <p className="text-sm text-muted-foreground">{t('batch.selection', { selected: selected.size, total: candidates.length })}</p>
                      <Button type="button" disabled={!selected.size} onClick={configure}>{t('batch.configure')}</Button>
                    </div>
                  </>}
                </>}
              </section>

              {(centers.length > 0 || batch.job) && <Button variant="outline" onClick={() => setStep(2)}>{t('batchFlow.toRetrieval')}</Button>}</>}
            {step === 2 && <section className="space-y-4" aria-label={t('batchFlow.steps.2')}>
              <h2 className="font-medium">{t('batchFlow.steps.2')}</h2>
              {!centers.length && !batch.job ? <p className="text-sm text-muted-foreground">{t('batchFlow.selectFirst')}</p> : <>
                {batch.job ? <p className="text-sm text-muted-foreground">{t('batch.settings_summary', batch.job.settings)}</p> : <form
                  onSubmit={event => { event.preventDefault(); void batch.start(centers, settings) }} aria-label={t('batch.start_form')} className="flex flex-wrap items-end gap-3">
                  {(['graph_depth', 'rag_top_k'] as const).map(key => <div key={key} className="w-40 space-y-1">
                    <label htmlFor={`${id}-${key}`} className="text-sm text-muted-foreground">{t(`batch.${key}`)}</label>
                    <Input id={`${id}-${key}`} className="w-full" type="number" min={key === 'graph_depth' ? 0 : 1} max={key === 'graph_depth' ? 5 : 100}
                      required value={settings[key]} disabled={batch.busy} onChange={event => setSettings(previous => ({ ...previous, [key]: Number(event.target.value) }))} />
                  </div>)}
                  <Button type="submit" disabled={batch.busy}>{t(batch.busy ? 'batch.starting' : 'batch.start')}</Button>
                </form>}

                <p className="text-xs text-muted-foreground">{t('batchFlow.retrievalHint')}</p>
                <Button variant="outline" disabled={!packagesReady} onClick={() => setStep(3)}>{t('batchFlow.toPackages')}</Button>
              </>}
            </section>}
            {step === 3 && <section className="space-y-4" aria-label={t('batchFlow.steps.3')}>
              <h2 className="font-medium">{t('batchFlow.steps.3')}</h2>
              {packagesReady && batch.job ? <><Suspense fallback={<p role="status">{t('batchFlow.loadingResults')}</p>}><PackagePreview key={batch.job.id} job={batch.job} /></Suspense><Button onClick={() => setStep(4)}>{t('batchFlow.toDistillation')}</Button></> : <p className="text-sm text-muted-foreground">{t('batchFlow.packagesPending')}</p>}
            </section>}
            {step === 4 && <section className="space-y-4" aria-label={t('batchFlow.steps.4')}>
              <h2 className="font-medium">{t('batchFlow.steps.4')}</h2>
              <p className="text-sm text-muted-foreground">{t('batchFlow.distillationHint')}</p>
              <DistillationStartButton workflow={workflow} available={packagesReady} />
            </section>}
          </>}
          {batch.error && <p role="alert" className="text-sm text-destructive">{t(`batch.errors.${batch.error}`)}</p>}
        </div>
        <aside className="min-w-0 space-y-5 rounded-xl border bg-card p-4 sm:p-5 xl:sticky xl:top-4" aria-label={t('batchFlow.monitor')}>
          <div><h2 className="font-semibold">{t('batchFlow.monitor')}</h2><p className="mt-1 text-xs text-muted-foreground">{t('batchFlow.monitorHint')}</p></div>
          <RetrievalProgress batch={batch} />
          <div className="border-t pt-4">{workflow.job ? <DataWorkflowProgress key={workflow.job.job_id} job={workflow.job} /> : <p className="text-sm text-muted-foreground">{t('batchFlow.noDistillation')}</p>}</div>
        </aside>
      </div>
    </main>
  </div>
}
