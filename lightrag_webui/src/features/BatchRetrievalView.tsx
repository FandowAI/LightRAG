import { useEffect, useId, useRef, useState } from 'react'
import { useTranslation } from 'react-i18next'
import { queryEntityCenters, type EntityCenterCandidate } from '@/api/lightrag'
import Button from '@/components/ui/Button'
import Input from '@/components/ui/Input'
import { batchIsActive, useBatchRetrieval } from './retrieval/useBatchRetrieval'

export default function BatchRetrievalView() {
  const { t } = useTranslation()
  const id = useId()
  const [thresholds, setThresholds] = useState({ min_neighbors: 3, min_documents: 2 })
  const [candidates, setCandidates] = useState<EntityCenterCandidate[] | null>(null)
  const [selected, setSelected] = useState<Set<string>>(new Set())
  const [settings, setSettings] = useState({ graph_depth: 2, rag_top_k: 10 })
  const [centers, setCenters] = useState<string[]>([])
  const [finding, setFinding] = useState(false)
  const [candidateError, setCandidateError] = useState(false)
  const request = useRef<AbortController | null>(null)
  const configurePanel = useRef<HTMLElement | null>(null)
  const batch = useBatchRetrieval()
  const active = batchIsActive(batch.job)
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
    setTimeout(() => { configurePanel.current?.focus(); configurePanel.current?.scrollIntoView?.({ behavior: 'smooth', block: 'nearest' }) }, 0)
  }
  const reset = () => { batch.reset(); setCenters([]) }
  const percentage = batch.job ? Math.floor(batch.job.processed / Math.max(batch.job.total, 1) * 100) : 0
  const chosen = batch.job ? batch.job.total : centers.length

  return <div className="size-full overflow-y-auto">
    <main className="mx-auto max-w-4xl space-y-7 px-5 py-6 pb-12 sm:px-8">
      <h1 className="text-2xl font-semibold tracking-tight">{t('batch.title')}</h1>
      {batch.restoring ? <p role="status" className="text-sm text-muted-foreground">{t('batch.restoring')}</p> : <>
        <section className="space-y-4" aria-labelledby={`${id}-select`}>
          <div className="flex items-center justify-between gap-3">
            <h2 id={`${id}-select`} className="font-medium">{t('batch.step_select')}</h2>
            {(centers.length > 0 || batch.job) && <>
              <span className="text-sm text-muted-foreground">{t('batch.chosen', { count: chosen })}</span>
              {!active && <Button type="button" variant="ghost" disabled={batch.busy} onClick={reset}>{t('batch.reselect')}</Button>}
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

        {(centers.length > 0 || batch.job) && <section ref={configurePanel} tabIndex={-1} className="space-y-4 border-t pt-5 outline-none" aria-labelledby={`${id}-retrieve`}>
          <h2 id={`${id}-retrieve`} className="font-medium">{t('batch.step_retrieve')}</h2>
          {batch.job ? <p className="text-sm text-muted-foreground">{t('batch.settings_summary', batch.job.settings)}</p> : <form
            onSubmit={event => { event.preventDefault(); void batch.start(centers, settings) }} aria-label={t('batch.start_form')} className="flex flex-wrap items-end gap-3">
            {(['graph_depth', 'rag_top_k'] as const).map(key => <div key={key} className="w-40 space-y-1">
              <label htmlFor={`${id}-${key}`} className="text-sm text-muted-foreground">{t(`batch.${key}`)}</label>
              <Input id={`${id}-${key}`} className="w-full" type="number" min={key === 'graph_depth' ? 0 : 1} max={key === 'graph_depth' ? 5 : 100}
                required value={settings[key]} disabled={batch.busy} onChange={event => setSettings(previous => ({ ...previous, [key]: Number(event.target.value) }))} />
            </div>)}
            <Button type="submit" disabled={batch.busy}>{t(batch.busy ? 'batch.starting' : 'batch.start')}</Button>
          </form>}
        </section>}

        {batch.job && <section className="space-y-4 border-t pt-5" aria-labelledby={`${id}-packages`}>
          <div className="flex items-center justify-between gap-3">
            <h2 id={`${id}-packages`} className="font-medium">{t('batch.step_packages')}</h2>
            {active && <Button type="button" variant="ghost" disabled={batch.busy || batch.job.cancel_requested} onClick={() => void batch.stop()}>{t(batch.job.cancel_requested ? 'batch.stopping' : 'batch.stop')}</Button>}
          </div>
          <div className="flex flex-wrap items-baseline justify-between gap-2" role="status">
            <span className="text-sm text-muted-foreground">{t('batch.progress', { processed: batch.job.processed, total: batch.job.total })}</span>
            <span className="text-lg font-semibold tabular-nums">{t('batch.package_count', { count: batch.job.package_count })}</span>
          </div>
          <progress aria-label={t('batch.progress_label')} value={active && batch.job.phase ? batch.job.phase_done || 0 : batch.job.processed} max={active && batch.job.phase ? Math.max(batch.job.phase_total || 0, 1) : batch.job.total} className="h-2 w-full accent-emerald-500" />
          {!!batch.job.duplicate_chunks && <p className="text-sm text-muted-foreground">{t('batch.deduplicated', { count: batch.job.duplicate_chunks, skipped: batch.job.skipped || 0 })}</p>}
          <p className="text-sm text-muted-foreground">{active
            ? batch.job.cancel_requested ? t('batch.wait_stop') : batch.job.phase
              ? t(`batch.phase.${batch.job.phase}`, { done: batch.job.phase_done || 0, total: batch.job.phase_total || 0 })
              : t('batch.current', { name: batch.job.current_center || '…', percent: percentage })
            : t(`batch.status.${batch.job.status}`)}</p>
          {(batch.job.failed > 0 || batch.job.empty > 0) && <details className="text-sm text-muted-foreground">
            <summary className="cursor-pointer">{t('batch.exceptions', { failed: batch.job.failed, empty: batch.job.empty })}</summary>
            <ul className="mt-2 space-y-1">{batch.job.items.filter(item => ['failed', 'empty'].includes(item.status)).map(item =>
              <li key={item.center}>{item.center} · {t(`batch.item.${item.status}`)}</li>)}</ul>
          </details>}
          {batch.job.export_path && <p className="break-all text-xs text-muted-foreground">{t('batch.saved_path')}<code>{batch.job.export_path}</code></p>}
          <Button type="button" disabled={active || batch.busy || !batch.configured || !batch.job.package_count || batch.job.handoff.status === 'accepted'} onClick={() => void batch.submit()}>
            {t(batch.job.handoff.status === 'accepted' ? 'batch.submitted' : 'batch.submit')}
          </Button>
          {!batch.configured && <p className="text-xs text-muted-foreground">{t(batch.configured === false ? 'batch.distillation_pending' : 'batch.distillation_unknown')}</p>}
        </section>}
      </>}
      {batch.error && <p role="alert" className="text-sm text-destructive">{t(`batch.errors.${batch.error}`)}</p>}
    </main>
  </div>
}
