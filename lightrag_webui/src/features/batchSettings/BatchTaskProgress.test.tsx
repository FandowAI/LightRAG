import { expect, test } from 'bun:test'
import { screen, waitFor } from '@testing-library/react'
import { renderWithProviders } from '@/test/render'
import { batchClientFixture } from '@/test/batchControl'
import type { BatchTask } from '@/api/batchControl'
import BatchTaskProgress from './BatchTaskProgress'

const noop = () => {}
const task = (batch: string, queued: number, active: number): BatchTask => ({
  task_id: `task-${batch}`, data_batch_id: batch, config_revision_id: 'rev-a', kind: 'distillation', state: 'running', phase: 'distillation', terminal: false, cancel_requested: false, progress: null, result: null, error: null, waiting_reason: null, created_at: 0, provider_admission: { queued, active }
})

test('independent progress distinguishes shared-capacity waiting from active model requests', async () => {
  const { client, tasks } = batchClientFixture()
  tasks['batch-a'] = [task('batch-a', 2, 1)]
  const view = renderWithProviders(<BatchTaskProgress client={client} batch="batch-a" refresh={0} onTasks={noop} />)
  await screen.findByRole('status', { name: 'Model requests' })
  expect(screen.getByRole('status', { name: 'Model requests' }).textContent).toContain('Waiting for shared capacity: 2')
  expect(screen.getByRole('status', { name: 'Model requests' }).textContent).toContain('In flight: 1')
  tasks['batch-a'] = [task('batch-a', 0, 0)]
  view.rerender(<BatchTaskProgress client={client} batch="batch-a" refresh={1} onTasks={noop} />)
  await waitFor(() => expect(screen.queryAllByRole('status', { name: 'Model requests' })).toHaveLength(0))
})

test('switching batches hides the previous provider queue before the next batch loads', async () => {
  const { client, tasks } = batchClientFixture()
  tasks['batch-a'] = [task('batch-a', 7, 0)]
  const view = renderWithProviders(<BatchTaskProgress client={client} batch="batch-a" refresh={0} onTasks={noop} />)
  await screen.findByRole('status', { name: 'Model requests' })
  let resolve!: (value: { items: BatchTask[] }) => void
  client.tasks.mockImplementationOnce(() => new Promise((done) => { resolve = done }))
  view.rerender(<BatchTaskProgress client={client} batch="batch-b" refresh={0} onTasks={noop} />)
  expect(screen.queryAllByRole('status', { name: 'Model requests' })).toHaveLength(0)
  resolve({ items: [task('batch-b', 1, 3)] })
  await screen.findByRole('status', { name: 'Model requests' })
  expect(screen.getByRole('status', { name: 'Model requests' }).textContent).toContain('Waiting for shared capacity: 1')
  expect(screen.getByRole('status', { name: 'Model requests' }).textContent).toContain('In flight: 3')
})
