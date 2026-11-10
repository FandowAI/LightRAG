import { describe, expect, test } from 'bun:test'
import { fireEvent, screen, waitFor } from '@testing-library/react'
import { renderWithProviders } from '@/test/render'
import { batchClientFixture } from '@/test/batchControl'
import type { BatchHttpError } from '@/api/batchControl'
import BatchSettingsView from './BatchSettingsView'
import ApplicationProgress from './ApplicationProgress'

async function mount() {
  const fixture = batchClientFixture()
  renderWithProviders(<BatchSettingsView client={fixture.client} />)
  await screen.findByLabelText('Parallel documents')
  return fixture
}

describe('batch configuration settings', () => {
  test('requires a reviewed preview before save and retains active revision for pending RAG changes', async () => {
    const { client } = await mount()
    expect(screen.queryAllByRole('button', { name: 'Save configuration' })).toHaveLength(0)
    fireEvent.change(screen.getByLabelText('Parallel documents'), { target: { value: '7' } })
    const selector = screen.getByLabelText('Data batch') as HTMLSelectElement
    expect(selector.disabled).toBe(true)
    fireEvent.change(selector, { target: { value: 'batch-b' } })
    expect(client.settings.mock.calls.map(call => call[0])).toEqual(['batch-a'])
    fireEvent.click(screen.getByRole('button', { name: 'Validate and preview changes' }))
    await screen.findByRole('button', { name: 'Save configuration' })
    expect(client.save.mock.calls.length).toBe(0)
    fireEvent.click(screen.getByRole('button', { name: 'Save configuration' }))
    await screen.findByText('Saved; awaiting application')
    expect(client.save.mock.calls[0][1].expected_revision).toBe('rev-a')
    expect(client.save.mock.calls[0][1].configuration.rag.max_parallel_insert).toBe(7)
    expect(screen.getByText('rev-a').textContent).toBe('rev-a')
    expect(client.apply.mock.calls.length).toBe(0)
  })

  test('discards only the local draft and invalidates a preview after another edit', async () => {
    const { client } = await mount()
    fireEvent.change(screen.getByLabelText('Parallel documents'), { target: { value: '7' } })
    fireEvent.click(screen.getByRole('button', { name: 'Validate and preview changes' }))
    await screen.findByRole('button', { name: 'Save configuration' })
    fireEvent.change(screen.getByLabelText('Parallel documents'), { target: { value: '8' } })
    expect(screen.queryAllByRole('button', { name: 'Save configuration' })).toHaveLength(0)
    fireEvent.click(screen.getByRole('button', { name: 'Discard draft and reload' }))
    await waitFor(() => expect((screen.getByLabelText('Parallel documents') as HTMLInputElement).value).toBe('3'))
    expect(client.save.mock.calls.length).toBe(0)
  })

  test('keeps the same request identity after a transport failure and shows conflicts without overwriting a draft', async () => {
    const { client } = await mount()
    client.save.mockRejectedValueOnce(new Error('connection lost'))
    fireEvent.change(screen.getByLabelText('Parallel documents'), { target: { value: '7' } })
    fireEvent.click(screen.getByRole('button', { name: 'Validate and preview changes' }))
    await screen.findByRole('button', { name: 'Save configuration' })
    fireEvent.click(screen.getByRole('button', { name: 'Save configuration' }))
    await screen.findByText('Error: connection lost')
    const first = client.save.mock.calls[0][1].request_id
    client.save.mockRejectedValueOnce(Object.assign(new Error('stale revision'), { status: 409 }) as BatchHttpError)
    fireEvent.click(screen.getByRole('button', { name: 'Save configuration' }))
    await screen.findByText('The server configuration changed. Keep your draft for reference and reload before saving again.')
    expect(client.save.mock.calls[1][1].request_id).toBe(first)
    expect((screen.getByLabelText('Parallel documents') as HTMLInputElement).value).toBe('7')
    expect(screen.queryAllByRole('button', { name: 'Save configuration' })).toHaveLength(0)
  })

  test('copies only selected reusable groups and keeps target retrieval settings', async () => {
    const { client } = await mount()
    fireEvent.click(screen.getByText('Reuse configuration groups'))
    fireEvent.change(screen.getByLabelText('Copy source batch'), { target: { value: 'batch-b' } })
    fireEvent.click(screen.getByRole('checkbox', { name: 'RAG' }))
    expect(screen.queryAllByRole('checkbox', { name: 'Retrieval' })).toHaveLength(0)
    fireEvent.click(screen.getByRole('button', { name: 'Copy into draft' }))
    await waitFor(() => expect((screen.getByLabelText('Parallel documents') as HTMLInputElement).value).toBe('11'))
    expect(client.copy.mock.calls[0][2]).toEqual({ rag: { batch: 'batch-b', revision: 'rev-b' } })
    fireEvent.click(screen.getByRole('tab', { name: 'Retrieval' }))
    expect((screen.getByLabelText('Vector candidates') as HTMLInputElement).value).toBe('10')
  })

  test('edits Prompt content inline without creating a legacy settings write', async () => {
    const { client } = await mount()
    fireEvent.click(screen.getByRole('tab', { name: 'Prompts' }))
    fireEvent.change(screen.getByLabelText('Prompt content'), { target: { value: 'new scenario: $payload' } })
    fireEvent.click(screen.getByRole('button', { name: 'Validate and preview changes' }))
    await screen.findByRole('button', { name: 'Save configuration' })
    expect(client.preview.mock.calls[0][1].prompt_contents).toEqual({ scenario: 'new scenario: $payload' })
  })

  test('shows permission-bound read-only settings', async () => {
    const { client, state } = batchClientFixture()
    state['batch-a'].permissions.edit = false
    renderWithProviders(<BatchSettingsView client={client} />)
    await screen.findByLabelText('Parallel documents')
    expect(screen.getByLabelText('Parallel documents').closest('fieldset')?.hasAttribute('disabled')).toBe(true)
    expect((screen.getByRole('button', { name: 'Validate and preview changes' }) as HTMLButtonElement).disabled).toBe(true)
  })

  test('retains invalid path drafts across tabs and blocks preview and batch switching', async () => {
    await mount()
    fireEvent.click(screen.getByRole('tab', { name: 'Input and execution' }))
    fireEvent.change(screen.getByLabelText('Record path (JSON array)'), { target: { value: '[broken' } })
    fireEvent.click(screen.getByRole('tab', { name: 'RAG' }))
    expect((screen.getByRole('button', { name: 'Validate and preview changes' }) as HTMLButtonElement).disabled).toBe(true)
    expect((screen.getByLabelText('Data batch') as HTMLSelectElement).disabled).toBe(true)
    fireEvent.click(screen.getByRole('tab', { name: 'Input and execution' }))
    expect((screen.getByLabelText('Record path (JSON array)') as HTMLInputElement).value).toBe('[broken')
    fireEvent.change(screen.getByLabelText('Record path (JSON array)'), { target: { value: '["results"]' } })
    expect((screen.getByRole('button', { name: 'Validate and preview changes' }) as HTMLButtonElement).disabled).toBe(false)
  })

  test('renders an unavailable-service state instead of fetching a default workspace', async () => {
    const { client } = batchClientFixture()
    client.list.mockRejectedValueOnce(Object.assign(new Error('not found'), { status: 404 }))
    renderWithProviders(<BatchSettingsView client={client} />)
    await screen.findByText('Batch management is not enabled on this service. Configure the batch manager to use this page.')
    expect(client.settings.mock.calls.length).toBe(0)
  })

  test('polls application separately and never reports applied before a verified terminal response', async () => {
    const { client } = batchClientFixture()
    const complete = { operation_id: 'op-test', data_batch_id: 'batch-a', config_revision_id: 'rev-new', state: 'applied' as const, terminal: true }
    client.application.mockResolvedValueOnce(complete)
    let finished = 0
    renderWithProviders(<ApplicationProgress client={client} batch="batch-a" initial={{ ...complete, state: 'queued', terminal: false }} canApply onFinished={() => { finished++ }} />)
    expect(screen.queryAllByText('Configuration applied')).toHaveLength(0)
    await screen.findByText('Configuration applied')
    expect(finished).toBe(1)
  })
})
