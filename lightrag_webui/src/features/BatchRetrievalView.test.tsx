/// <reference types="bun" />
import { afterEach, describe, expect, mock, spyOn, test } from 'bun:test'
import { cleanup, fireEvent, waitFor } from '@testing-library/react'
import { renderWithProviders } from '@/test/render'
import * as api from '@/api/lightrag'
import { backendBaseUrl } from '@/lib/constants'
import { useAuthStore } from '@/stores/state'
import BatchRetrievalView from './BatchRetrievalView'

afterEach(() => { cleanup(); mock.restore(); sessionStorage.clear() })

const job: api.BatchJob = {
  id: 'test-batch', status: 'completed', total: 1, processed: 1, failed: 0, empty: 0,
  package_count: 1, current_center: null, cancel_requested: false,
  settings: { graph_depth: 2, rag_top_k: 30 }, handoff: { status: 'not_submitted' },
  items: [{ center: '产品供应部', status: 'completed', packages: 1 }]
}

function restore() {
  sessionStorage.setItem(`lightrag-batch:${backendBaseUrl}:${useAuthStore.getState().username || 'guest'}`, job.id)
}

describe('minimal batch retrieval', () => {
  test('a missing restored batch clears its saved reference and returns to selection', async () => {
    restore()
    spyOn(api, 'getBatchConfig').mockResolvedValue({ distillation_configured: false })
    spyOn(api, 'getBatchRetrieval').mockRejectedValue(api.toHttpRequestError(404, 'Not Found', { detail: 'Batch not found' }))
    const view = renderWithProviders(<BatchRetrievalView />)
    await waitFor(() => expect(view.getByRole('button', { name: 'Find candidates' })).toBeTruthy())
    expect(sessionStorage.getItem(`lightrag-batch:${backendBaseUrl}:${useAuthStore.getState().username || 'guest'}`)).toBeNull()
  })

  test('a disconnected embedding tunnel is explained without showing batch progress', async () => {
    spyOn(api, 'getBatchConfig').mockResolvedValue({ distillation_configured: false })
    spyOn(api, 'queryEntityCenters').mockResolvedValue({
      policy: { version: 'v1', min_neighbors: 3, min_documents: 2, evidence_scope: 'processed_documents_only' },
      summary: { graph_entities: 1, supported_relations: 4, processed_documents: 2, recommended: 1, review: 0, background: 0, unavailable: 0 },
      candidates: [{ name: '产品供应部', tier: 'recommended', entity_type: '组织与部门', neighbor_count: 4, document_count: 2, chunk_count: 3, in_structural_core: true, reasons: [], cautions: [] }]
    })
    spyOn(api, 'startBatchRetrieval').mockRejectedValue(api.toHttpRequestError(503, 'Service Unavailable', { detail: 'embedding_unavailable' }))
    const view = renderWithProviders(<BatchRetrievalView />)
    fireEvent.submit(view.getByRole('form', { name: 'Select core entities' }))
    await waitFor(() => expect(view.getByLabelText('Select 产品供应部')).toBeTruthy())
    fireEvent.click(view.getByRole('button', { name: 'Set selected centers and enter retrieval' }))
    fireEvent.submit(view.getByRole('form', { name: 'Start batch retrieval' }))
    await waitFor(() => expect(view.getByRole('alert').textContent).toContain('embedding'))
    expect(view.queryAllByRole('progressbar')).toHaveLength(0)
  })

  test('offers only two filter parameters, selects centers, then explicitly starts a batch', async () => {
    spyOn(api, 'getBatchConfig').mockResolvedValue({ distillation_configured: false })
    spyOn(api, 'queryEntityCenters').mockResolvedValue({
      policy: { version: 'v1', min_neighbors: 3, min_documents: 2, evidence_scope: 'processed_documents_only' },
      summary: { graph_entities: 3, supported_relations: 4, processed_documents: 2, recommended: 2, review: 0, background: 1, unavailable: 0 },
      candidates: ['产品供应部', '市场部', '背景'].map((name, i) => ({ name, tier: i === 2 ? 'background' : 'recommended', entity_type: '组织与部门', neighbor_count: 4, document_count: 2, chunk_count: 3, in_structural_core: true, reasons: [], cautions: [] }))
    })
    const start = spyOn(api, 'startBatchRetrieval').mockResolvedValue(job)
    spyOn(api, 'getBatchRetrieval').mockResolvedValue(job)
    const view = renderWithProviders(<BatchRetrievalView />)
    expect(view.getAllByRole('spinbutton').length).toBe(2)
    expect(view.queryAllByRole('combobox')).toHaveLength(0)
    expect(view.queryAllByRole('textbox')).toHaveLength(0)
    fireEvent.submit(view.getByRole('form', { name: 'Select core entities' }))
    await waitFor(() => expect(view.getByLabelText('Select 市场部')).toBeTruthy())
    expect(view.queryAllByText('背景')).toHaveLength(0)
    fireEvent.click(view.getByLabelText('Select 市场部'))
    fireEvent.click(view.getByRole('button', { name: 'Set selected centers and enter retrieval' }))
    expect(start).not.toHaveBeenCalled()
    expect((view.getByLabelText('Graph depth') as HTMLInputElement).value).toBe('2')
    expect((view.getByLabelText('Vector candidate chunks') as HTMLInputElement).value).toBe('30')
    fireEvent.submit(view.getByRole('form', { name: 'Start batch retrieval' }))
    await waitFor(() => expect(start).toHaveBeenCalledTimes(1))
    expect(start.mock.calls[0][0]).toEqual({ centers: ['产品供应部'], graph_depth: 2, rag_top_k: 30 })
    await waitFor(() => expect(view.getByText('1 package ready')).toBeTruthy())
    expect(view.getByRole('progressbar').getAttribute('value')).toBe('1')
    expect(view.getByRole('button', { name: 'Send batch to distillation' }).hasAttribute('disabled')).toBe(true)
    expect(view.getByText('Distillation connection pending')).toBeTruthy()
  })

  test('restores a running job and shows real processed and package counts', async () => {
    restore()
    spyOn(api, 'getBatchConfig').mockResolvedValue({ distillation_configured: false })
    const running = { ...job, status: 'running' as const, total: 3, processed: 1, package_count: 1, current_center: '市场部' }
    spyOn(api, 'getBatchRetrieval').mockResolvedValue(running)
    const stop = spyOn(api, 'stopBatchRetrieval').mockResolvedValue({ ...running, cancel_requested: true })
    const view = renderWithProviders(<BatchRetrievalView />)
    await waitFor(() => expect(view.getByText('Processed 1 / 3 entities')).toBeTruthy())
    expect(view.getByRole('progressbar').getAttribute('max')).toBe('3')
    fireEvent.click(view.getByRole('button', { name: 'Stop' }))
    await waitFor(() => expect(stop).toHaveBeenCalledWith(job.id))
    expect(view.getByText('1 package ready')).toBeTruthy()
    expect(view.queryAllByText('Batch complete')).toHaveLength(0)
  })

  test('reports batch deduplication without presenting fully repeated centers as failures', async () => {
    restore()
    spyOn(api, 'getBatchConfig').mockResolvedValue({ distillation_configured: false })
    spyOn(api, 'getBatchRetrieval').mockResolvedValue({ ...job, total: 3, processed: 3, duplicate_chunks: 17, skipped: 1,
      items: [...job.items, { center: '市场部', status: 'skipped_duplicate', packages: 0 }] })
    const view = renderWithProviders(<BatchRetrievalView />)
    await waitFor(() => expect(view.getByText('17 duplicate passages removed across packages; 1 fully repeated centers skipped')).toBeTruthy())
    expect(view.getByText('Batch complete')).toBeTruthy()
    expect(view.queryAllByText(/retrieval failed/i)).toHaveLength(0)
  })

  test('shows actual selection-stage progress and retained review items', async () => {
    restore()
    spyOn(api, 'getBatchConfig').mockResolvedValue({ distillation_configured: false })
    spyOn(api, 'getBatchRetrieval').mockResolvedValue({ ...job, status: 'running', processed: 0, package_count: 0,
      phase: 'selecting', phase_done: 2, phase_total: 8, review_pending: 1 })
    const view = renderWithProviders(<BatchRetrievalView />)
    await waitFor(() => expect(view.getByText('Selecting relevant topics: 2 / 8 sources')).toBeTruthy())
    expect(view.getByRole('progressbar').getAttribute('value')).toBe('2')
    expect(view.getByRole('progressbar').getAttribute('max')).toBe('8')
    expect(view.getByText('1 sources need review and were excluded from packages.')).toBeTruthy()
  })

  test('shows the server export path after publication', async () => {
    restore()
    spyOn(api, 'getBatchConfig').mockResolvedValue({ distillation_configured: false })
    const path = '/data/fando/lightrag/exports/enterprise/test-batch/packages.jsonl'
    spyOn(api, 'getBatchRetrieval').mockResolvedValue({ ...job, export_path: path })
    const view = renderWithProviders(<BatchRetrievalView />)
    await waitFor(() => expect(view.getByText(path).textContent).toBe(path))
    expect(view.getByText(/Saved to:/).textContent).toContain(path)
  })

  test('only reports distillation acceptance after a successful explicit submission', async () => {
    restore()
    spyOn(api, 'getBatchConfig').mockResolvedValue({ distillation_configured: true })
    spyOn(api, 'getBatchRetrieval').mockResolvedValue(job)
    const send = spyOn(api, 'submitBatchDistillation').mockRejectedValueOnce(new Error('unconfirmed'))
      .mockResolvedValue({ ...job, handoff: { status: 'accepted', receipt_id: 'remote' } })
    const view = renderWithProviders(<BatchRetrievalView />)
    await waitFor(() => expect(view.getByRole('button', { name: 'Send batch to distillation' }).hasAttribute('disabled')).toBe(false))
    fireEvent.click(view.getByRole('button', { name: 'Send batch to distillation' }))
    await waitFor(() => expect(view.getByRole('alert').textContent).toContain('did not confirm'))
    expect(view.queryAllByRole('button', { name: 'Accepted by distillation' })).toHaveLength(0)
    fireEvent.click(view.getByRole('button', { name: 'Send batch to distillation' }))
    await waitFor(() => expect(view.getByRole('button', { name: 'Accepted by distillation' }).hasAttribute('disabled')).toBe(true))
    expect(send).toHaveBeenCalledTimes(2)
  })
})
