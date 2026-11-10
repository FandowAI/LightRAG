import { afterEach, expect, test } from 'bun:test'
import { cleanup, fireEvent, screen, waitFor } from '@testing-library/react'
import { renderWithProviders } from '@/test/render'
import { batchClientFixture } from '@/test/batchControl'
import { useBatchContext } from '@/stores/batchContext'
import BatchChunkingSettings from './BatchChunkingSettings'

afterEach(() => { cleanup(); useBatchContext.getState().select(null) })

test('document settings expose paragraph chunking and save only the selected batch without changing embedding', async () => {
  const { client, state } = batchClientFixture()
  state['batch-a'].configuration.rag.chunking_strategy = 'paragraph_semantic'
  useBatchContext.getState().select(state['batch-a'].batch)
  renderWithProviders(<BatchChunkingSettings client={client} />)
  await screen.findByDisplayValue('Paragraph semantic (default)')
  await waitFor(() => expect((screen.getByLabelText('Chunk size (tokens)') as HTMLInputElement).disabled).toBe(false))
  fireEvent.change(screen.getByLabelText('Chunk size (tokens)'), { target: { value: '2200' } })
  fireEvent.click(screen.getByRole('button', { name: 'Save chunking settings' }))
  await waitFor(() => expect(client.apply.mock.calls.length).toBe(1))
  expect(client.save.mock.calls[0][0]).toBe('batch-a')
  expect(client.save.mock.calls[0][1].configuration.rag.chunk_size).toBe(2200)
  expect(client.save.mock.calls[0][1].configuration.rag.embedding_profile).toBe('embed-v1')
  expect(client.apply.mock.calls[0][2]).toBe('rev-new')
  expect(client.startIndexRebuild.mock.calls.length).toBe(0)
})

test('closing the document settings discards a late publication instead of applying it after close', async () => {
  const { client, state } = batchClientFixture()
  useBatchContext.getState().select(state['batch-a'].batch)
  let finish!: () => void
  client.save.mockImplementationOnce(() => new Promise((resolve) => { finish = () => resolve({ batch: 'batch-a', revision: 'rev-new', operation: 'op-new', status: 'pending_restart', requires_restart: true }) }))
  const view = renderWithProviders(<BatchChunkingSettings client={client} />)
  await waitFor(() => expect((screen.getByLabelText('Chunk size (tokens)') as HTMLInputElement).disabled).toBe(false))
  fireEvent.change(screen.getByLabelText('Chunk size (tokens)'), { target: { value: '2200' } })
  fireEvent.click(screen.getByRole('button', { name: 'Save chunking settings' }))
  await waitFor(() => expect(client.save.mock.calls.length).toBe(1))
  view.unmount(); finish()
  await new Promise((resolve) => setTimeout(resolve, 0))
  expect(client.apply.mock.calls.length).toBe(0)
})
