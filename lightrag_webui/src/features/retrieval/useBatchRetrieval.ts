import { useCallback, useEffect, useRef, useState } from 'react'
import { getBatchRetrieval, startBatchRetrieval, stopBatchRetrieval, type BatchJob } from '@/api/lightrag'
import { backendBaseUrl } from '@/lib/constants'
import { useAuthStore } from '@/stores/state'

export const batchIsActive = (job: BatchJob | null) => !!job && ['queued', 'running'].includes(job.status)

export function useBatchRetrieval() {
  const username = useAuthStore(state => state.username) || 'guest'
  const storageKey = `lightrag-batch:${backendBaseUrl}:${username}`
  const [jobId, setJobId] = useState<string | null>(() => {
    try { return sessionStorage.getItem(storageKey) } catch { return null }
  })
  const [job, setJob] = useState<BatchJob | null>(null)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const mounted = useRef(true)
  useEffect(() => { mounted.current = true; return () => { mounted.current = false } }, [])

  const remember = useCallback((id: string | null) => {
    try { if (id) sessionStorage.setItem(storageKey, id); else sessionStorage.removeItem(storageKey) } catch { /* Session storage may be disabled. */ }
    setJobId(id)
  }, [storageKey])

  useEffect(() => {
    if (!jobId) return
    const request = new AbortController()
    let timer: ReturnType<typeof setTimeout>
    const poll = async () => {
      try {
        const result = await getBatchRetrieval(jobId, request.signal)
        if (request.signal.aborted) return
        setJob(result); setError('')
        if (batchIsActive(result)) timer = setTimeout(poll, 1000)
      } catch (cause) {
        if (request.signal.aborted) return
        if ((cause as { status?: number }).status === 404) {
          remember(null); setJob(null); setError('missing')
        } else {
          setError('connection'); timer = setTimeout(poll, 3000)
        }
      }
    }
    void poll()
    return () => { request.abort(); clearTimeout(timer) }
  }, [jobId, remember])

  const start = async (centers: string[], settings: { graph_depth: number; rag_top_k: number }) => {
    if (busy || batchIsActive(job)) return
    setBusy(true); setError('')
    try {
      const result = await startBatchRetrieval({ centers, ...settings })
      if (!mounted.current) return
      setJob(result); remember(result.id)
    } catch (cause) {
      const failure = cause as Error & { status?: number }
      if (mounted.current) setError(failure.status === 409 ? 'conflict' : failure.status === 503 && failure.message.includes('embedding_unavailable') ? 'model' : 'start')
    } finally { if (mounted.current) setBusy(false) }
  }
  const stop = async () => {
    if (!job || busy) return
    setBusy(true)
    try { const result = await stopBatchRetrieval(job.id); if (mounted.current) setJob(result) } catch { if (mounted.current) setError('connection') }
    finally { if (mounted.current) setBusy(false) }
  }
  const reset = () => { if (!batchIsActive(job)) { setJob(null); remember(null); setError('') } }
  return { job, busy, error, restoring: !!jobId && !job, start, stop, reset }
}
