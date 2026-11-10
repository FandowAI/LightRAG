import { useEffect, useMemo, useRef, useState } from 'react'
import { useTranslation } from 'react-i18next'
import Button from '@/components/ui/Button'
import type { BatchClient, IndexRebuild } from '@/api/batchControl'
import useRequestScope from '@/hooks/useRequestScope'

export default function BatchIndexProgress({ client, batch, initial, onFinished }: { client: BatchClient; batch: string; initial?: IndexRebuild | null; onFinished?: () => void }) {
  const { t } = useTranslation()
  const scope = useMemo(() => ({ client, batch }), [client, batch])
  const begin = useRequestScope(scope)
  const [loaded, setLoaded] = useState<{ scope: object; items: IndexRebuild[] } | null>(null)
  const [selected, setSelected] = useState<{ scope: object; id: string } | null>(null)
  const [loadError, setLoadError] = useState<{ scope: object; message: string } | null>(null)
  const [actionError, setActionError] = useState<{ scope: object; message: string } | null>(null)
  const [acting, setActing] = useState<object | null>(null)
  const action = useRef<object | null>(null)
  const seen = useRef(new Set<string>())
  const notified = useRef(new Set<string>())
  const [refresh, setRefresh] = useState(0)
  const items = useMemo(() => loaded?.scope === scope ? loaded.items : initial?.data_batch_id === batch ? [initial] : [], [loaded, scope, initial, batch])
  const current = items.find((item) => selected?.scope === scope && item.operation_id === selected.id) ?? items[0]
  useEffect(() => {
    const request = begin()
    let timer: ReturnType<typeof setTimeout> | undefined
    async function poll() {
      try {
        const result = await client.indexRebuilds(batch, request.signal)
        if (!request.current()) return
        if (result.items.some((item) => item.data_batch_id !== batch)) throw new Error(t('batchIndex.wrongBatch'))
        setLoaded((previous) => ({ scope, items: result.items.map((item) => {
          const prior = previous?.scope === scope ? previous.items.find((value) => value.operation_id === item.operation_id) : undefined
          return prior && prior.version > item.version ? prior : item
        }) }))
        setLoadError(null)
        if (result.items.every((item) => item.terminal)) return
      } catch (failure) {
        if (!request.current()) return
        setLoadError({ scope, message: String(failure) })
      }
      if (request.current()) timer = setTimeout(() => void poll(), 1500)
    }
    void poll()
    return () => { request.cancel(); if (timer) clearTimeout(timer) }
  }, [begin, batch, client, scope, refresh, t])
  useEffect(() => {
    for (const item of items) {
      const key = `${batch}:${item.operation_id}`
      if (!item.terminal) seen.current.add(key)
      else if (seen.current.has(key) && !notified.current.has(key)) {
        notified.current.add(key)
        onFinished?.()
      }
    }
  }, [items, batch, onFinished])
  async function act(recover: boolean) {
    if (!current || action.current === scope || (recover ? !current.can_recover : !current.can_cancel)) return
    const request = begin()
    if (!request.current()) { request.finish(); return }
    action.current = scope
    setActing(scope)
    setActionError(null)
    try {
      const next = recover ? await client.recoverIndexRebuild(batch, current.operation_id, current.version, request.signal) : await client.cancelIndexRebuild(batch, current.operation_id, request.signal)
      if (!request.current()) return
      if (next.data_batch_id !== batch || next.operation_id !== current.operation_id) throw new Error(t('batchIndex.wrongBatch'))
      setLoaded((value) => ({ scope, items: [next, ...(value?.scope === scope ? value.items : items).filter((item) => item.operation_id !== next.operation_id)] }))
      setRefresh((value) => value + 1)
    } catch (failure) {
      if (request.current()) setActionError({ scope, message: String(failure) })
    } finally {
      if (action.current === scope) action.current = null
      if (request.current()) setActing(null)
      request.finish()
    }
  }
  const queued = current?.provider_admission.items.reduce((sum, item) => sum + item.queued, 0) ?? 0
  const active = current?.provider_admission.items.reduce((sum, item) => sum + item.active, 0) ?? 0
  return <section aria-label={t('batchIndex.progress')} className="space-y-3 rounded-lg border p-4">
    <div className="flex flex-wrap items-center justify-between gap-2"><h2 className="font-medium">{t('batchIndex.progress')}</h2><Button type="button" size="sm" variant="outline" onClick={() => setRefresh((value) => value + 1)}>{t('batchIndex.refresh')}</Button></div>
    {items.length > 0 && <label className="block space-y-1 text-sm">{t('batchIndex.history')}<select aria-label={t('batchIndex.history')} className="block w-full rounded border bg-background p-2" value={current?.operation_id ?? ''} onChange={(event) => setSelected({ scope, id: event.target.value })}>{items.map((item) => <option key={item.operation_id} value={item.operation_id}>{item.operation_id} · {t(`batchIndex.states.${item.state}`, { defaultValue: item.state })}</option>)}</select></label>}
    {!current && <p className="text-sm">{t('batchIndex.empty')}</p>}
    {current && <>
      <p role="status">{t(`batchIndex.states.${current.state}`, { defaultValue: current.state })}</p>
      <p className="text-sm">{current.target.embedding_model} · {current.target.dimensions}</p>
      {current.progress && <p className="text-sm">{t('batchIndex.counts', current.progress)}</p>}
      {current.progress && current.progress.total > 0 && <progress aria-label={t('batchIndex.verified')} className="w-full" max={current.progress.total} value={current.progress.verified} />}
      {(queued > 0 || active > 0) && <p role="status" aria-label={t('batchWorkbench.modelAdmission')} className="text-sm">{t('batchWorkbench.providerQueued', { count: queued })}{' · '}{t('batchWorkbench.providerActive', { count: active })}</p>}
      {current.cancel_requested && !current.terminal && <p className="text-sm">{t('batchIndex.cancelling')}</p>}
      {current.waiting_reason && <p className="text-sm">{current.waiting_reason}</p>}
      {current.error && <p className="text-sm text-destructive">{current.error}</p>}
      {current.rollback_error && <p className="text-sm text-destructive">{current.rollback_error}</p>}
      <div className="flex flex-wrap gap-2">
        {current.can_cancel && <Button type="button" variant="outline" disabled={acting === scope || current.cancel_requested} onClick={() => void act(false)}>{t('batchIndex.cancel')}</Button>}
        {current.can_recover && <Button type="button" disabled={acting === scope} onClick={() => void act(true)}>{t('batchIndex.recover')}</Button>}
      </div>
    </>}
    {loadError?.scope === scope && <p role="alert" className="text-sm text-destructive">{loadError.message}</p>}
    {actionError?.scope === scope && <p role="alert" className="text-sm text-destructive">{actionError.message}</p>}
  </section>
}
