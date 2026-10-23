import { afterEach, expect, mock, spyOn, test } from 'bun:test'
import { lightragAxios } from './lightrag'
import { findBatchWorkflow, getWorkflowLogs, submitBatchWorkflow } from './dataWorkflow'
import { workflowFixture } from '@/test/dataWorkflow'

afterEach(() => mock.restore())

test('the batch button uses the canonical endpoint and sends only server-resolved identity', async () => {
  const post = spyOn(lightragAxios, 'post').mockResolvedValue({ data: workflowFixture('completed') })
  await submitBatchWorkflow('batch-1')
  expect(post.mock.calls[0][0]).toBe('/integrations/data-work-flow/submit')
  expect(post.mock.calls[0][1]).toEqual({ source: { type: 'batch', batch_id: 'batch-1' }, start: true })
})

test('restoration filters by batch identity and returns a task or null', async () => {
  const job = workflowFixture('completed')
  const get = spyOn(lightragAxios, 'get').mockResolvedValueOnce({ data: { items: [job] } }).mockResolvedValueOnce({ data: { items: [] } })
  expect((await findBatchWorkflow('batch-1'))?.job_id).toBe(job.job_id)
  expect(get.mock.calls[0][0]).toBe('/integrations/data-work-flow/jobs')
  expect(get.mock.calls[0][1]?.params).toEqual({ source_batch_id: 'batch-1', limit: 1 })
  expect(await findBatchWorkflow('missing')).toBeNull()
})

test('log reads preserve the backend byte cursor', async () => {
  const get = spyOn(lightragAxios, 'get').mockResolvedValue({ data: { text: '原文', next_offset: 6, has_more: false } })
  await getWorkflowLogs('distill-1', 3)
  expect(get.mock.calls[0][0]).toBe('/integrations/data-work-flow/jobs/distill-1/logs')
  expect(get.mock.calls[0][1]?.params).toEqual({ stream: 'stderr', offset: 3, limit: 8192 })
})
