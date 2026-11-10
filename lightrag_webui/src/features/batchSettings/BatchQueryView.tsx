import { useEffect, useState } from 'react'
import { useTranslation } from 'react-i18next'
import type { BatchClient } from '@/api/batchControl'
import { createQuerySettingsStore, type QuerySettingsStore } from '@/stores/querySettings'
import type { RetrievalHistoryStore } from '@/stores/retrievalHistory'
import RetrievalView from '@/features/RetrievalView'
import Button from '@/components/ui/Button'
import { queryDefaults } from './batchQueryDefaults'

export default function BatchQueryView({ client, batch, identity, historyStore, active = true }: {
  client: BatchClient; batch: string; identity: string; historyStore: RetrievalHistoryStore; active?: boolean
}) {
  const { t } = useTranslation()
  const [ready, setReady] = useState<{ scope: string; client: BatchClient; store: QuerySettingsStore } | null>(null)
  const [error, setError] = useState<{ scope: string; message: string } | null>(null)
  const [refresh, setRefresh] = useState(0)
  const scope = JSON.stringify([identity, batch])
  useEffect(() => {
    if (!active) return
    const controller = new AbortController()
    void client.settings(batch, controller.signal).then((snapshot) => {
      if (controller.signal.aborted) return
      const values = snapshot.active_configuration.retrieval.ordinary ?? {}
      const defaults = queryDefaults(values)
      const version = JSON.stringify(Object.entries(defaults).sort(([a], [b]) => a.localeCompare(b)))
      const key = `lightrag::batch-query-settings::${encodeURIComponent(scope)}::${encodeURIComponent(version)}`
      setReady({ scope, client, store: createQuerySettingsStore(key, defaults) })
      setError(null)
    }).catch((failure) => {
      if (!controller.signal.aborted) setError({ scope, message: String(failure) })
    })
    return () => controller.abort()
  }, [batch, client, scope, refresh, active])
  if (error?.scope === scope) return <div className="space-y-3 p-6"><p role="alert">{error.message}</p><Button onClick={() => { setError(null); setRefresh((value) => value + 1) }}>{t('batchWorkbench.queryRetry')}</Button></div>
  if (!ready || ready.scope !== scope || ready.client !== client) return <p role="status" className="p-6">{t('batchWorkbench.queryLoading')}</p>
  return <RetrievalView settingsStore={ready.store} historyStore={historyStore} />
}
