import { expect, mock, test } from 'bun:test'
import { StrictMode } from 'react'
import { act, fireEvent, screen, waitFor } from '@testing-library/react'
import type { TrainingExport, BatchTask } from '@/api/batchControl'
import { batchClientFixture } from '@/test/batchControl'
import { renderWithProviders } from '@/test/render'
import BatchTrainingExports from './BatchTrainingExports'
import BatchTaskArtifacts from './BatchTaskArtifacts'

function exported(batch = 'batch-a', task = 'task-a'): TrainingExport {
  return { export_id: `export-${batch}`, data_batch_id: batch, task_id: task, config_revision_id: 'rev-a', format: 'messages', selection: 'processed', state: 'completed', counts: { samples: 3 }, error: null, created_at: 1, updated_at: 2 }
}

test('training export submits the chosen format and final-version policy for the displayed task', async () => {
  const { client } = batchClientFixture()
  renderWithProviders(<StrictMode><BatchTrainingExports client={client} batch="batch-a" task="task-a" /></StrictMode>)
  expect(client.trainingExports.mock.calls).toHaveLength(0)
  fireEvent.click(screen.getByRole('button', { name: 'Training export' }))
  await waitFor(() => expect(client.trainingExports.mock.calls.length).toBeGreaterThan(0))
  fireEvent.change(screen.getByRole('combobox', { name: 'Training format' }), { target: { value: 'alpaca' } })
  fireEvent.change(screen.getByRole('combobox', { name: 'QA selection' }), { target: { value: 'machine_passed' } })
  fireEvent.click(screen.getByRole('button', { name: 'Create training export' }))
  await screen.findByRole('button', { name: 'Download provenance' })
  expect(client.createTrainingExport.mock.calls[0].slice(0, 4)).toEqual(['batch-a', 'task-a', 'alpaca', 'machine_passed'])
  expect(screen.getByText(/3 QA records/).textContent?.includes('alpaca')).toBe(true)
})

test('failed submission retains its idempotency key and an outstanding list does not hide the error', async () => {
  const { client } = batchClientFixture()
  let resolve!: (value: { items: TrainingExport[] }) => void
  client.trainingExports.mockImplementationOnce(() => new Promise((done) => { resolve = done }))
  client.createTrainingExport.mockRejectedValueOnce(new Error('request outcome unknown'))
  renderWithProviders(<BatchTrainingExports client={client} batch="batch-a" task="task-a" />)
  fireEvent.click(screen.getByRole('button', { name: 'Training export' }))
  fireEvent.click(screen.getByRole('button', { name: 'Create training export' }))
  await screen.findByRole('alert')
  await act(async () => { resolve({ items: [] }) })
  expect(screen.getByRole('alert').textContent).toContain('request outcome unknown')
  fireEvent.click(screen.getByRole('button', { name: 'Create training export' }))
  await screen.findByRole('button', { name: 'Download training data' })
  expect(client.createTrainingExport.mock.calls[1][4]).toBe(client.createTrainingExport.mock.calls[0][4])
})

test('closing during submission and reopening does not leave the create button disabled', async () => {
  const { client } = batchClientFixture()
  let resolve!: (value: TrainingExport) => void
  client.createTrainingExport.mockImplementationOnce(() => new Promise((done) => { resolve = done }))
  renderWithProviders(<BatchTrainingExports client={client} batch="batch-a" task="task-a" />)
  fireEvent.click(screen.getByRole('button', { name: 'Training export' }))
  fireEvent.click(screen.getByRole('button', { name: 'Create training export' }))
  fireEvent.click(screen.getByRole('button', { name: 'Training export' }))
  expect(client.createTrainingExport.mock.calls[0][5]?.aborted).toBe(true)
  fireEvent.click(screen.getByRole('button', { name: 'Training export' }))
  await waitFor(() => expect((screen.getByRole('button', { name: 'Create training export' }) as HTMLButtonElement).disabled).toBe(false))
  fireEvent.click(screen.getByRole('button', { name: 'Create training export' }))
  await screen.findByRole('button', { name: 'Download provenance' })
  expect(client.createTrainingExport.mock.calls[1][4]).toBe(client.createTrainingExport.mock.calls[0][4])
  await act(async () => { resolve(exported()) })
})

