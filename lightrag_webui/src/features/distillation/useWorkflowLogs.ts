import { useEffect, useMemo, useState } from 'react'
import { getWorkflowLogs, type WorkflowJob } from '@/api/dataWorkflow'

const MAX_VISIBLE_CHARACTERS = 65536

export default function useWorkflowLogs(job: WorkflowJob) {
  const cursor = useMemo(() => ({ id: job.job_id, offset: 0 }), [job.job_id])
  const [state, setState] = useState({ id: job.job_id, text: '', version: 0, trimmed: false, reconnecting: false, updated: 0 })
  useEffect(() => {
    const controller = new AbortController()
    let timer: ReturnType<typeof setTimeout> | undefined
    const read = async () => {
      try {
        const requested = cursor.offset
        const value = await getWorkflowLogs(job.job_id, requested, controller.signal)
        if (controller.signal.aborted) return
        if (value.next_offset < value.offset || value.next_offset < 0) throw new Error('Invalid log cursor')
        const reset = value.offset !== requested
        cursor.offset = value.next_offset
        setState((previous) => {
          const old = previous.id === job.job_id && !reset ? previous : { text: '', version: 0, trimmed: false }
          const text = old.text + value.text
          return { id: job.job_id, text: text.slice(-MAX_VISIBLE_CHARACTERS), version: old.version + (value.text ? 1 : 0), trimmed: old.trimmed || text.length > MAX_VISIBLE_CHARACTERS, reconnecting: false, updated: Date.now() }
        })
        const draining = value.has_more && value.next_offset > requested
        if (!job.terminal || value.has_more) timer = setTimeout(() => void read(), draining ? 0 : 1500)
      } catch {
        if (controller.signal.aborted) return
        setState((previous) => ({ ...previous, reconnecting: true }))
        timer = setTimeout(() => void read(), 3000)
      }
    }
    void read()
    return () => { controller.abort(); if (timer) clearTimeout(timer) }
  }, [job.job_id, job.terminal, cursor])
  return state.id === job.job_id ? state : { id: job.job_id, text: '', version: 0, trimmed: false, reconnecting: false, updated: 0 }
}
