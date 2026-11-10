import { beforeEach, afterEach, expect, mock, spyOn, test } from 'bun:test'
import { cleanup, fireEvent, waitFor, within } from '@testing-library/react'
import { renderWithProviders } from '@/test/render'
import * as api from '@/api/lightrag'
import * as workflowApi from '@/api/dataWorkflow'
import { workflowFixture, recipeSettingsFixture } from '@/test/dataWorkflow'
import { backendBaseUrl } from '@/lib/constants'
import { useAuthStore } from '@/stores/state'
import BatchRetrievalView from './BatchRetrievalView'
const retrieval: api.BatchJob = { id: 'four-step-job', status: 'completed', total: 2, processed: 2, failed: 0, empty: 0, export_path: '/exports/packages.jsonl', package_count: 1, current_center: null, cancel_requested: false, settings: { graph_depth: 2, rag_top_k: 10 }, handoff: { status: 'not_submitted' }, items: [] }
beforeEach(() => {
  spyOn(workflowApi, 'findBatchWorkflow').mockResolvedValue(null)
  spyOn(workflowApi, 'getWorkflowConfiguration').mockResolvedValue({ ready: true, issues: [] })
  spyOn(workflowApi, 'getWorkflowLogs').mockResolvedValue({ text: '', offset: 0, next_offset: 0, has_more: false })
})
afterEach(() => { cleanup(); mock.restore(); sessionStorage.clear(); localStorage.clear() })
function restore() {
  sessionStorage.setItem(`lightrag-batch:${backendBaseUrl}:${useAuthStore.getState().username || 'guest'}`, retrieval.id)
  spyOn(api, 'getBatchRetrieval').mockResolvedValue(retrieval)
}
test('offers exactly four steps and both configurations before any task', async () => {
  const recipe = spyOn(workflowApi, 'getRecipeSettings').mockResolvedValue(recipeSettingsFixture())
  const view = renderWithProviders(<BatchRetrievalView />)
  expect(within(view.getByRole('navigation', { name: 'Batch workflow steps' })).getAllByRole('button').length).toBe(4)
  expect(view.queryAllByRole('button', { name: /5.*results/i }).length).toBe(0)
  expect(view.getByRole('complementary', { name: 'Task progress and logs' })).toBeTruthy()
  fireEvent.click(view.getByRole('button', { name: 'Retrieval settings' }))
  fireEvent.change(view.getByRole('spinbutton', { name: 'Graph depth' }), { target: { value: '1' } })
  fireEvent.click(view.getByRole('button', { name: 'Save retrieval preferences' }))
  expect(JSON.parse(localStorage.getItem(`lightrag-retrieval-settings:${backendBaseUrl}:${useAuthStore.getState().username || 'guest'}:`) || '{}').graph_depth).toBe(1)
  fireEvent.click(view.getByRole('button', { name: 'Distillation settings' }))
  await waitFor(() => expect(recipe).toHaveBeenCalledTimes(1))
  expect(view.getByRole('dialog')).toBeTruthy()
})
test('switching steps preserves running logs and the captured retrieval configuration', async () => {
  restore()
  spyOn(workflowApi, 'findBatchWorkflow').mockResolvedValue({ ...workflowFixture(), source: { type: 'batch', batch_id: retrieval.id }, poll_after_ms: 10000 })
  const logs = spyOn(workflowApi, 'getWorkflowLogs').mockResolvedValue({ text: 'live log', offset: 0, next_offset: 8, has_more: false })
  const view = renderWithProviders(<BatchRetrievalView />)
  await waitFor(() => expect(view.getByLabelText('Distillation logs').textContent).toBe('live log'))
  fireEvent.click(view.getByRole('button', { name: /1.*Select entities/ }))
  fireEvent.click(view.getByRole('button', { name: /4.*Distill QA/ }))
  expect(view.getByLabelText('Distillation logs').textContent).toBe('live log')
  expect(logs.mock.calls.length).toBe(1)
  expect(view.getByRole('button', { name: 'Preview QA' }).hasAttribute('disabled')).toBe(true)
  fireEvent.click(view.getByRole('button', { name: 'Retrieval settings' }))
  fireEvent.change(view.getByRole('spinbutton', { name: 'Graph depth' }), { target: { value: '1' } })
  fireEvent.click(view.getByRole('button', { name: 'Save retrieval preferences' }))
  fireEvent.click(view.getByRole('button', { name: /2.*Batch retrieval/ }))
  expect(view.getByText('Graph depth 2 · 10 vector candidate chunks')).toBeTruthy()
})
test('previews all three evidence fields without a fifth step', async () => {
  restore()
  const get = spyOn(api.lightragAxios, 'get').mockResolvedValue({ data: { items: [{ entities: [{ name: 'department' }], relationships: [], original_texts: [{ text: 'source passage' }] }], offset: 0, next_offset: 1, has_more: false } })
  const view = renderWithProviders(<BatchRetrievalView />)
  await waitFor(() => expect(view.getByText('1 package ready')).toBeTruthy())
  fireEvent.click(view.getByRole('button', { name: /3.*Check packages/ }))
  await waitFor(() => expect(view.getByText('source passage')).toBeTruthy())
  expect(view.getByText('1 entities · 0 relationships · 1 passages')).toBeTruthy()
  expect(get.mock.calls[0][0]).toBe(`/batch-retrieval/jobs/${retrieval.id}/preview`)
  expect(view.getByRole('button', { name: 'Download all packages' })).toBeTruthy()
})
