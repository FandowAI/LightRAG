import { useEffect, useMemo, useRef, useState } from 'react'
import { useSettingsStore } from '@/stores/settings'
import { createBatchClient, type BatchClient, type BatchRecord } from '@/api/batchControl'
import { useBatchContext } from '@/stores/batchContext'
import BatchSettingsView from './BatchSettingsView'

export default function BatchSettingsPage({ client: injectedClient }: { client?: BatchClient } = {}) {
  const apiKey = useSettingsStore.use.apiKey()
  const [previous, setPrevious] = useState(apiKey)
  const [epoch, setEpoch] = useState(0)
  if (previous !== apiKey) { setPrevious(apiKey); setEpoch(epoch + 1) }
  const defaultClient = useMemo(() => createBatchClient(apiKey), [apiKey])
  const client = injectedClient ?? defaultClient
  const entry = useRef<AbortController | null>(null)
  useEffect(() => () => entry.current?.abort(), [client])
  async function enter(batch: BatchRecord) {
    entry.current?.abort()
    const controller = new AbortController()
    entry.current = controller
    await client.workbenchHealth(batch.id, controller.signal)
    if (controller.signal.aborted) return
    useBatchContext.getState().select(batch)
    useSettingsStore.setState({ currentTab: 'documents' })
  }
  return <BatchSettingsView key={epoch} client={client} initialBatch={useBatchContext.getState().batch?.id} onEnter={enter} />
}
