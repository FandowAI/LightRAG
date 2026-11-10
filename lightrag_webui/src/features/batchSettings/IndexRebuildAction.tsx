import { useMemo, useRef, useState } from 'react'
import { useTranslation } from 'react-i18next'
import Button from '@/components/ui/Button'
import { requestId, type BatchClient, type BatchHttpError, type IndexRebuild } from '@/api/batchControl'
import useRequestScope from '@/hooks/useRequestScope'

export default function IndexRebuildAction({ client, batch, revision, activeProfile, profiles, allowed, disabled, onStarted }: {
  client: BatchClient
  batch: string
  revision: string
  activeProfile: string
  profiles: { id: string; model_name: string; dimensions: number | null }[]
  allowed: boolean
  disabled: boolean
  onStarted: (record: IndexRebuild) => void
}) {
  const { t } = useTranslation()
  const scope = useMemo(() => ({ client, batch, revision }), [client, batch, revision])
  const begin = useRequestScope(scope)
  const [selection, setSelection] = useState<{ scope: object; value: string } | null>(null)
  const [busy, setBusy] = useState<object | null>(null)
  const [error, setError] = useState<{ scope: object; message: string; uncertain: boolean } | null>(null)
  const attempt = useRef<{ scope: object; profile: string; id: string } | null>(null)
  const acting = useRef<object | null>(null)
  const profile = selection?.scope === scope ? selection.value : activeProfile
  const uncertain = error?.scope === scope && error.uncertain
  const blocked = disabled || busy === scope || !profiles.some((item) => item.id === profile && item.dimensions)
  async function start() {
    if (!allowed || blocked || acting.current === scope) return
    const request = begin()
    if (!request.current()) { request.finish(); return }
    let pending = attempt.current
    if (!pending || pending.scope !== scope || pending.profile !== profile) {
      pending = { scope, profile, id: requestId() }
      attempt.current = pending
    }
    acting.current = scope
    setBusy(scope)
    setError(null)
    try {
      const record = await client.startIndexRebuild(batch, revision, profile, pending.id, request.signal)
      if (!request.current()) return
      if (record.data_batch_id !== batch) throw new Error(t('batchIndex.wrongBatch'))
      if (attempt.current === pending) attempt.current = null
      onStarted(record)
    } catch (failure) {
      if (request.current()) {
        const status = (failure as BatchHttpError).status
        const definitive = status !== undefined && [400, 401, 403, 404, 405, 413, 422].includes(status)
        if (definitive && attempt.current === pending) attempt.current = null
        setError({ scope, message: String(failure), uncertain: !definitive })
      }
    } finally {
      if (acting.current === scope) acting.current = null
      if (request.current()) setBusy(null)
      request.finish()
    }
  }
  if (!allowed) return null
  return <section aria-label={t('batchIndex.action')} className="space-y-3 rounded-lg border p-4">
    <h2 className="font-medium">{t('batchIndex.action')}</h2>
    <p className="text-sm text-muted-foreground">{t('batchIndex.help')}</p>
    <div className="flex flex-wrap items-end gap-3">
      <label className="space-y-1 text-sm">{t('batchIndex.profile')}
        <select aria-label={t('batchIndex.profile')} className="block rounded border bg-background p-2" value={profile} disabled={disabled || busy === scope || uncertain} onChange={(event) => setSelection({ scope, value: event.target.value })}>
          {profiles.filter((item) => item.dimensions).map((item) => <option key={item.id} value={item.id}>{item.id} · {item.model_name} · {item.dimensions}</option>)}
        </select>
      </label>
      <Button type="button" disabled={blocked} onClick={() => void start()}>{t('batchIndex.start')}</Button>
    </div>
    {uncertain && <p className="text-sm">{t('batchIndex.uncertain')}</p>}
    {error?.scope === scope && <p role="alert" className="text-sm text-destructive">{error.message}</p>}
  </section>
}
