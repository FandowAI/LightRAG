import { expect, test } from 'bun:test'
import { fireEvent, screen } from '@testing-library/react'
import { renderWithProviders } from '@/test/render'
import { batchClientFixture } from '@/test/batchControl'
import type { Application } from '@/api/batchControl'
import ApplicationProgress from './ApplicationProgress'

const initial = (state: Application['state'], terminal: boolean): Application => ({
  operation_id: 'op-a', data_batch_id: 'batch-a', config_revision_id: 'rev-new', state, terminal
})

test('confirmed rollback shows restoration and does not offer to apply a retired operation', () => {
  const { client } = batchClientFixture()
  renderWithProviders(<ApplicationProgress client={client} batch="batch-a" initial={{ ...initial('rolled_back', true), error: 'New runtime failed' }} canApply onFinished={() => {}} />)
  expect(screen.getByRole('status').textContent).toBe('Application failed; the previous configuration is running')
  expect(screen.getAllByText('New runtime failed')).toHaveLength(1)
  expect(screen.queryAllByRole('button', { name: 'Retry application' })).toHaveLength(0)
  expect(screen.queryAllByRole('button', { name: 'Discard pending configuration' })).toHaveLength(0)
})

test('failed rollback displays both errors and permits an explicit retry', async () => {
  const { client } = batchClientFixture()
  client.apply.mockResolvedValueOnce(initial('queued', false))
  renderWithProviders(<ApplicationProgress client={client} batch="batch-a" initial={{ ...initial('rollback_failed', true), error: 'New runtime failed', rollback_error: 'Previous runtime could not start' }} canApply onFinished={() => {}} />)
  expect(screen.getByRole('status').textContent).toBe('Recovery failed; this batch remains closed')
  expect(screen.getAllByText('Previous runtime could not start')).toHaveLength(1)
  fireEvent.click(screen.getByRole('button', { name: 'Retry application' }))
  await screen.findByText('Application queued')
  expect(client.apply.mock.calls[0][0]).toBe('batch-a')
  expect(client.apply.mock.calls[0][1]).toBe('op-a')
})

test('a confirmed failure gives the next explicit retry a new request identity', async () => {
  const { client } = batchClientFixture()
  client.apply.mockResolvedValueOnce(initial('queued', false))
  client.application.mockResolvedValueOnce(initial('rollback_failed', true))
  renderWithProviders(<ApplicationProgress client={client} batch="batch-a" initial={initial('awaiting_action', false)} canApply onFinished={() => {}} />)
  fireEvent.click(screen.getByRole('button', { name: 'Apply and restart this batch' }))
  await screen.findByText('Recovery failed; this batch remains closed')
  fireEvent.click(screen.getByRole('button', { name: 'Retry application' }))
  await screen.findByText('Application queued')
  expect(client.apply.mock.calls).toHaveLength(2)
  expect(client.apply.mock.calls[0][3] === client.apply.mock.calls[1][3]).toBe(false)
})
