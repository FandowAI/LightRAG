import { useEffect, useMemo, useState } from 'react'
import { useTranslation } from 'react-i18next'
import Button from '@/components/ui/Button'
import Input from '@/components/ui/Input'
import { createBatchClient, requestId, type BatchSettings, type BatchClient } from '@/api/batchControl'
import { useBatchContext } from '@/stores/batchContext'
import { useSettingsStore } from '@/stores/settings'
import useRequestScope from '@/hooks/useRequestScope'

export default function BatchChunkingSettings({ client: injectedClient }: { client?: BatchClient } = {}) {
  const { t } = useTranslation()
  const batch = useBatchContext((value) => value.batch)
  const apiKey = useSettingsStore.use.apiKey()
  const client = useMemo(() => injectedClient ?? createBatchClient(apiKey), [apiKey, injectedClient])
  const context = useMemo(() => ({ batch: batch?.id, client }), [batch?.id, client])
  const begin = useRequestScope(context)
  const [snapshot, setSnapshot] = useState<BatchSettings | null>(null)
  const [strategy, setStrategy] = useState('paragraph_semantic')
  const [size, setSize] = useState(2000)
  const [overlap, setOverlap] = useState(100)
  const [error, setError] = useState('')
  const [busy, setBusy] = useState(false)
  const [refresh, setRefresh] = useState(0)
  useEffect(() => {
    if (!batch) return
    const request = begin()
    let timer: ReturnType<typeof setTimeout> | undefined
    const load = async () => {
      try {
        const next = await client.settings(batch.id, request.signal)
        if (!request.current()) return
        setSnapshot(next)
        setStrategy(next.configuration.rag.chunking_strategy)
        setSize(next.configuration.rag.chunk_size)
        setOverlap(next.configuration.rag.chunk_overlap_size)
        if (next.batch.pending && next.application?.state !== 'awaiting_action') timer = setTimeout(() => void load(), 2000)
      } catch (failure) { if (request.current()) setError(String(failure)) }
    }
    void load()
    return () => { request.cancel(); if (timer) clearTimeout(timer) }
  }, [batch, client, begin, refresh])
  if (!batch) return null
  const pending = Boolean(snapshot?.batch.pending)
  const valid = Number.isInteger(size) && size > 0 && Number.isInteger(overlap) && overlap >= 0 && overlap < size
  const editable = snapshot?.permissions.edit && snapshot.permissions.apply_config
  const changed = snapshot && (strategy !== snapshot.configuration.rag.chunking_strategy || size !== snapshot.configuration.rag.chunk_size || overlap !== snapshot.configuration.rag.chunk_overlap_size)
  async function save(resume = false) {
    if (!snapshot || !batch || busy) return
    const request = begin()
    setBusy(true); setError('')
    try {
      if (resume && snapshot.batch.pending && snapshot.batch.desired) {
        await client.apply(batch.id, snapshot.batch.pending, snapshot.batch.desired, requestId())
      } else {
        const configuration = structuredClone(snapshot.configuration)
        Object.assign(configuration.rag, { chunking_strategy: strategy, chunk_size: size, chunk_overlap_size: overlap })
        const saved = await client.save(batch.id, { expected_revision: snapshot.batch.active, request_id: requestId(), configuration })
        if (!request.current()) return
        if (saved.requires_restart) {
          if (!saved.operation) throw new Error('Missing configuration application identity')
          await client.apply(batch.id, saved.operation, saved.revision, requestId())
        }
      }
      if (request.current()) setRefresh((value) => value + 1)
    } catch (failure) {
      if (request.current()) { setError(String(failure)); setRefresh((value) => value + 1) }
    } finally { if (request.current()) setBusy(false); request.finish() }
  }
  return <section className="space-y-3 rounded border p-3" aria-label={t('documentChunking.title')}>
    <h3 className="font-medium">{t('documentChunking.title')}</h3>
    <p className="text-xs text-muted-foreground">{t('documentChunking.hint')}</p>
    {error && <p role="alert" className="text-sm text-destructive">{error}</p>}
    <fieldset disabled={!snapshot || !editable || busy || pending} className="space-y-3">
      <label className="flex items-center justify-between gap-4 text-sm">{t('documentChunking.strategy')}
        <select aria-label={t('documentChunking.strategy')} className="rounded border bg-background p-2" value={strategy} onChange={(event) => setStrategy(event.target.value)}>
          {['paragraph_semantic', 'semantic_vector', 'recursive_character', 'fixed_token'].map((value) => <option key={value} value={value}>{t(`documentChunking.strategies.${value}`)}</option>)}
          {strategy === 'token' && <option value="token">{t('documentChunking.strategies.fixed_token')}</option>}
        </select>
      </label>
      <label className="flex items-center justify-between gap-4 text-sm">{t('documentChunking.size')}<Input aria-label={t('documentChunking.size')} className="w-28" type="number" min={1} value={size} onChange={(event) => setSize(Number(event.target.value))} /></label>
      <label className="flex items-center justify-between gap-4 text-sm">{t('documentChunking.overlap')}<Input aria-label={t('documentChunking.overlap')} className="w-28" type="number" min={0} max={size - 1} value={overlap} onChange={(event) => setOverlap(Number(event.target.value))} /></label>
      <Button type="button" disabled={!valid || !changed} onClick={() => void save()}>{t('documentChunking.save')}</Button>
    </fieldset>
    {pending && <p role="status" className="text-sm">{t('documentChunking.applying')}</p>}
    {snapshot?.application?.state === 'awaiting_action' && <Button type="button" disabled={busy || !editable} onClick={() => void save(true)}>{t('documentChunking.apply')}</Button>}
    {snapshot?.application?.state === 'rolled_back' && <p role="alert">{t('documentChunking.rolledBack')}</p>}
  </section>
}
