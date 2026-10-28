import { useEffect, useRef, useState } from 'react'
import {
  findBatchWorkflow,
  getWorkflowConfiguration,
  getWorkflowJob,
  retryWorkflowJob,
  startWorkflowJob,
  submitBatchWorkflow,
  type WorkflowConfiguration,
  type WorkflowJob
} from '@/api/dataWorkflow'

export function useDataWorkflow(batchId: string) {
  const [configuration, setConfiguration] = useState<WorkflowConfiguration | null>(null)
  const [job, setJob] = useState<WorkflowJob | null>(null)
  const [restoring, setRestoring] = useState(true)
  const [submitting, setSubmitting] = useState(false)
  const [error, setError] = useState<'connection' | 'submit' | null>(null)
  const submittingRef = useRef(false)
  const submission = useRef<AbortController | null>(null)
  const latestJob = useRef<string | null>(null)
  const retryRequest = useRef<{ jobId: string; id: string } | null>(null)
  const [configurationRevision, setConfigurationRevision] = useState(0)
  const canRetry = !!job && ['failed', 'interrupted'].includes(job.status)

  useEffect(() => {
    const request = new AbortController()
    let restoreTimer: ReturnType<typeof setTimeout> | undefined
    let configTimer: ReturnType<typeof setTimeout> | undefined
    const loadConfiguration = async () => {
      try {
        const config = await getWorkflowConfiguration(request.signal)
        if (!request.signal.aborted) setConfiguration(config)
      } catch {
        if (!request.signal.aborted) configTimer = setTimeout(loadConfiguration, 3000)
      }
    }
    const restore = async () => {
      try {
        const restored = await findBatchWorkflow(batchId, request.signal)
        if (request.signal.aborted) return
        if (!latestJob.current) {
          setJob(restored)
          latestJob.current = restored?.job_id ?? null
        }
        setRestoring(false)
        setError(null)
      } catch {
        if (!request.signal.aborted) {
          setError('connection')
          restoreTimer = setTimeout(restore, 3000)
        }
      }
    }
    void loadConfiguration()
    void restore()
    return () => {
      request.abort()
      submission.current?.abort()
      clearTimeout(restoreTimer)
      clearTimeout(configTimer)
    }
  }, [batchId, configurationRevision])

  useEffect(() => {
    if (!job || job.terminal || !job.poll_after_ms) return
    const request = new AbortController()
    const timer = setTimeout(async () => {
      try {
        const updated = await getWorkflowJob(job.job_id, request.signal)
        if (!request.signal.aborted) {
          setJob(updated)
          setError(null)
        }
      } catch {
        if (!request.signal.aborted) {
          setError('connection')
          setJob((previous) => previous && { ...previous, poll_after_ms: 3000 })
        }
      }
    }, job.poll_after_ms)
    return () => {
      request.abort()
      clearTimeout(timer)
    }
  }, [job])

  const submit = async () => {
    if (submittingRef.current || restoring || (job && !job.can_start && !canRetry)) return
    submittingRef.current = true
    setSubmitting(true)
    setError(null)
    const request = new AbortController()
    submission.current = request
    try {
      let created: WorkflowJob
      if (job && canRetry) {
        if (retryRequest.current?.jobId !== job.job_id) {
          retryRequest.current = {
            jobId: job.job_id,
            id:
              globalThis.crypto?.randomUUID?.() ??
              `${Date.now()}-${Math.random().toString(36).slice(2)}`
          }
        }
        created = await retryWorkflowJob(job.job_id, retryRequest.current.id, request.signal)
      } else if (job?.can_start) {
        created = await startWorkflowJob(job.job_id, request.signal)
      } else {
        created = await submitBatchWorkflow(batchId, request.signal)
      }
      if (!request.signal.aborted) {
        latestJob.current = created.job_id
        setJob(created)
      }
    } catch {
      if (!request.signal.aborted) setError('submit')
    } finally {
      submittingRef.current = false
      if (!request.signal.aborted) setSubmitting(false)
    }
  }

  const refreshConfiguration = () => {
    setConfiguration(null)
    setConfigurationRevision((value) => value + 1)
  }
  return {
    configuration,
    job,
    restoring,
    submitting,
    error,
    submit,
    canRetry,
    refreshConfiguration
  }
}