test('late lists and download bodies from another batch are aborted and discarded', async () => {
  const { client } = batchClientFixture()
  let resolveList!: (value: { items: TrainingExport[] }) => void
  client.trainingExports.mockImplementationOnce(() => new Promise((done) => { resolveList = done }))
  const view = renderWithProviders(<BatchTrainingExports client={client} batch="batch-a" task="task-a" />)
  fireEvent.click(screen.getByRole('button', { name: 'Training export' }))
  view.rerender(<BatchTrainingExports client={client} batch="batch-b" task="task-b" />)
  expect(client.trainingExports.mock.calls[0][2]?.aborted).toBe(true)
  await act(async () => { resolveList({ items: [exported()] }) })
  expect(screen.queryAllByRole('button', { name: 'Download training data' })).toHaveLength(0)
  client.trainingExports.mockResolvedValueOnce({ items: [exported('batch-b', 'task-b')] })
  fireEvent.click(screen.getByRole('button', { name: 'Refresh exports' }))
  let resolveDownload!: (value: Blob) => void
  client.trainingExportDownload.mockImplementationOnce(() => new Promise((done) => { resolveDownload = done }))
  fireEvent.click(await screen.findByRole('button', { name: 'Download training data' }))
  await waitFor(() => expect(client.trainingExportDownload.mock.calls).toHaveLength(1))
  expect(client.trainingExportDownload.mock.calls[0].slice(0, 4)).toEqual(['batch-b', 'task-b', 'export-batch-b', 'dataset'])
  const originalCreate = URL.createObjectURL
  const create = mock(() => 'blob:stale-training')
  URL.createObjectURL = create
  try {
    view.rerender(<BatchTrainingExports client={client} batch="batch-a" task="task-a" />)
    expect(client.trainingExportDownload.mock.calls[0][4]?.aborted).toBe(true)
    await act(async () => { resolveDownload(new Blob(['old result'])) })
    expect(create.mock.calls).toHaveLength(0)
  } finally { URL.createObjectURL = originalCreate }
})

test('training downloads use authenticated client blobs and release the temporary URL', async () => {
  const { client, exportsByTask } = batchClientFixture()
  exportsByTask['batch-a:task-a'] = [exported()]
  const originalCreate = URL.createObjectURL
  const originalRevoke = URL.revokeObjectURL
  const originalClick = HTMLAnchorElement.prototype.click
  const downloads: string[] = []
  const revoke = mock(() => {})
  URL.createObjectURL = mock(() => 'blob:training-export')
  URL.revokeObjectURL = revoke
  HTMLAnchorElement.prototype.click = function () { downloads.push(this.download) }
  try {
    renderWithProviders(<BatchTrainingExports client={client} batch="batch-a" task="task-a" />)
    fireEvent.click(screen.getByRole('button', { name: 'Training export' }))
    fireEvent.click(await screen.findByRole('button', { name: 'Download provenance' }))
    await waitFor(() => expect(downloads).toEqual(['provenance-messages-export-batch-a.jsonl']))
    expect(client.trainingExportDownload.mock.calls[0].slice(0, 4)).toEqual(['batch-a', 'task-a', 'export-batch-a', 'provenance'])
    expect(revoke).toHaveBeenCalledWith('blob:training-export')
  } finally {
    URL.createObjectURL = originalCreate
    URL.revokeObjectURL = originalRevoke
    HTMLAnchorElement.prototype.click = originalClick
  }
})

test('task artifacts only show training export when the server grants it', () => {
  const { client } = batchClientFixture()
  const task: BatchTask = { task_id: 'task-a', data_batch_id: 'batch-a', config_revision_id: 'rev-a', kind: 'distillation', state: 'completed', terminal: true, phase: 'completed', cancel_requested: false, progress: null, result: null, error: null, waiting_reason: null, created_at: 0 }
  const view = renderWithProviders(<BatchTaskArtifacts client={client} batch="batch-a" task={task} />)
  expect(screen.queryAllByRole('button', { name: 'Training export' })).toHaveLength(0)
  view.rerender(<BatchTaskArtifacts client={client} batch="batch-a" task={{ ...task, can_export_training: true }} />)
  expect(screen.queryAllByRole('button', { name: 'Training export' })).toHaveLength(1)
})

test('an active export disables another submission until refreshed terminal progress arrives', async () => {
  const { client, exportsByTask } = batchClientFixture()
  exportsByTask['batch-a:task-a'] = [{ ...exported(), state: 'running', counts: { sources: 1, samples: 0 } }]
  renderWithProviders(<BatchTrainingExports client={client} batch="batch-a" task="task-a" />)
  fireEvent.click(screen.getByRole('button', { name: 'Training export' }))
  await screen.findByText(/0 QA records/)
  expect((screen.getByRole('button', { name: 'Create training export' }) as HTMLButtonElement).disabled).toBe(true)
  expect(screen.queryAllByRole('button', { name: 'Download training data' })).toHaveLength(0)
  exportsByTask['batch-a:task-a'] = [exported()]
  fireEvent.click(screen.getByRole('button', { name: 'Refresh exports' }))
  await screen.findByRole('button', { name: 'Download training data' })
  expect((screen.getByRole('button', { name: 'Create training export' }) as HTMLButtonElement).disabled).toBe(false)
})
