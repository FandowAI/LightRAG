import { afterEach, expect, test } from 'bun:test'
import { useEffect } from 'react'
import { cleanup, fireEvent, screen, waitFor } from '@testing-library/react'
import { renderWithProviders } from '@/test/render'
import { batchClientFixture } from '@/test/batchControl'
import { useBatchContext } from '@/stores/batchContext'
import BatchWorkbenchSelector from './BatchWorkbenchSelector'
import { findRememberedBatch, lastBatchStorageKey } from './batchSelection'
import BatchSettingsView from './BatchSettingsView'
import BatchSettingsPage from './BatchSettingsPage'
import BatchWorkflows from './BatchWorkflows'
import BatchWorkbenchContent from './BatchWorkbenchContent'

const originalRuntime = window.__LIGHTRAG_CONFIG__
const lastBatchStorageKeyForGuest = lastBatchStorageKey('guest')
afterEach(() => { cleanup(); localStorage.clear(); useBatchContext.getState().select(null); window.__LIGHTRAG_CONFIG__ = originalRuntime })

test('a manager-only deployment asks for a batch and never mounts default-workspace business requests', async () => {
  window.__LIGHTRAG_CONFIG__ = { batchOnly: true }
  const { client, state } = batchClientFixture()
  let mounts = 0
  function Business() {
    useEffect(() => { mounts += 1 }, [])
    return <div>Selected batch documents</div>
  }
  const content = renderWithProviders(<BatchWorkbenchContent batch={null} client={client}><Business /></BatchWorkbenchContent>)
  await screen.findByText('Select or create a batch in the upper left to use the existing pages.')
  expect(mounts).toBe(0)
  expect(screen.queryAllByText('Selected batch documents')).toHaveLength(0)
  content.rerender(<BatchWorkbenchContent batch={state['batch-a'].batch} client={client}><Business /></BatchWorkbenchContent>)
  await screen.findByText('Selected batch documents')
  expect(mounts).toBe(1)
  expect(screen.queryAllByRole('button', { name: 'Enter workbench' })).toHaveLength(0)
})

test('the batch selector names its empty manager state instead of offering a legacy workspace', async () => {
  window.__LIGHTRAG_CONFIG__ = { batchOnly: true }
  const { client } = batchClientFixture()
  renderWithProviders(<BatchWorkbenchSelector client={client} />)
  await screen.findByRole('option', { name: 'Batch A' })
  expect(screen.getAllByRole('option', { name: 'Choose a batch' })).toHaveLength(1)
  expect(screen.queryAllByRole('option', { name: 'Original workspace' })).toHaveLength(0)
})

test('the regular deployment keeps its original workspace entry available', () => {
  window.__LIGHTRAG_CONFIG__ = { batchOnly: false }
  const { client } = batchClientFixture()
  renderWithProviders(<BatchWorkbenchContent batch={null} client={client}><div>Original workspace documents</div></BatchWorkbenchContent>)
  expect(screen.getAllByText('Original workspace documents')).toHaveLength(1)
})

test('a batch enters the workbench only after its own runtime becomes ready', async () => {
  const { client, state } = batchClientFixture()
  let ready!: () => void
  client.workbenchHealth.mockImplementationOnce(() => new Promise((resolve) => { ready = () => resolve({ configuration: { workspace: state['batch-b'].batch.workspace } }) }))
  renderWithProviders(<BatchWorkbenchSelector client={client} />)
  await screen.findByRole('option', { name: 'Batch B' })
  fireEvent.change(screen.getByLabelText('Current data batch'), { target: { value: 'batch-b' } })
  expect(useBatchContext.getState().batch === null).toBe(true)
  expect((screen.getByLabelText('Current data batch') as HTMLSelectElement).disabled).toBe(true)
  ready()
  await waitFor(() => expect(useBatchContext.getState().batch?.id).toBe('batch-b'))
  expect(client.workbenchHealth.mock.calls[0][0]).toBe('batch-b')
  expect((screen.getByLabelText('Current data batch') as HTMLSelectElement).value).toBe('batch-b')
})

test('a failed batch start preserves the prior workspace instead of selecting an unavailable batch', async () => {
  const { client, state } = batchClientFixture()
  useBatchContext.getState().select(state['batch-a'].batch)
  client.workbenchHealth.mockRejectedValueOnce(new Error('Worker unavailable'))
  renderWithProviders(<BatchWorkbenchSelector client={client} />)
  await screen.findByRole('option', { name: 'Batch B' })
  fireEvent.change(screen.getByLabelText('Current data batch'), { target: { value: 'batch-b' } })
  await screen.findByText('Error: Worker unavailable')
  expect(useBatchContext.getState().batch?.id).toBe('batch-a')
})

