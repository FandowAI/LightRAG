import { useCallback, useEffect, useRef, useState } from 'react'
import { useTranslation } from 'react-i18next'
import Button from '@/components/ui/Button'
import Input from '@/components/ui/Input'
import { Dialog, DialogContent, DialogDescription, DialogHeader, DialogTitle } from '@/components/ui/Dialog'
import { requestId, type BatchClient, type BatchRecord } from '@/api/batchControl'
import { useBatchContext } from '@/stores/batchContext'
import { useAuthStore } from '@/stores/state'
import { isBatchOnlyDeployment } from '@/lib/runtimeConfig'
import { findRememberedBatch, lastBatchStorageKey } from './batchSelection'

export default function BatchWorkbenchSelector({ client }: { client: BatchClient }) {
  const { t } = useTranslation()
  const selected = useBatchContext((state) => state.batch)
  const username = useAuthStore((state) => state.username)
  const [batches, setBatches] = useState<BatchRecord[]>([])
  const [error, setError] = useState('')
  const [opening, setOpening] = useState(false)
  const [creating, setCreating] = useState(false)
  const [dialog, setDialog] = useState(false)
  const [name, setName] = useState('')
  const entry = useRef<AbortController | null>(null)
  const attempt = useRef<{ name: string; id: string } | null>(null)
  const restoredStorageKey = useRef<string | null>(null)

  const storageKey = lastBatchStorageKey(username)

  const open = useCallback(async (batch: BatchRecord | null) => {
    entry.current?.abort()
    const controller = new AbortController()
    entry.current = controller
    if (!batch) {
      localStorage.removeItem(storageKey)
      useBatchContext.getState().select(null)
      setOpening(false)
      return
    }
    setOpening(true)
    setError('')
    try {
      await client.workbenchHealth(batch.id, controller.signal)
      if (!controller.signal.aborted) {
        localStorage.setItem(storageKey, batch.id)
        useBatchContext.getState().select(batch)
      }
    } catch (failure) {
      if (!controller.signal.aborted) setError(String(failure))
    } finally {
      if (!controller.signal.aborted) setOpening(false)
    }
  }, [client, storageKey])

  useEffect(() => {
    const controller = new AbortController()
    void client.list(controller.signal).then((result) => {
      if (!controller.signal.aborted) { setBatches(result.items); setError('') }
    }).catch((failure) => {
      if (!controller.signal.aborted && failure.status !== 404) setError(String(failure))
    })
    return () => { controller.abort(); entry.current?.abort() }
  }, [client])

  useEffect(() => {
    if (selected || restoredStorageKey.current === storageKey || batches.length === 0) return
    restoredStorageKey.current = storageKey
    const rememberedId = localStorage.getItem(storageKey)
    if (!rememberedId) return
    const remembered = findRememberedBatch(batches, rememberedId)
    if (!remembered) {
      localStorage.removeItem(storageKey)
      return
    }
    // Opening a remembered batch is an external health-check side effect; the
    // callback selects the batch only after that check succeeds.
    // eslint-disable-next-line react-hooks/set-state-in-effect
    void open(remembered)
  }, [batches, open, selected, storageKey])
  async function create() {
    const value = name.trim()
    if (!value || creating) return
    if (attempt.current?.name !== value) attempt.current = { name: value, id: requestId() }
    const controller = new AbortController()
    entry.current?.abort()
    entry.current = controller
    setCreating(true); setError('')
    try {
      const batch = await client.create(value, attempt.current.id)
      if (controller.signal.aborted) return
      setBatches((items) => [...items.filter((item) => item.id !== batch.id), batch])
      attempt.current = null; setName(''); setDialog(false)
      await open(batch)
    } catch (failure) {
      if (!controller.signal.aborted) setError(String(failure))
    } finally { if (!controller.signal.aborted || !attempt.current) setCreating(false) }
  }
  return <section aria-label={t('batchWorkbench.context')} className="border-b px-4 py-2 text-sm">
    <div className="flex flex-wrap items-center gap-3">
      <label className="flex items-center gap-2">{t('batchWorkbench.current')}
        <select aria-label={t('batchWorkbench.current')} value={selected?.id ?? ''} disabled={opening || creating} onChange={(event) => void open(batches.find((item) => item.id === event.target.value) ?? null)} className="bg-background rounded-md border p-1">
          <option value="">{t(isBatchOnlyDeployment() ? 'batchWorkbench.choose' : 'batchWorkbench.legacy')}</option>
          {batches.map((batch) => <option key={batch.id} value={batch.id}>{batch.name}</option>)}
        </select>
      </label>
      <Button size="sm" variant="outline" disabled={opening || creating} onClick={() => setDialog(true)}>{t('batchSettings.create')}</Button>
      {opening && <span role="status">{t('batchWorkbench.opening')}</span>}
    </div>
    {error && <p role="alert" className="mt-2 text-destructive">{error}</p>}
    <Dialog open={dialog} onOpenChange={(value) => { if (!creating) setDialog(value) }}>
      <DialogContent>
        <DialogHeader><DialogTitle>{t('batchSettings.create')}</DialogTitle><DialogDescription>{t('batchWorkbench.createHint')}</DialogDescription></DialogHeader>
        <form className="space-y-3" onSubmit={(event) => { event.preventDefault(); void create() }}>
          <label className="block space-y-1">{t('batchSettings.newName')}<Input aria-label={t('batchSettings.newName')} value={name} onChange={(event) => setName(event.target.value)} required maxLength={200} disabled={creating} /></label>
          <Button type="submit" disabled={creating || !name.trim()}>{t(creating ? 'batchWorkbench.submitting' : 'batchSettings.create')}</Button>
        </form>
      </DialogContent>
    </Dialog>
  </section>
}
