import { expect, mock, test } from 'bun:test'
import { StrictMode } from 'react'
import { act, fireEvent, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { renderWithProviders } from '@/test/render'
import { batchClientFixture } from '@/test/batchControl'
import type { BatchTask } from '@/api/batchControl'
import BatchTaskArtifacts from './BatchTaskArtifacts'
import BatchTaskProgress from './BatchTaskProgress'

const task = (batch: string, id: string): BatchTask => ({
  task_id: id, data_batch_id: batch, config_revision_id: 'rev-a', kind: 'distillation', state: 'completed', terminal: true, phase: 'completed', cancel_requested: false, progress: null, result: null, error: null, waiting_reason: null, created_at: 0
})

test('task progress and result requests remain usable after StrictMode effect replay', async () => {
  const { client, tasks } = batchClientFixture()
  const user = userEvent.setup()
  tasks['batch-a'] = [task('batch-a', 'task-strict')]
  await act(async () => {
    renderWithProviders(<StrictMode><BatchTaskProgress client={client} batch="batch-a" refresh={0} onTasks={() => {}} /></StrictMode>)
  })
  await screen.findByText('task-strict')
  await user.click(screen.getByRole('button', { name: 'View result' }))
  await waitFor(() => expect(screen.getByLabelText('Result or log content').textContent?.includes('task-strict')).toBe(true))
  expect(client.taskResult.mock.calls[0][2]?.aborted).toBe(false)
})

test('result inspection and paginated logs stay on their displayed batch and task', async () => {
  const { client } = batchClientFixture()
  client.taskLogs.mockResolvedValueOnce({ text: 'first part', offset: 0, next_offset: 10, has_more: true })
  client.taskLogs.mockResolvedValueOnce({ text: ' second part', offset: 10, next_offset: 22, has_more: false })
  renderWithProviders(<BatchTaskArtifacts client={client} batch="batch-a" task={task('batch-a', 'task-a')} />)
  fireEvent.click(screen.getByRole('button', { name: 'View result' }))
  await waitFor(() => expect(screen.getByLabelText('Result or log content').textContent?.includes('task-a')).toBe(true))
  expect(client.taskResult.mock.calls[0][0]).toBe('batch-a')
  fireEvent.click(screen.getByRole('button', { name: 'Distillation error log' }))
  await screen.findByText('first part')
  fireEvent.click(screen.getByRole('button', { name: 'Read more log' }))
  await screen.findByText('first part second part')
  expect(client.taskLogs.mock.calls[1].slice(0, 4)).toEqual(['batch-a', 'task-a', 'stderr', 10])
})

test('a late result from another batch is aborted and never displayed', async () => {
  const { client } = batchClientFixture()
  let resolve!: (value: Record<string, unknown>) => void
  client.taskResult.mockImplementationOnce(() => new Promise((done) => { resolve = done }))
  const view = renderWithProviders(<BatchTaskArtifacts client={client} batch="batch-a" task={task('batch-a', 'task-a')} />)
  fireEvent.click(screen.getByRole('button', { name: 'View result' }))
  view.rerender(<BatchTaskArtifacts client={client} batch="batch-b" task={task('batch-b', 'task-b')} />)
  expect(client.taskResult.mock.calls[0][2]?.aborted).toBe(true)
  await act(async () => { resolve({ text: 'other batch result' }) })
  expect(screen.queryAllByLabelText('Result or log content')).toHaveLength(0)
  expect((screen.getByRole('button', { name: 'View result' }) as HTMLButtonElement).disabled).toBe(false)
})

test('file downloads use the authenticated batch client and a temporary browser blob URL', async () => {
  const { client } = batchClientFixture()
  client.taskArtifacts.mockResolvedValueOnce({ items: [{ artifact: 'workflow-review', name: 'review.jsonl', bytes: 12 }] })
  const originalCreate = URL.createObjectURL
  const originalRevoke = URL.revokeObjectURL
  const originalClick = HTMLAnchorElement.prototype.click
  const downloads: string[] = []
  const revoke = mock(() => {})
  URL.createObjectURL = mock(() => 'blob:batch-output')
  URL.revokeObjectURL = revoke
  HTMLAnchorElement.prototype.click = function () { downloads.push(this.download) }
  try {
    renderWithProviders(<BatchTaskArtifacts client={client} batch="batch-a" task={task('batch-a', 'task-a')} />)
    fireEvent.click(screen.getByRole('button', { name: 'Download files' }))
    const file = await screen.findByRole('button', { name: 'review.jsonl (12 B)' })
    fireEvent.click(file)
    await waitFor(() => expect(downloads).toEqual(['review.jsonl']))
    expect(client.taskDownload.mock.calls[0].slice(0, 3)).toEqual(['batch-a', 'task-a', 'workflow-review'])
    expect(revoke).toHaveBeenCalledWith('blob:batch-output')
  } finally {
    URL.createObjectURL = originalCreate
    URL.revokeObjectURL = originalRevoke
    HTMLAnchorElement.prototype.click = originalClick
  }
})

test('switching batches during cancellation never refreshes or displays the old task list', async () => {
  const { client, tasks } = batchClientFixture()
  const a = { ...task('batch-a', 'task-a'), state: 'running', terminal: false }
  const b = { ...task('batch-b', 'task-b'), state: 'running', terminal: false }
  tasks['batch-a'] = [a]; tasks['batch-b'] = [b]
  let resolve!: (value: BatchTask) => void
  client.cancelTask.mockImplementationOnce(() => new Promise((done) => { resolve = done }))
  const received: string[][] = []
  const onTasks = (items: BatchTask[]) => { received.push(items.map((value) => value.task_id)) }
  const view = renderWithProviders(<BatchTaskProgress client={client} batch="batch-a" refresh={0} onTasks={onTasks} />)
  await screen.findByText('task-a')
  fireEvent.click(screen.getByRole('button', { name: 'Cancel task' }))
  view.rerender(<BatchTaskProgress client={client} batch="batch-b" refresh={0} onTasks={onTasks} />)
  await screen.findByText('task-b')
  await act(async () => { resolve({ ...a, state: 'cancelled', terminal: true }) })
  expect(screen.queryAllByText('task-a')).toHaveLength(0)
  expect(received.at(-1)).toEqual(['task-b'])
  expect(client.tasks.mock.calls.filter(([batch]) => batch === 'batch-a')).toHaveLength(1)
  expect((screen.getByRole('button', { name: 'Cancel task' }) as HTMLButtonElement).disabled).toBe(false)
})
