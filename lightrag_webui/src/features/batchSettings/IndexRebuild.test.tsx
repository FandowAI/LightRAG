import { expect, mock, test } from 'bun:test'
import { StrictMode } from 'react'
import { act, fireEvent, screen, waitFor } from '@testing-library/react'
import { renderWithProviders } from '@/test/render'
import { batchClientFixture, indexRebuildFixture } from '@/test/batchControl'
import type { BatchHttpError, IndexRebuild } from '@/api/batchControl'
import IndexRebuildAction from './IndexRebuildAction'
import BatchIndexProgress from './BatchIndexProgress'
import BatchSettingsView from './BatchSettingsView'

const profiles = [{ id: 'embed-v1', model_name: 'Test embed', dimensions: 8 }, { id: 'embed-v2', model_name: 'Next embed', dimensions: 16 }]

test('settings starts the whole rebuild and mounts its independent progress instead of ordinary apply', async () => {
  const { client, state } = batchClientFixture()
  state['batch-a'].permissions.rebuild_index = true
  renderWithProviders(<BatchSettingsView client={client} />)
  fireEvent.click(await screen.findByRole('button', { name: 'Start index rebuild' }))
  await screen.findByRole('region', { name: 'Index rebuild progress' })
  expect(client.startIndexRebuild.mock.calls[0].slice(0, 3)).toEqual(['batch-a', 'rev-a', 'embed-v1'])
  expect(client.apply.mock.calls).toHaveLength(0)
  expect((screen.getByRole('button', { name: 'Start index rebuild' }) as HTMLButtonElement).disabled).toBe(true)
})

test('model choice is pinned and an uncertain submission retries the same request identity', async () => {
  const { client } = batchClientFixture()
  const started = mock(() => {})
  client.startIndexRebuild.mockRejectedValueOnce(new Error('outcome unknown'))
  renderWithProviders(<IndexRebuildAction client={client} batch="batch-a" revision="rev-a" activeProfile="embed-v1" profiles={profiles} allowed disabled={false} onStarted={started} />)
  fireEvent.change(screen.getByRole('combobox', { name: 'Target embedding profile' }), { target: { value: 'embed-v2' } })
  fireEvent.click(screen.getByRole('button', { name: 'Start index rebuild' }))
  await screen.findByRole('alert')
  expect((screen.getByRole('combobox', { name: 'Target embedding profile' }) as HTMLSelectElement).disabled).toBe(true)
  fireEvent.click(screen.getByRole('button', { name: 'Start index rebuild' }))
  await waitFor(() => expect(started.mock.calls).toHaveLength(1))
  expect(client.startIndexRebuild.mock.calls[1].slice(0, 3)).toEqual(['batch-a', 'rev-a', 'embed-v2'])
  expect(client.startIndexRebuild.mock.calls[1][3]).toBe(client.startIndexRebuild.mock.calls[0][3])
})

test('the action respects server permissions and pending or dirty settings', () => {
  const { client } = batchClientFixture()
  const view = renderWithProviders(<IndexRebuildAction client={client} batch="batch-a" revision="rev-a" activeProfile="embed-v1" profiles={profiles} allowed={false} disabled={false} onStarted={() => {}} />)
  expect(screen.queryAllByRole('button', { name: 'Start index rebuild' })).toHaveLength(0)
  view.rerender(<IndexRebuildAction client={client} batch="batch-a" revision="rev-a" activeProfile="embed-v1" profiles={profiles} allowed disabled onStarted={() => {}} />)
  expect((screen.getByRole('button', { name: 'Start index rebuild' }) as HTMLButtonElement).disabled).toBe(true)
  expect(client.startIndexRebuild.mock.calls).toHaveLength(0)
})

test.each([409, 503])('HTTP %s can follow an accepted request and must retain its retry identity', async (status) => {
  const { client } = batchClientFixture()
  const failure: BatchHttpError = new Error('request may already be accepted')
  failure.status = status
  client.startIndexRebuild.mockRejectedValueOnce(failure)
  const started = mock(() => {})
  renderWithProviders(<IndexRebuildAction client={client} batch="batch-a" revision="rev-a" activeProfile="embed-v1" profiles={profiles} allowed disabled={false} onStarted={started} />)
  fireEvent.click(screen.getByRole('button', { name: 'Start index rebuild' }))
  await screen.findByRole('alert')
  expect((screen.getByRole('combobox', { name: 'Target embedding profile' }) as HTMLSelectElement).disabled).toBe(true)
  fireEvent.click(screen.getByRole('button', { name: 'Start index rebuild' }))
  await waitFor(() => expect(started.mock.calls).toHaveLength(1))
  expect(client.startIndexRebuild.mock.calls[1][3]).toBe(client.startIndexRebuild.mock.calls[0][3])
})

test('a late submission is aborted on a batch switch and cannot update the new batch', async () => {
  const { client } = batchClientFixture()
  const started = mock(() => {})
  let resolve!: (record: IndexRebuild) => void
  client.startIndexRebuild.mockImplementationOnce(() => new Promise((done) => { resolve = done }))
  const view = renderWithProviders(<StrictMode><IndexRebuildAction client={client} batch="batch-a" revision="rev-a" activeProfile="embed-v1" profiles={profiles} allowed disabled={false} onStarted={started} /></StrictMode>)
  fireEvent.click(screen.getByRole('button', { name: 'Start index rebuild' }))
  view.rerender(<StrictMode><IndexRebuildAction client={client} batch="batch-b" revision="rev-b" activeProfile="embed-v1" profiles={profiles} allowed disabled={false} onStarted={started} /></StrictMode>)
  expect(client.startIndexRebuild.mock.calls[0][4]?.aborted).toBe(true)
  await act(async () => { resolve(indexRebuildFixture()) })
  expect(started.mock.calls).toHaveLength(0)
  expect((screen.getByRole('button', { name: 'Start index rebuild' }) as HTMLButtonElement).disabled).toBe(false)
  fireEvent.click(screen.getByRole('button', { name: 'Start index rebuild' }))
  await waitFor(() => expect(started.mock.calls).toHaveLength(1))
  expect(client.startIndexRebuild.mock.calls[1][0]).toBe('batch-b')
})

