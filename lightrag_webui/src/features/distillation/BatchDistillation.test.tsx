import { afterEach, expect, mock, spyOn, test } from 'bun:test'
import { cleanup, fireEvent, waitFor } from '@testing-library/react'
import * as api from '@/api/dataWorkflow'
import { renderWithProviders } from '@/test/render'
import { recipeSettingsFixture, workflowFixture } from '@/test/dataWorkflow'
import BatchDistillation from './BatchDistillation'

afterEach(() => {
  cleanup()
  mock.restore()
})

function config() {
  spyOn(api, 'getWorkflowConfiguration').mockResolvedValue({ ready: true, issues: [] })
}

test('restores a workflow from backend and shows unknown progress without a fabricated percent', async () => {
  config()
  spyOn(api, 'findBatchWorkflow').mockResolvedValue(workflowFixture('completed'))
  const view = renderWithProviders(<BatchDistillation batchId="test-batch" available />)
  await waitFor(() => expect(view.getByText('Distillation complete')).toBeTruthy())
  expect(
    view.getByRole('button', { name: 'Distillation task created' }).hasAttribute('disabled')
  ).toBe(true)
  expect(view.getByText(/Processed Unknown sources/)).toBeTruthy()
  expect(view.queryAllByRole('progressbar')).toHaveLength(0)
})

test('polls real progress then stops at the terminal receipt', async () => {
  config()
  spyOn(api, 'findBatchWorkflow').mockResolvedValue(workflowFixture())
  const completed = workflowFixture('completed')
  completed.progress = { ...completed.progress, percent: 100, processed_sources: 2 }
  const poll = spyOn(api, 'getWorkflowJob').mockResolvedValue(completed)
  const view = renderWithProviders(<BatchDistillation batchId="test-batch" available />)
  await waitFor(() => expect(view.getByText('Distillation complete')).toBeTruthy())
  expect(view.getByRole('progressbar').getAttribute('value')).toBe('100')
  await new Promise((resolve) => setTimeout(resolve, 40))
  expect(poll).toHaveBeenCalledTimes(1)
})

test('submits once and keeps retrieval availability separate from workflow status', async () => {
  config()
  spyOn(api, 'findBatchWorkflow').mockResolvedValue(null)
  const submit = spyOn(api, 'submitBatchWorkflow').mockResolvedValue(workflowFixture('failed'))
  const view = renderWithProviders(<BatchDistillation batchId="test-batch" available />)
  const button = view.getByRole('button', { name: 'Send batch to distillation' })
  await waitFor(() => expect(button.hasAttribute('disabled')).toBe(false))
  fireEvent.click(button)
  fireEvent.click(button)
  await waitFor(() => expect(view.getByText('Distillation failed')).toBeTruthy())
  expect(submit).toHaveBeenCalledTimes(1)
  expect(submit.mock.calls[0][0]).toBe('test-batch')
})

test('loads logs using the backend byte cursor and displays an execution error', async () => {
  config()
  const failed = { ...workflowFixture('failed'), error: 'Model configuration invalid' }
  spyOn(api, 'findBatchWorkflow').mockResolvedValue(failed)
  const logs = spyOn(api, 'getWorkflowLogs')
    .mockResolvedValueOnce({ text: 'first', offset: 0, next_offset: 5, has_more: true })
    .mockResolvedValueOnce({ text: ' second', offset: 5, next_offset: 12, has_more: false })
  const view = renderWithProviders(<BatchDistillation batchId="test-batch" available />)
  await waitFor(() =>
    expect(view.getByRole('alert').textContent).toContain('Model configuration invalid')
  )
  fireEvent.click(view.getByText('Task details and logs'))
  fireEvent.click(view.getByRole('button', { name: 'Load more logs' }))
  await waitFor(() => expect(view.getByLabelText('Distillation logs').textContent).toBe('first'))
  fireEvent.click(view.getByRole('button', { name: 'Load more logs' }))
  await waitFor(() =>
    expect(view.getByLabelText('Distillation logs').textContent).toBe('first second')
  )
  expect(logs.mock.calls[1][1]).toBe(5)
})

