import { afterEach, expect, mock, spyOn, test } from 'bun:test'
import { cleanup, fireEvent, screen, waitFor } from '@testing-library/react'
import * as api from '@/api/dataWorkflow'
import { renderWithProviders } from '@/test/render'
import { recipeSettingsFixture } from '@/test/dataWorkflow'
import DataWorkflowSettingsDialog from './DataWorkflowSettingsDialog'

afterEach(() => {
  cleanup()
  mock.restore()
})

async function openSettings() {
  fireEvent.click(screen.getByRole('button', { name: 'Distillation settings' }))
  await waitFor(() => expect(screen.getAllByText(/Recipe v3/)).toHaveLength(1))
}
const saveDisabled = () =>
  (screen.getByRole('button', { name: 'Save and validate' }) as HTMLButtonElement).disabled

test('shows only supported parameters and saves them without topic or score inputs', async () => {
  const snapshot = recipeSettingsFixture()
  spyOn(api, 'getRecipeSettings').mockResolvedValue(snapshot)
  const save = spyOn(api, 'saveRecipeSettings').mockImplementation(async (_revision, settings) => ({
    ...snapshot,
    revision: 'b'.repeat(64),
    settings
  }))
  const saved = mock(() => {})
  renderWithProviders(<DataWorkflowSettingsDialog onSaved={saved} />)
  expect(screen.queryAllByRole('dialog')).toHaveLength(0)
  await openSettings()
  expect(
    screen.queryAllByRole('button', { name: /Add topic|Load procurement example/ })
  ).toHaveLength(0)
  expect(screen.queryAllByLabelText(/Topic name|Fallback topic/)).toHaveLength(0)
  expect(screen.queryAllByText('Minimum passing scores by dimension')).toHaveLength(0)
  expect(screen.queryAllByLabelText(/Maximum revisions|Concurrent expert/)).toHaveLength(0)
  expect(screen.getAllByLabelText(/Inspection instructions/)).toHaveLength(1)
  expect(saveDisabled()).toBe(false)
  fireEvent.change(screen.getByLabelText('Maximum questions per source (questions_per_source)'), {
    target: { value: '3' }
  })
  fireEvent.click(screen.getByRole('button', { name: 'Save and validate' }))
  await waitFor(() =>
    expect(screen.getAllByText('Saved and validated. Applies to new tasks only.')).toHaveLength(1)
  )
  expect(save.mock.calls[0][0]).toBe('a'.repeat(64))
  expect(save.mock.calls[0][1]).toEqual({ ...snapshot.settings, questions_per_source: 3 })
  for (const key of ['topics', 'fallback_topic', 'topic_catalog_file', 'quality_thresholds']) {
    expect(key in save.mock.calls[0][1]).toBe(false)
  }
  expect(saved).toHaveBeenCalledTimes(1)
})

test('invalid remaining numeric parameters prevent saving', async () => {
  spyOn(api, 'getRecipeSettings').mockResolvedValue(recipeSettingsFixture())
  renderWithProviders(<DataWorkflowSettingsDialog onSaved={() => {}} />)
  await openSettings()
  expect(saveDisabled()).toBe(false)
  fireEvent.change(screen.getByLabelText('Maximum questions per source (questions_per_source)'), {
    target: { value: '0' }
  })
  expect(saveDisabled()).toBe(true)
  fireEvent.change(screen.getByLabelText('Maximum questions per source (questions_per_source)'), {
    target: { value: '2' }
  })
  expect(saveDisabled()).toBe(false)
})

test('validation errors remain visible and are not reported as saved', async () => {
  spyOn(api, 'getRecipeSettings').mockResolvedValue(recipeSettingsFixture())
  spyOn(api, 'saveRecipeSettings').mockRejectedValue(new Error('Invalid workflow parameters'))
  const saved = mock(() => {})
  renderWithProviders(<DataWorkflowSettingsDialog onSaved={saved} />)
  await openSettings()
  fireEvent.click(screen.getByRole('button', { name: 'Save and validate' }))
  await waitFor(() =>
    expect(screen.getByRole('alert').textContent).toContain('Invalid workflow parameters')
  )
  expect(saved).toHaveBeenCalledTimes(0)
  expect(screen.queryAllByText('Saved and validated. Applies to new tasks only.')).toHaveLength(0)
})

test('an installed schema mismatch prevents saving', async () => {
  spyOn(api, 'getRecipeSettings').mockResolvedValue({
    ...recipeSettingsFixture(),
    installed_schema_version: 4,
    editable: false
  })
  renderWithProviders(<DataWorkflowSettingsDialog onSaved={() => {}} />)
  await openSettings()
  expect(screen.getByRole('alert').textContent).toContain('does not match')
  expect(saveDisabled()).toBe(true)
})

