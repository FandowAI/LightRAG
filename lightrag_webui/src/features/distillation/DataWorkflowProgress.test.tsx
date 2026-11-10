import { afterEach, beforeEach, expect, mock, spyOn, test } from 'bun:test'
import { cleanup, fireEvent, waitFor } from '@testing-library/react'
import * as api from '@/api/dataWorkflow'
import { renderWithProviders } from '@/test/render'
import { workflowFixture } from '@/test/dataWorkflow'
import DataWorkflowProgress from './DataWorkflowProgress'
beforeEach(() => { spyOn(api, 'getWorkflowLogs').mockResolvedValue({ text: '', offset: 0, next_offset: 0, has_more: false }) })
afterEach(() => { cleanup(); mock.restore() })
test('drains terminal UTF-8 logs automatically using byte cursors, then stops', async () => {
  const logs = spyOn(api, 'getWorkflowLogs').mockResolvedValueOnce({ text: '原文', offset: 0, next_offset: 6, has_more: true }).mockResolvedValueOnce({ text: ' done', offset: 6, next_offset: 11, has_more: false })
  const view = renderWithProviders(<DataWorkflowProgress job={workflowFixture('completed')} />)
  await waitFor(() => expect(view.getByLabelText('Distillation logs').textContent).toBe('原文 done'))
  expect(logs.mock.calls[1][1]).toBe(6)
  await new Promise(resolve => setTimeout(resolve, 50))
  expect(logs.mock.calls.length).toBe(2)
  expect(view.queryAllByRole('button', { name: 'Load more logs' }).length).toBe(0)
})
test('terminal transition fetches the final tail and preserves paused following', async () => {
  const logs = spyOn(api, 'getWorkflowLogs').mockResolvedValueOnce({ text: 'start', offset: 0, next_offset: 5, has_more: false }).mockResolvedValueOnce({ text: ' end', offset: 5, next_offset: 9, has_more: false })
  const view = renderWithProviders(<DataWorkflowProgress job={workflowFixture()} />)
  await waitFor(() => expect(view.getByLabelText('Distillation logs').textContent).toBe('start'))
  fireEvent.click(view.getByRole('button', { name: 'Following latest · click to pause' }))
  view.rerender(<DataWorkflowProgress job={workflowFixture('completed')} />)
  await waitFor(() => expect(view.getByLabelText('Distillation logs').textContent).toBe('start end'))
  expect(logs.mock.calls[1][1]).toBe(5)
  fireEvent.click(view.getByRole('button', { name: 'New logs · follow latest' }))
  expect(view.getByRole('button', { name: 'Following latest · click to pause' }).getAttribute('aria-pressed')).toBe('true')
})
test('connection loss reconnects automatically without replaying the task', async () => {
  const logs = spyOn(api, 'getWorkflowLogs').mockRejectedValueOnce(new Error('disconnected')).mockResolvedValueOnce({ text: 'restored', offset: 0, next_offset: 8, has_more: false })
  const start = spyOn(api, 'startWorkflowJob')
  const view = renderWithProviders(<DataWorkflowProgress job={workflowFixture('completed')} />)
  await waitFor(() => expect(view.getByText(/Connection interrupted/)).toBeTruthy())
  await waitFor(() => expect(view.getByLabelText('Distillation logs').textContent).toBe('restored'), { timeout: 4500 })
  expect(logs.mock.calls.length).toBe(2); expect(start).not.toHaveBeenCalled()
})
test('scope changes abort old requests and discard stale log content', async () => {
  let resolve!: (value: api.WorkflowLogs) => void
  const logs = spyOn(api, 'getWorkflowLogs').mockImplementationOnce(() => new Promise(done => { resolve = done })).mockResolvedValue({ text: 'new batch', offset: 0, next_offset: 9, has_more: false })
  const view = renderWithProviders(<DataWorkflowProgress job={workflowFixture('completed')} />)
  await waitFor(() => expect(logs.mock.calls.length).toBe(1))
  const signal = logs.mock.calls[0][2]
  view.rerender(<DataWorkflowProgress job={{ ...workflowFixture('completed'), job_id: 'another-batch' }} />)
  resolve({ text: 'private old batch', offset: 0, next_offset: 17, has_more: false })
  await waitFor(() => expect(view.getByLabelText('Distillation logs').textContent).toBe('new batch'))
  expect(signal?.aborted).toBe(true)
})
test('completed QA preview and download use authenticated job-scoped results', async () => {
  const job = workflowFixture('completed'); job.result = { run_id: 'run-test', directory: '/owned/run', status: 'completed', summary: {} }
  const results = spyOn(api, 'getWorkflowResults').mockResolvedValue({ items: [{ question: 'Final question', answer: 'Final answer', source_id: 's', work_item_id: 'w', version_id: 'v' }], offset: 0, next_offset: 1, has_more: false })
  const download = spyOn(api, 'downloadWorkflowArtifact').mockResolvedValue(new Blob(['full official record']))
  spyOn(URL, 'createObjectURL').mockReturnValue('blob:test'); spyOn(URL, 'revokeObjectURL').mockImplementation(() => {})
  const click = spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(() => {})
  const view = renderWithProviders(<DataWorkflowProgress job={job} />)
  fireEvent.click(view.getByRole('button', { name: 'Preview QA' }))
  await waitFor(() => expect(view.getByText('Final answer')).toBeTruthy())
  expect(results.mock.calls[0][0]).toBe(job.job_id)
  fireEvent.click(view.getByRole('button', { name: 'Download results' }))
  await waitFor(() => expect(click).toHaveBeenCalledTimes(1))
  expect(download.mock.calls[0].slice(0, 2)).toEqual([job.job_id, 'review'])
})
