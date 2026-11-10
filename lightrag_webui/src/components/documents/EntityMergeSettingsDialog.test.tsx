import { batchClientFixture } from '@/test/batchControl'
import { afterAll, afterEach, expect, mock, spyOn, test } from 'bun:test'
import { act, cleanup, fireEvent, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { renderWithProviders } from '@/test/render'
import { toast } from 'sonner'
import { useBatchContext } from '@/stores/batchContext'
import { batchSettingsFixture } from '@/test/batchControl'
import type { EntityMergePreview, EntityMergeSnapshot } from '@/api/entityMerge'

const api = await import('@/api/entityMerge')
const original = { ...api }
let verified = true

function snapshot(): EntityMergeSnapshot {
  return {
    workspace: 'test', revision: 1, write_enabled: true,
    embedding_model: 'test', embedding_dim: 4,
    identity_verification: { enabled: verified, uncertain_policy: 'keep_separate', registry: null },
    settings: { enabled: true, similarity_threshold: 0.78, margin_threshold: 0.05, top_k: 8 }
  }
}
const candidate = (): EntityMergePreview => ({
  existing_exact_name: false, decision: 'candidate', target: null,
  candidates: [{ name: 'Example Department', type: 'Organization', score: 0.99 }]
})
const read = mock(async (signal?: AbortSignal) => { void signal; return snapshot() })
const save = mock(async (revision: number, settings: EntityMergeSnapshot['settings'], signal?: AbortSignal) => { void revision; void settings; void signal; return snapshot() })
const preview = mock(async (name: string, type: string, signal?: AbortSignal) => { void name; void type; void signal; return candidate() })
mock.module('@/api/entityMerge', () => ({
  getEntityMergeSettings: read, saveEntityMergeSettings: save, previewEntityMerge: preview
}))
function deferred<T>() {
  let resolve!: (value: T) => void
  let reject!: (error: Error) => void
  const promise = new Promise<T>((yes, no) => { resolve = yes; reject = no })
  return { promise, resolve, reject }
}

const { default: EntityMergeSettingsDialog } = await import('./EntityMergeSettingsDialog')
afterEach(() => {
  cleanup()
  useBatchContext.getState().select(null)
  verified = true
  read.mockReset().mockImplementation(async () => snapshot())
  save.mockReset().mockImplementation(async () => snapshot())
  preview.mockReset().mockImplementation(async () => candidate())
})
afterAll(() => { mock.module('@/api/entityMerge', () => original) })

test('identity mode explains automatic evidence checks and removes the legacy margin control', async () => {
  const user = userEvent.setup()
  renderWithProviders(<EntityMergeSettingsDialog chunkingClient={batchClientFixture().client} />)
  await user.click(screen.getByRole('button'))
  expect(await screen.findByText(/Keep uncertain objects separate/)).toBeVisible()
  expect(screen.queryAllByRole('spinbutton', { name: /margin/i })).toHaveLength(0)
  expect(screen.getByRole('checkbox', { name: /verified glossary remains active/ })).toBeVisible()
})

test('a high scoring preview is shown as a candidate, never as a confirmed merge', async () => {
  const user = userEvent.setup()
  renderWithProviders(<EntityMergeSettingsDialog chunkingClient={batchClientFixture().client} />)
  await user.click(screen.getByRole('button'))
  const fields = await screen.findAllByRole('textbox')
  fireEvent.change(fields[0], { target: { value: 'Example' } })
  fireEvent.change(fields[1], { target: { value: 'Organization' } })
  await user.click(screen.getByRole('button', { name: 'Preview' }))
  expect(await screen.findByText(/Name candidates only/)).toBeVisible()
  expect(screen.getByText('0.9900')).toBeVisible()
})

test('legacy mode retains the existing margin setting', async () => {
  verified = false
  const user = userEvent.setup()
  renderWithProviders(<EntityMergeSettingsDialog chunkingClient={batchClientFixture().client} />)
  await user.click(screen.getByRole('button'))
  expect(await screen.findByRole('spinbutton', { name: /margin/i })).toBeVisible()
})

test('closing and reopening discards an old settings read even if its transport ignores abort', async () => {
  const late = deferred<EntityMergeSnapshot>()
  read.mockImplementationOnce(() => late.promise)
  const user = userEvent.setup()
  renderWithProviders(<EntityMergeSettingsDialog chunkingClient={batchClientFixture().client} />)
  await user.click(screen.getByRole('button'))
  await waitFor(() => expect(read.mock.calls.length).toBe(1))
  const oldSignal = read.mock.calls[0][0]!
  await user.click(screen.getByRole('button', { name: 'Close' }))
  expect(oldSignal.aborted).toBe(true)
  await user.click(screen.getByRole('button'))
  expect(await screen.findByRole('button', { name: 'Save settings' })).toBeEnabled()
  await act(async () => late.resolve({ ...snapshot(), workspace: 'old-workspace', revision: 99 }))
  expect(screen.queryAllByText(/old-workspace/)).toHaveLength(0)
  expect(screen.getByRole('spinbutton', { name: 'Candidate Top K' }).getAttribute('value')).toBe('8')
})

test('a cancelled save cannot announce success or overwrite the reopened settings revision', async () => {
  const late = deferred<EntityMergeSnapshot>()
  save.mockImplementationOnce(() => late.promise)
  const success = spyOn(toast, 'success').mockImplementation(() => 'ignored')
  try {
    const user = userEvent.setup()
    renderWithProviders(<EntityMergeSettingsDialog chunkingClient={batchClientFixture().client} />)
    await user.click(screen.getByRole('button'))
    await user.click(await screen.findByRole('button', { name: 'Save settings' }))
    await waitFor(() => expect(save.mock.calls.length).toBe(1))
    const oldSignal = save.mock.calls[0][2]!
    await user.click(screen.getByRole('button', { name: 'Close' }))
    expect(oldSignal.aborted).toBe(true)
    await user.click(screen.getByRole('button'))
    expect(await screen.findByRole('button', { name: 'Save settings' })).toBeEnabled()
    await act(async () => late.resolve({ ...snapshot(), revision: 99 }))
    expect(success.mock.calls.length).toBe(0)
    await user.click(screen.getByRole('button', { name: 'Save settings' }))
    await waitFor(() => expect(save.mock.calls.length).toBe(2))
    expect(save.mock.calls[1][0]).toBe(1)
  } finally { success.mockRestore() }
})

test('a batch switch closes the old dialog, aborts preview and discards its late result', async () => {
  const late = deferred<EntityMergePreview>()
  preview.mockImplementationOnce(() => late.promise)
  useBatchContext.getState().select(batchSettingsFixture().batch)
  const user = userEvent.setup()
  renderWithProviders(<EntityMergeSettingsDialog chunkingClient={batchClientFixture().client} />)
  await user.click(screen.getByRole('button'))
  const fields = await screen.findAllByRole('textbox')
  fireEvent.change(fields[0], { target: { value: 'Old entity' } })
  fireEvent.change(fields[1], { target: { value: 'Organization' } })
  await user.click(screen.getByRole('button', { name: 'Preview' }))
  const oldSignal = preview.mock.calls[0][2]!
  act(() => useBatchContext.getState().select(batchSettingsFixture('batch-b').batch))
  expect(oldSignal.aborted).toBe(true)
  expect(screen.queryAllByRole('dialog')).toHaveLength(0)
  await user.click(screen.getByRole('button'))
  await screen.findByRole('button', { name: 'Save settings' })
  await act(async () => late.resolve({ ...candidate(), candidates: [{ name: 'Old candidate', type: 'Organization', score: 0.99 }] }))
  expect(screen.queryAllByText(/Old candidate/)).toHaveLength(0)
  expect((screen.getAllByRole('textbox')[0] as HTMLInputElement).value).toBe('')
})

test('a preview error after closing does not show an obsolete error notification', async () => {
  const late = deferred<EntityMergePreview>()
  preview.mockImplementationOnce(() => late.promise)
  const error = spyOn(toast, 'error').mockImplementation(() => 'ignored')
  try {
    const user = userEvent.setup()
    renderWithProviders(<EntityMergeSettingsDialog chunkingClient={batchClientFixture().client} />)
    await user.click(screen.getByRole('button'))
    const fields = await screen.findAllByRole('textbox')
    fireEvent.change(fields[0], { target: { value: 'Old entity' } })
    fireEvent.change(fields[1], { target: { value: 'Organization' } })
    await user.click(screen.getByRole('button', { name: 'Preview' }))
    await user.click(screen.getByRole('button', { name: 'Close' }))
    await act(async () => late.reject(new Error('old request failed')))
    expect(error.mock.calls.length).toBe(0)
  } finally { error.mockRestore() }
})
