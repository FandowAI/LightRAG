import { useEffect, useRef, useState } from 'react'
import { findBatchWorkflow, getWorkflowConfiguration, getWorkflowJob, retryWorkflowJob, startWorkflowJob, submitBatchWorkflow, type WorkflowConfiguration, type WorkflowJob } from '@/api/dataWorkflow'
import { backendBaseUrl } from '@/lib/constants'
import { useAuthStore } from '@/stores/state'
import { useBatchContext } from '@/stores/batchContext'

export function useDataWorkflow(batchId: string | null) {
  const username = useAuthStore(state => state.username) || 'guest'
  const workspace = useBatchContext(state => state.batch?.id) || ''
  const storageKey = `lightrag-workflow:${backendBaseUrl}:${username}:${workspace}`
  const [configuration, setConfiguration] = useState<WorkflowConfiguration | null>(null)
  const [job, setJob] = useState<WorkflowJob | null>(null)
  const restoreKey = `${storageKey}:${batchId ?? 'monitor'}`
  const [restoredSource, setRestoredSource] = useState<string | null>(null)
  const restoring = restoredSource !== restoreKey
  const [submitting, setSubmitting] = useState(false)
  const [error, setError] = useState<'connection' | 'submit' | null>(null)
  const submittingRef = useRef(false)
  const submission = useRef<AbortController | null>(null)
  const retryRequest = useRef<{ jobId: string; id: string } | null>(null)
  const [configurationRevision, setConfigurationRevision] = useState(0)
  const sourceJob = job?.source.batch_id === batchId ? job : null
  const canRetry = !!sourceJob && ['failed', 'interrupted'].includes(sourceJob.status)
  const monitoringOther = !!job && !job.terminal && !sourceJob

  useEffect(() => {
    const request = new AbortController()
    let timer: ReturnType<typeof setTimeout> | undefined
    const load = async () => {
      try {
        const value = await getWorkflowConfiguration(request.signal)
        if (!request.signal.aborted) setConfiguration(value)
      } catch { if (!request.signal.aborted) timer = setTimeout(load, 3000) }
    }
    void load()
    return () => { request.abort(); clearTimeout(timer) }
  }, [configurationRevision])

  useEffect(() => {
    const request = new AbortController()
    let timer: ReturnType<typeof setTimeout> | undefined
    let remembered: string | null = null
    try { remembered = sessionStorage.getItem(storageKey) } catch { /* Optional session cache. */ }
    const restore = async () => {
      try {
        const value = batchId ? await findBatchWorkflow(batchId, request.signal) : remembered ? await getWorkflowJob(remembered, request.signal) : null
        if (request.signal.aborted) return
        setJob(previous => !value && previous && !previous.terminal ? previous : value)
        setRestoredSource(restoreKey); setError(null)
      } catch (failure) {
        if (request.signal.aborted) return
        if (!batchId && (failure as { status?: number }).status === 404) {
          try { sessionStorage.removeItem(storageKey) } catch { /* Optional cache. */ }
          setJob(null); setRestoredSource(restoreKey)
        } else { setError('connection'); timer = setTimeout(restore, 3000) }
      }
    }
    void restore()
    return () => { request.abort(); submission.current?.abort(); clearTimeout(timer) }
  }, [batchId, storageKey, restoreKey])

  useEffect(() => {
    if (!job) return
    try { sessionStorage.setItem(storageKey, job.job_id) } catch { /* Optional cache. */ }
    if (job.terminal || !job.poll_after_ms) return
    const request = new AbortController()
    const timer = setTimeout(async () => {
      try {
        const value = await getWorkflowJob(job.job_id, request.signal)
        if (!request.signal.aborted) { setJob(value); setError(null) }
      } catch {
        if (!request.signal.aborted) { setError('connection'); setJob(previous => previous && { ...previous, poll_after_ms: 3000 }) }
      }
    }, job.poll_after_ms)
    return () => { request.abort(); clearTimeout(timer) }
  }, [job, storageKey])

  const submit = async () => {
    if (!batchId || submittingRef.current || restoring || monitoringOther || (sourceJob && !sourceJob.can_start && !canRetry)) return
    submittingRef.current = true; setSubmitting(true); setError(null)
    const request = new AbortController()
    submission.current = request
    try {
      let created: WorkflowJob
      if (sourceJob && canRetry) {
        if (retryRequest.current?.jobId !== sourceJob.job_id) retryRequest.current = { jobId: sourceJob.job_id, id: globalThis.crypto?.randomUUID?.() ?? `${Date.now()}-${Math.random().toString(36).slice(2)}` }
        created = await retryWorkflowJob(sourceJob.job_id, retryRequest.current.id, request.signal)
      } else if (sourceJob?.can_start) created = await startWorkflowJob(sourceJob.job_id, request.signal)
      else created = await submitBatchWorkflow(batchId, request.signal)
      if (!request.signal.aborted) setJob(created)
    } catch { if (!request.signal.aborted) setError('submit') }
    finally { submittingRef.current = false; if (!request.signal.aborted) setSubmitting(false) }
  }
  const refreshConfiguration = () => { setConfiguration(null); setConfigurationRevision(value => value + 1) }
  return { configuration, job, sourceJob, monitoringOther, restoring, submitting, error, submit, canRetry, refreshConfiguration }
}
