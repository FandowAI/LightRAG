import { afterAll, afterEach, expect, mock, test } from 'bun:test'
import { cleanup, fireEvent, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { renderWithProviders } from '@/test/render'

const api = await import('@/api/entityMerge')
const original = { ...api }
let verified = true

mock.module('@/api/entityMerge', () => ({
  getEntityMergeSettings: async () => ({
    workspace: 'test', revision: 1, write_enabled: true,
    embedding_model: 'test', embedding_dim: 4,
    identity_verification: { enabled: verified, uncertain_policy: 'keep_separate', registry: null },
    settings: { enabled: true, similarity_threshold: 0.78, margin_threshold: 0.05, top_k: 8 }
  }),
  saveEntityMergeSettings: async () => ({}),
  previewEntityMerge: async () => ({
    existing_exact_name: false, decision: 'candidate', target: null,
    candidates: [{ name: 'Example Department', type: 'Organization', score: 0.99 }]
  })
}))

const { default: EntityMergeSettingsDialog } = await import('./EntityMergeSettingsDialog')
afterEach(() => { cleanup(); verified = true })
afterAll(() => { mock.module('@/api/entityMerge', () => original) })

test('identity mode explains automatic evidence checks and removes the legacy margin control', async () => {
  const user = userEvent.setup()
  renderWithProviders(<EntityMergeSettingsDialog />)
  await user.click(screen.getByRole('button'))
  expect(await screen.findByText(/Keep uncertain objects separate/)).toBeVisible()
  expect(screen.queryAllByRole('spinbutton', { name: /margin/i })).toHaveLength(0)
  expect(screen.getByRole('checkbox', { name: /verified glossary remains active/ })).toBeVisible()
})

test('a high scoring preview is shown as a candidate, never as a confirmed merge', async () => {
  const user = userEvent.setup()
  renderWithProviders(<EntityMergeSettingsDialog />)
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
  renderWithProviders(<EntityMergeSettingsDialog />)
  await user.click(screen.getByRole('button'))
  expect(await screen.findByRole('spinbutton', { name: /margin/i })).toBeVisible()
})