test('model fields use the installed schema and Prompt edits bind a new version', async () => {
  const snapshot = recipeSettingsFixture()
  spyOn(api, 'getRecipeSettings').mockResolvedValue(snapshot)
  spyOn(api, 'saveRecipeSettings').mockImplementation(async (_revision, settings) => ({
    ...snapshot,
    settings
  }))
  const oldPath = snapshot.settings.stages.scenario.prompt_file!
  const newPath = '/config/prompts/workspace/scenario-new.md'
  spyOn(api, 'listPromptFiles').mockResolvedValue({
    directory: '/config/prompts/workspace',
    items: [{ path: oldPath, filename: 'scenario.md' }]
  })
  const document = {
    stage: 'scenario' as const,
    path: oldPath,
    filename: 'scenario.md',
    sha256: 'a'.repeat(64),
    content: 'Original $payload',
    preview: 'Original synthetic',
    variables: ['text', 'payload']
  }
  spyOn(api, 'readPromptFile').mockImplementation(async (_stage, path) => ({
    ...document,
    path,
    content: path === newPath ? 'Edited $payload' : document.content
  }))
  const publish = spyOn(api, 'savePromptFile').mockResolvedValue({
    ...document,
    path: newPath,
    content: 'Edited $payload'
  })
  spyOn(api, 'previewPrompt').mockResolvedValue({ preview: 'Edited synthetic context' })
  renderWithProviders(<DataWorkflowSettingsDialog onSaved={() => {}} />)
  await openSettings()
  fireEvent.click(screen.getByRole('tab', { name: 'Model profiles' }))
  expect(screen.getAllByLabelText('API endpoint (api_url)')).toHaveLength(1)
  fireEvent.change(screen.getByLabelText('Profile concurrency (max_workers)'), {
    target: { value: '2' }
  })
  fireEvent.click(screen.getByRole('tab', { name: 'Stages and Prompt files' }))
  await waitFor(() =>
    expect((screen.getByLabelText('Prompt text editor') as HTMLTextAreaElement).value).toBe(
      'Original $payload'
    )
  )
  fireEvent.change(screen.getByLabelText('Prompt text editor'), {
    target: { value: 'Edited $payload' }
  })
  await waitFor(() => expect(saveDisabled()).toBe(true))
  fireEvent.click(screen.getByRole('button', { name: 'Preview draft' }))
  await waitFor(() => expect(screen.getAllByText('Edited synthetic context')).toHaveLength(1))
  fireEvent.click(screen.getByRole('button', { name: 'Save Prompt version' }))
  await waitFor(() => expect(publish).toHaveBeenCalledTimes(1))
  await waitFor(() => expect(saveDisabled()).toBe(false))
  fireEvent.click(screen.getByRole('button', { name: 'Save and validate' }))
  await waitFor(() => expect(api.saveRecipeSettings).toHaveBeenCalledTimes(1))
  const sent = (api.saveRecipeSettings as ReturnType<typeof spyOn>).mock
    .calls[0][1] as api.RecipeValues
  expect(sent.stages.scenario.prompt_file).toBe(newPath)
  expect(sent.models.generator.max_workers).toBe(2)
  expect(publish.mock.calls[0]).toEqual(['scenario', 'scenario.md', 'Edited $payload'])
})

test('uploading a Prompt selects its published version and keeps a visible success notice', async () => {
  const snapshot = recipeSettingsFixture()
  spyOn(api, 'getRecipeSettings').mockResolvedValue(snapshot)
  const document = {
    stage: 'scenario' as const,
    path: snapshot.settings.stages.scenario.prompt_file!,
    filename: 'scenario.md',
    sha256: 'a'.repeat(64),
    content: 'Original $payload',
    preview: 'Original synthetic',
    variables: ['payload']
  }
  const uploaded = {
    ...document,
    path: '/config/prompts/workspace/scenario-uploaded.md',
    content: 'Uploaded $payload'
  }
  spyOn(api, 'listPromptFiles').mockResolvedValue({
    directory: '/config/prompts/workspace',
    items: []
  })
  spyOn(api, 'readPromptFile').mockImplementation(async (_stage, path) =>
    path === uploaded.path ? uploaded : document
  )
  const publish = spyOn(api, 'savePromptFile').mockResolvedValue(uploaded)
  const save = spyOn(api, 'saveRecipeSettings').mockImplementation(async (_revision, settings) => ({
    ...snapshot,
    settings
  }))
  renderWithProviders(<DataWorkflowSettingsDialog onSaved={() => {}} />)
  await openSettings()
  fireEvent.click(screen.getByRole('tab', { name: 'Stages and Prompt files' }))
  await waitFor(() =>
    expect((screen.getByLabelText('Prompt text editor') as HTMLTextAreaElement).value).toBe(
      document.content
    )
  )
  const file = new File(['Uploaded $payload'], 'custom.md', { type: 'text/markdown' })
  Object.defineProperty(file, 'arrayBuffer', {
    value: async () => new TextEncoder().encode('Uploaded $payload').buffer
  })
  fireEvent.change(screen.getByLabelText('Upload Prompt file'), { target: { files: [file] } })
  await waitFor(() =>
    expect((screen.getByLabelText('Prompt text editor') as HTMLTextAreaElement).value).toBe(
      uploaded.content
    )
  )
  await waitFor(() => expect(saveDisabled()).toBe(false))
  expect(publish.mock.calls[0]).toEqual(['scenario', 'custom.md', 'Uploaded $payload'])
  expect(
    screen.getAllByText('Prompt version saved. Save the configuration to apply it to new tasks.')
  ).toHaveLength(1)
  expect(save).toHaveBeenCalledTimes(0)
  fireEvent.click(screen.getByRole('button', { name: 'Save and validate' }))
  await waitFor(() => expect(save).toHaveBeenCalledTimes(1))
  expect(save.mock.calls[0][1].stages.scenario.prompt_file).toBe(uploaded.path)
})