test('restores an existing job even when configuration is temporarily unavailable', async () => {
  spyOn(api, 'getWorkflowConfiguration').mockRejectedValue(new Error('configuration unavailable'))
  spyOn(api, 'findBatchWorkflow').mockResolvedValue(workflowFixture('completed'))
  const view = renderWithProviders(<BatchDistillation batchId="test-batch" available />)
  await waitFor(() => expect(view.getByText('Distillation complete')).toBeTruthy())
  expect(view.queryAllByText('Restoring distillation task…')).toHaveLength(0)
})

test('explicitly starts a previously prepared batch without creating another workflow', async () => {
  config()
  spyOn(api, 'findBatchWorkflow').mockResolvedValue(workflowFixture('ready'))
  const start = spyOn(api, 'startWorkflowJob').mockResolvedValue(workflowFixture('completed'))
  const view = renderWithProviders(<BatchDistillation batchId="test-batch" available />)
  await waitFor(() =>
    expect(view.getByRole('button', { name: 'Start prepared task' }).hasAttribute('disabled')).toBe(
      false
    )
  )
  fireEvent.click(view.getByRole('button', { name: 'Start prepared task' }))
  await waitFor(() => expect(view.getByText('Distillation complete')).toBeTruthy())
  expect(start).toHaveBeenCalledTimes(1)
  expect(start.mock.calls[0][0]).toBe('distill-test')
})

test('a failed task uses an explicit retry with the same click ID after a network failure', async () => {
  config()
  spyOn(api, 'findBatchWorkflow').mockResolvedValue(workflowFixture('failed'))
  const retry = spyOn(api, 'retryWorkflowJob')
    .mockRejectedValueOnce(new Error('Connection lost'))
    .mockResolvedValue(workflowFixture('ready'))
  const view = renderWithProviders(<BatchDistillation batchId="test-batch" available />)
  await waitFor(() =>
    expect(view.getByRole('button', { name: 'Retry distillation' }).hasAttribute('disabled')).toBe(
      false
    )
  )
  fireEvent.click(view.getByRole('button', { name: 'Retry distillation' }))
  await waitFor(() => expect(view.getAllByRole('alert')).toHaveLength(1))
  fireEvent.click(view.getByRole('button', { name: 'Retry distillation' }))
  await waitFor(() =>
    expect(view.getAllByRole('button', { name: 'Start prepared task' })).toHaveLength(1)
  )
  expect(retry.mock.calls[0][0]).toBe('distill-test')
  expect(retry.mock.calls[0][1]).toBe(retry.mock.calls[1][1])
})

test('saving missing settings refreshes readiness for the failed batch', async () => {
  spyOn(api, 'getWorkflowConfiguration')
    .mockResolvedValueOnce({ ready: false, issues: ['Configuration unavailable'] })
    .mockResolvedValue({ ready: true, issues: [] })
  spyOn(api, 'findBatchWorkflow').mockResolvedValue(workflowFixture('failed'))
  const snapshot = recipeSettingsFixture()
  spyOn(api, 'getRecipeSettings').mockResolvedValue(snapshot)
  spyOn(api, 'saveRecipeSettings').mockImplementation(async (_revision, settings) => ({
    ...snapshot,
    settings
  }))
  const view = renderWithProviders(<BatchDistillation batchId="test-batch" available />)
  await waitFor(() => expect(view.getAllByText('Distillation failed')).toHaveLength(1))
  expect(view.getByRole('button', { name: 'Retry distillation' }).hasAttribute('disabled')).toBe(
    true
  )
  fireEvent.click(view.getByRole('button', { name: 'Distillation settings' }))
  await waitFor(() =>
    expect(view.getAllByRole('button', { name: 'Save and validate' })).toHaveLength(1)
  )
  fireEvent.click(view.getByRole('button', { name: 'Save and validate' }))
  await waitFor(() =>
    expect(view.getAllByText('Saved and validated. Applies to new tasks only.')).toHaveLength(1)
  )
  fireEvent.click(view.getByRole('button', { name: 'Close settings' }))
  await waitFor(() =>
    expect(view.getByRole('button', { name: 'Retry distillation' }).hasAttribute('disabled')).toBe(
      false
    )
  )
})
