import { afterEach, describe, expect, mock, test } from 'bun:test'
import { cleanup, fireEvent, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'

import { renderWithProviders } from '@/test/render'
import type { PromptSnapshot, PromptValue } from '@/api/promptAdmin'

let savedFields: Record<string, PromptValue> | null = null
let savedRevision: number | null = null
let publishedRevision: number | null = null
let resetCall: { language: string; revision: number } | null = null

const snapshot: PromptSnapshot = {
  revision: 0,
  write_enabled: true,
  groups: [
    {
      id: 'extraction_json',
      state: 'active',
      reason: 'JSON mode is active',
      has_draft: false,
      fields: [
        { key: 'entity_extraction_json_user_prompt', project_default: 'English: {input_text}', zh_default: '中文：{input_text}', default: 'Input: {input_text}', active: 'Input: {input_text}', draft: 'Input: {input_text}', customized: false },
        { key: 'entity_extraction_json_examples', project_default: ['{"entities":[{"name":"A3"}],"relationships":[]}'], zh_default: ['{"entities":[{"name":"甲"}],"relationships":[]}'], default: ['{"entities":[{"name":"A3"}],"relationships":[]}'], active: ['{"entities":[{"name":"A3"}],"relationships":[]}'], draft: ['{"entities":[{"name":"A3"}],"relationships":[]}'], customized: false }
      ]
    },
    {
      id: 'extraction_text',
      state: 'inactive',
      reason: 'Text mode is inactive',
      has_draft: false,
      fields: [{ key: 'entity_extraction_user_prompt', project_default: 'English: {input_text}', zh_default: '中文：{input_text}', default: 'Input: {input_text}', active: 'Input: {input_text}', draft: 'Input: {input_text}', customized: false }]
    }
  ]
}

mock.module('@/api/promptAdmin', () => ({
  getPromptSnapshot: async () => snapshot,
  getPromptHistory: async () => [{
    revision: 3,
    at: '2026-10-21T05:00:00Z',
    action: 'draft',
    actor: 'alice',
    fields: { entity_extraction_json_user_prompt: 'Recovered {input_text}' }
  }],
  savePromptDraft: async (group: string, revision: number, fields: Record<string, PromptValue>) => {
    savedFields = fields
    savedRevision = revision
    return { ...snapshot, revision: revision + 1, groups: snapshot.groups.map((item) => item.id === group ? { ...item, has_draft: true } : item) }
  },
  publishPromptDraft: async (_group: string, revision: number) => { publishedRevision = revision; return snapshot },
  resetPromptGroup: async (_group: string, revision: number, language: string) => { resetCall = { language, revision }; return snapshot },
}))

const { default: PromptManager } = await import('./PromptManager')

afterEach(() => { cleanup(); savedFields = null; savedRevision = null; publishedRevision = null; resetCall = null })

describe('prompt management', () => {
  test('keeps inactive variants in place and explains their live status', async () => {
    const user = userEvent.setup()
    renderWithProviders(<PromptManager />)

    const textTab = await screen.findByRole('button', { name: 'Delimiter text output' })
    await user.click(textTab)
    await user.click(screen.getByRole('button', { name: 'Edit' }))

    const heading = screen.getByRole('heading', { name: 'Entity and relation extraction' })
    expect(heading).toBeVisible()
    expect(screen.getByText('Not in use')).toBeVisible()
    expect(screen.getByText(/output protocol, not a Markdown input format/)).toBeVisible()
    expect(screen.getByRole('textbox', { name: /User instruction/ })).toBeEnabled()

    const details = screen.getAllByRole('button', { name: 'Usage details' })
    await user.click(details[details.length - 1])
    expect(within(document.body).getAllByText(/Delimiter text is an output protocol/).length).toBeGreaterThan(0)
  })

  test('shows JSON examples as raw source and saves the original list shape', async () => {
    const user = userEvent.setup()
    renderWithProviders(<PromptManager />)

    await user.click(await screen.findByRole('button', { name: 'Edit' }))
    const example = await screen.findByRole('textbox', { name: 'Example 1' }) as HTMLTextAreaElement
    expect(example.value).toBe('{"entities":[{"name":"A3"}],"relationships":[]}')
    expect(example.value).not.toContain('\\n')
    fireEvent.change(example, { target: { value: '{\n  "entities": [],\n  "relationships": []\n}' } })
    await user.click(screen.getByRole('button', { name: 'Other edit actions' }))
    await user.click(screen.getByRole('button', { name: 'Save draft' }))

    await waitFor(() => expect(savedFields?.entity_extraction_json_examples).toEqual([
      '{\n  "entities": [],\n  "relationships": []\n}'
    ]))
    expect(publishedRevision).toBeNull()
    await user.click(screen.getByRole('button', { name: 'Publish' }))
    await waitFor(() => expect(publishedRevision).toBe(1))
  })

  test('shows raw source in view mode and loads an old version only into the editor', async () => {
    const user = userEvent.setup()
    const field = snapshot.groups[0].fields[0]
    const original = field.draft
    field.draft = '# Prompt title\n\n**Important**'
    try {
      renderWithProviders(<PromptManager />)
      expect((await screen.findByText(/# Prompt title/)).tagName).toBe('PRE')
      expect(screen.getByText(/# Prompt title/).textContent).toBe('# Prompt title\n\n**Important**')
      expect(screen.queryByRole('heading', { name: 'Prompt title' })).toBeNull()
      expect(screen.queryByRole('textbox', { name: /User instruction/ })).toBeNull()

      await user.click(screen.getByRole('button', { name: 'Version history' }))
      await user.click(await screen.findByRole('button', { name: 'Load into editor' }))
      expect((screen.getByRole('textbox', { name: /User instruction/ }) as HTMLTextAreaElement).value).toBe('Recovered {input_text}')
      expect(savedFields).toBeNull()
    } finally {
      field.draft = original
    }
  })

  test('shows the source directly without a redundant preview action', async () => {
    renderWithProviders(<PromptManager />)
    expect((await screen.findByText('Input: {input_text}')).tagName).toBe('PRE')
    expect(screen.queryByRole('button', { name: 'Preview full prompt' })).toBeNull()
  })

  test('publishes editor changes with the revision returned by draft save', async () => {
    const user = userEvent.setup()
    renderWithProviders(<PromptManager />)

    await user.click(await screen.findByRole('button', { name: 'Edit' }))
    fireEvent.change(screen.getByRole('textbox', { name: /User instruction/ }), { target: { value: 'Changed: {input_text}' } })
    await user.click(screen.getByRole('button', { name: 'Publish' }))
    await waitFor(() => expect(publishedRevision).toBe(1))
    expect(savedRevision).toBe(0)
    expect(savedFields?.entity_extraction_json_user_prompt).toBe('Changed: {input_text}')
  })

  test('restores English from the menu directly, and Chinese with the main button', async () => {
    const user = userEvent.setup()
    renderWithProviders(<PromptManager />)

    await user.click(await screen.findByRole('button', { name: 'Other default versions' }))
    await user.click(screen.getByRole('button', { name: 'Restore project English original' }))
    await waitFor(() => expect(resetCall).toEqual({ language: 'en', revision: 0 }))
    expect(savedFields).toBeNull()

    const field = snapshot.groups[0].fields[0]
    field.customized = true
    try {
      await user.click(screen.getByRole('button', { name: 'Delimiter text output' }))
      await user.click(screen.getByRole('button', { name: 'JSON output' }))
      await user.click(screen.getByRole('button', { name: 'Restore configured defaults' }))
      await waitFor(() => expect(resetCall).toEqual({ language: 'zh', revision: 0 }))
    } finally {
      field.customized = false
    }
  })
})