test('independent progress shows verified counts, provider waiting and requested cancellation', async () => {
  const { client } = batchClientFixture()
  const item = indexRebuildFixture()
  item.provider_admission.items = [{ queued: 2, active: 1 }]
  client.indexRebuilds.mockResolvedValue({ items: [item] })
  renderWithProviders(<BatchIndexProgress client={client} batch="batch-a" initial={item} />)
  expect(screen.getByRole('progressbar', { name: 'Verified vectors' }).getAttribute('value')).toBe('1')
  expect(screen.getByText('Written 2 / 4 · verified 1 / 4 vectors').textContent).toContain('1 / 4')
  expect((await screen.findByLabelText('Model requests')).textContent).toContain('2')
  fireEvent.click(screen.getByRole('button', { name: 'Cancel index rebuild' }))
  await screen.findByText('Cancellation requested. Waiting for safe shutdown and restoration.')
  expect(client.cancelIndexRebuild.mock.calls[0].slice(0, 2)).toEqual(['batch-a', item.operation_id])
  expect(screen.queryAllByText('Previous index restored')).toHaveLength(0)
  expect(client.startIndexRebuild.mock.calls).toHaveLength(0)
})

test('stale progress lists are discarded when batch identity changes', async () => {
  const { client } = batchClientFixture()
  let resolve!: (record: { items: IndexRebuild[] }) => void
  client.indexRebuilds.mockImplementationOnce(() => new Promise((done) => { resolve = done }))
  const first = indexRebuildFixture()
  first.target.embedding_model = 'Old batch model'
  const next = indexRebuildFixture('batch-b', 'applied')
  next.target.embedding_model = 'Current batch model'
  client.indexRebuilds.mockResolvedValueOnce({ items: [next] })
  const view = renderWithProviders(<BatchIndexProgress client={client} batch="batch-a" initial={first} />)
  view.rerender(<BatchIndexProgress client={client} batch="batch-b" initial={next} />)
  expect(client.indexRebuilds.mock.calls[0][1]?.aborted).toBe(true)
  await act(async () => { resolve({ items: [first] }) })
  expect(screen.queryAllByText(/Old batch model/)).toHaveLength(0)
  expect(screen.getByText(/Current batch model/).textContent).toContain('8')
})

test('restoration recovery pins the observed phase version and never starts another paid rebuild', async () => {
  const { client } = batchClientFixture()
  const item = indexRebuildFixture('batch-a', 'rollback_failed')
  item.version = 7
  client.indexRebuilds.mockResolvedValueOnce({ items: [item] })
  renderWithProviders(<BatchIndexProgress client={client} batch="batch-a" initial={item} />)
  fireEvent.click(screen.getByRole('button', { name: 'Retry previous-index restoration' }))
  await screen.findByText('Stopping before restoration')
  expect(client.recoverIndexRebuild.mock.calls[0].slice(0, 3)).toEqual(['batch-a', item.operation_id, 7])
  expect(client.startIndexRebuild.mock.calls).toHaveLength(0)
})

test('a late cancellation response cannot replace another batch progress', async () => {
  const { client } = batchClientFixture()
  const initial = indexRebuildFixture()
  const next = indexRebuildFixture('batch-b', 'applied')
  client.indexRebuilds.mockResolvedValueOnce({ items: [initial] }).mockResolvedValueOnce({ items: [next] })
  let resolve!: (record: IndexRebuild) => void
  client.cancelIndexRebuild.mockImplementationOnce(() => new Promise((done) => { resolve = done }))
  const view = renderWithProviders(<BatchIndexProgress client={client} batch="batch-a" initial={initial} />)
  fireEvent.click(screen.getByRole('button', { name: 'Cancel index rebuild' }))
  view.rerender(<BatchIndexProgress client={client} batch="batch-b" initial={next} />)
  expect(client.cancelIndexRebuild.mock.calls[0][2]?.aborted).toBe(true)
  await act(async () => { resolve({ ...initial, version: 2, cancel_requested: true }) })
  expect((screen.getByRole('combobox', { name: 'Recent rebuilds' }) as HTMLSelectElement).value).toBe(next.operation_id)
  expect(screen.queryAllByText('Cancellation requested. Waiting for safe shutdown and restoration.')).toHaveLength(0)
})

test('read-only progress hides cancellation and recovery controls', async () => {
  const { client } = batchClientFixture()
  const item = indexRebuildFixture('batch-a', 'rollback_failed')
  item.can_cancel = false
  item.can_recover = false
  client.indexRebuilds.mockResolvedValue({ items: [item] })
  renderWithProviders(<BatchIndexProgress client={client} batch="batch-a" initial={item} />)
  await waitFor(() => expect(client.indexRebuilds.mock.calls).toHaveLength(1))
  expect(screen.queryAllByRole('button', { name: 'Cancel index rebuild' })).toHaveLength(0)
  expect(screen.queryAllByRole('button', { name: 'Retry previous-index restoration' })).toHaveLength(0)
})
