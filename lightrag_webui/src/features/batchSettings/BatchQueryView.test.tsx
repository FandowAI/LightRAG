import { afterAll, afterEach, beforeAll, beforeEach, expect, mock, test } from 'bun:test'
import { act, cleanup, fireEvent, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { renderWithProviders } from '@/test/render'
import { seedCustomization, resetCustomization } from '@/test/customization'
import { captureProcessState, restoreProcessState, type ProcessStateSnapshot } from '@/test/processState'
import { useSettingsStore } from '@/stores/settings'
import { useQuerySettingsStore } from '@/stores/querySettings'
import { createRetrievalHistoryStore } from '@/stores/retrievalHistory'
import { batchClientFixture } from '@/test/batchControl'
import type { BatchSettings } from '@/api/batchControl'
import type { QueryRequest } from '@/api/lightrag'
import en from '@/locales/en.json'
import type BatchQueryViewType from './BatchQueryView'

let BatchQueryView: typeof BatchQueryViewType
let realApi: Record<string, unknown>
const requests: QueryRequest[] = []
const stores = [useSettingsStore, useQuerySettingsStore]
let snapshot: ProcessStateSnapshot

beforeAll(async () => {
  realApi = { ...await import('@/api/lightrag') }
  mock.module('@/api/lightrag', () => ({ ...realApi, queryText: mock(async (value: QueryRequest) => { requests.push(value); return { response: 'batch response' } }), queryTextStream: mock(async (value: QueryRequest) => { requests.push(value) }) }))
  BatchQueryView = (await import('./BatchQueryView')).default
})
beforeEach(() => { snapshot = captureProcessState(stores); requests.length = 0; seedCustomization() })
afterEach(() => { cleanup(); resetCustomization(); restoreProcessState(stores, snapshot) })
afterAll(() => { mock.module('@/api/lightrag', () => realApi) })

test('batch query submission uses its active server defaults and keeps browser overrides within that defaults profile', async () => {
  const { client, state } = batchClientFixture()
  state['batch-a'].active_configuration.retrieval.ordinary = { top_k: 73, mode: 'naive' }
  const history = createRetrievalHistoryStore('test::batch-query-view::a')
  const view = renderWithProviders(<BatchQueryView client={client} batch="batch-a" identity="alice" historyStore={history} />)
  const field = await screen.findByRole('spinbutton', { name: en.retrievePanel.querySettings.topK })
  expect((field as HTMLInputElement).value).toBe('73')
  const user = userEvent.setup()
  await user.type(within(screen.getByRole('search')).getByRole('textbox'), 'Explain this evidence')
  await user.keyboard('{Enter}')
  await waitFor(() => expect(requests).toHaveLength(1))
  expect(requests[0].top_k).toBe(73)
  expect(requests[0].mode).toBe('naive')
  fireEvent.change(field, { target: { value: '88' } })
  view.unmount()
  renderWithProviders(<BatchQueryView client={client} batch="batch-a" identity="alice" historyStore={history} />)
  const restored = await screen.findByRole('spinbutton', { name: en.retrievePanel.querySettings.topK })
  expect((restored as HTMLInputElement).value).toBe('88')
})

test('a late settings response cannot replace another batch query defaults', async () => {
  const { client, state } = batchClientFixture()
  let resolve!: (value: BatchSettings) => void
  client.settings.mockImplementationOnce(() => new Promise((done) => { resolve = done }))
  state['batch-b'].active_configuration.retrieval.ordinary = { top_k: 23 }
  const history = createRetrievalHistoryStore('test::batch-query-view::b')
  const view = renderWithProviders(<BatchQueryView client={client} batch="batch-a" identity="alice" historyStore={history} />)
  view.rerender(<BatchQueryView client={client} batch="batch-b" identity="alice" historyStore={history} />)
  const field = await screen.findByRole('spinbutton', { name: en.retrievePanel.querySettings.topK })
  expect((field as HTMLInputElement).value).toBe('23')
  await act(async () => { resolve(state['batch-a']) })
  expect((screen.getByRole('spinbutton', { name: en.retrievePanel.querySettings.topK }) as HTMLInputElement).value).toBe('23')
  expect(client.settings.mock.calls[0][1]?.aborted).toBe(true)
})

test('returning from configuration reloads changed active defaults without using an earlier profile override', async () => {
  const { client, state } = batchClientFixture()
  state['batch-a'].active_configuration.retrieval.ordinary = { top_k: 73 }
  const history = createRetrievalHistoryStore('test::batch-query-view::return')
  const view = renderWithProviders(<BatchQueryView client={client} batch="batch-a" identity="alice" historyStore={history} />)
  const field = await screen.findByRole('spinbutton', { name: en.retrievePanel.querySettings.topK })
  fireEvent.change(field, { target: { value: '88' } })
  view.rerender(<BatchQueryView client={client} batch="batch-a" identity="alice" historyStore={history} active={false} />)
  state['batch-a'].active_configuration.retrieval.ordinary = { top_k: 77 }
  view.rerender(<BatchQueryView client={client} batch="batch-a" identity="alice" historyStore={history} />)
  await waitFor(() => expect((screen.getByRole('spinbutton', { name: en.retrievePanel.querySettings.topK }) as HTMLInputElement).value).toBe('77'))
})

test('invalid server query defaults show a recoverable error before rendering the send form', async () => {
  const { client, state } = batchClientFixture()
  state['batch-a'].active_configuration.retrieval.ordinary = { top_k: -5 }
  const history = createRetrievalHistoryStore('test::batch-query-view::invalid')
  renderWithProviders(<BatchQueryView client={client} batch="batch-a" identity="alice" historyStore={history} />)
  await screen.findByRole('alert')
  expect(screen.queryAllByRole('search')).toHaveLength(0)
  state['batch-a'].active_configuration.retrieval.ordinary = { top_k: 31 }
  fireEvent.click(screen.getByRole('button', { name: 'Reload query settings' }))
  const field = await screen.findByRole('spinbutton', { name: en.retrievePanel.querySettings.topK })
  expect((field as HTMLInputElement).value).toBe('31')
})