test('configuration offers entry for the displayed batch and blocks switching while its draft is unsaved', async () => {
  const { client } = batchClientFixture()
  let entered = ''
  renderWithProviders(<BatchSettingsView initialBatch="batch-b" client={client} onEnter={(batch) => { entered = batch.id }} />)
  await screen.findByLabelText('Parallel documents')
  fireEvent.click(screen.getByRole('button', { name: 'Enter workbench' }))
  await waitFor(() => expect(entered).toBe('batch-b'))
  fireEvent.change(screen.getByLabelText('Parallel documents'), { target: { value: '6' } })
  expect(useBatchContext.getState().unsaved).toBe(true)
  expect((screen.getByRole('button', { name: 'Enter workbench' }) as HTMLButtonElement).disabled).toBe(true)
})

test('leaving a settings page prevents its late startup result from changing the active batch', async () => {
  const { client, state } = batchClientFixture()
  let ready!: () => void
  client.workbenchHealth.mockImplementationOnce(() => new Promise((resolve) => { ready = () => resolve({ configuration: { workspace: state['batch-a'].batch.workspace } }) }))
  const rendered = renderWithProviders(<BatchSettingsPage client={client} />)
  await screen.findByLabelText('Parallel documents')
  fireEvent.click(screen.getByRole('button', { name: 'Enter workbench' }))
  rendered.unmount()
  expect(client.workbenchHealth.mock.calls[0][1]?.aborted).toBe(true)
  ready()
  await new Promise((resolve) => setTimeout(resolve, 0))
  expect(useBatchContext.getState().batch === null).toBe(true)
})

test('business controls submit for their displayed batch and progress supports cancel and explicit retry', async () => {
  const { client } = batchClientFixture()
  renderWithProviders(<BatchWorkflows client={client} batch="batch-b" />)
  const start = await screen.findByRole('button', { name: 'Start task' })
  await waitFor(() => expect((start as HTMLButtonElement).disabled).toBe(false))
  fireEvent.click(start)
  await screen.findByText('Queued')
  expect(client.submit.mock.calls[0][0]).toBe('batch-b')
  expect(client.submit.mock.calls[0][1].source).toEqual({ data_batch_id: 'batch-b', type: 'index' })
  expect(client.submit.mock.calls[0][1].expected_revision).toBe('rev-b')
  fireEvent.click(screen.getByRole('button', { name: 'Cancel task' }))
  await screen.findByText('Cancelled')
  client.retryTask.mockRejectedValueOnce(new Error('response lost'))
  fireEvent.click(screen.getByRole('button', { name: 'Retry with current configuration' }))
  await screen.findByText('Error: response lost')
  fireEvent.click(screen.getByRole('button', { name: 'Retry with current configuration' }))
  await screen.findByText('Queued')
  expect(client.retryTask.mock.calls[0][3]).toBe(client.retryTask.mock.calls[1][3])
})


test('the upper-left selector creates and opens a batch without exposing settings or index tools', async () => {
  const { client, state } = batchClientFixture()
  client.workbenchHealth.mockImplementation(async (id: string) => ({ configuration: { workspace: id } }))
  renderWithProviders(<BatchWorkbenchSelector client={client} />)
  await screen.findByRole('option', { name: 'Batch A' })
  fireEvent.click(screen.getByRole('button', { name: 'Create batch' }))
  fireEvent.change(screen.getByLabelText('New batch name'), { target: { value: 'Customer files' } })
  fireEvent.submit(screen.getByLabelText('New batch name').closest('form')!)
  await waitFor(() => expect(useBatchContext.getState().batch?.name).toBe('Customer files'))
  expect(client.create.mock.calls[0][0]).toBe('Customer files')
  expect(client.workbenchHealth.mock.calls[0][0]).toBe('batch-new')
  expect(screen.queryAllByRole('button', { name: 'Batch settings' })).toHaveLength(0)
  expect(screen.queryAllByRole('button', { name: 'Rebuild vector index' })).toHaveLength(0)
  expect(state['batch-a'].batch.active).toBe('rev-a')
})

test('remembered batch lookup is scoped to the available batch list', () => {
  const { state } = batchClientFixture()
  const available = [state['batch-a'].batch, state['batch-b'].batch]

  expect(findRememberedBatch(available, 'batch-b')?.name).toBe('Batch B')
  expect(findRememberedBatch(available, 'batch-missing')).toBeUndefined()
  expect(lastBatchStorageKeyForGuest).toContain(':guest')
})
