import { afterEach, expect, mock, spyOn, test } from 'bun:test'
import { act, cleanup, fireEvent, screen, waitFor } from '@testing-library/react'
import { renderWithProviders } from '@/test/render'
import ServerDirectoryImport from './ServerDirectoryImport'
import UploadDocumentsDialog from './UploadDocumentsDialog'
import * as api from '@/api/serverImport'
import * as lightrag from '@/api/lightrag'
import type { DocActionResponse } from '@/api/lightrag'

afterEach(() => {
  cleanup()
  mock.restore()
})

const files: api.ServerFile[] = ['a/notes.md', 'b/notes.md'].map((relative_path) => ({
  path: `/srv/documents/${relative_path}`,
  relative_path,
  size: 10,
  version: 'a'.repeat(64)
}))

async function openPreview() {
  spyOn(api, 'getServerImportConfig').mockResolvedValue({
    enabled: true,
    roots: ['/srv/documents']
  })
  spyOn(api, 'browseServerDirectory').mockResolvedValue({
    path: '/srv/documents',
    parent: null,
    directories: []
  })
  spyOn(api, 'previewServerDirectory').mockResolvedValue({
    path: '/srv/documents',
    files,
    skipped: 1,
    excluded: []
  })
  fireEvent.click(screen.getByRole('button', { name: 'Choose server path' }))
  expect(screen.getAllByRole('dialog', { name: 'Import from server directory' })).toHaveLength(1)
  await waitFor(() =>
    expect(screen.getAllByRole('button', { name: '/srv/documents' })).toHaveLength(1)
  )
  fireEvent.click(screen.getByRole('button', { name: '/srv/documents' }))
  await waitFor(() =>
    expect((screen.getByLabelText('Server directory path') as HTMLInputElement).value).toBe(
      '/srv/documents'
    )
  )
  fireEvent.click(screen.getByRole('button', { name: 'Preview files' }))
  await waitFor(() => expect(screen.getByRole('status').textContent).toContain('Pending 2'))
}

test('previews server files and imports without browser file data, preserving failed rows for retry', async () => {
  const sent: string[] = []
  let fail = true
  spyOn(api, 'importServerFile').mockImplementation(async (file) => {
    sent.push(file.path)
    if (file.path.includes('/b/') && fail) {
      fail = false
      throw new Error('Temporary failure')
    }
    return { status: 'success', message: 'queued', track_id: 'upload-test' }
  })
  const busy: boolean[] = []
  let accepted = 0
  renderWithProviders(
    <ServerDirectoryImport
      onBusyChange={(value) => busy.push(value)}
      onAccepted={() => {
        accepted++
      }}
    />
  )
  await openPreview()
  expect(screen.getByRole('status').textContent).toContain('Skipped 1')
  fireEvent.click(screen.getByRole('button', { name: 'Import these files' }))
  await waitFor(() => expect(screen.getByRole('status').textContent).toContain('Failed 1'))
  fireEvent.click(screen.getByRole('button', { name: 'Retry failed imports' }))
  await waitFor(() => expect(screen.getByRole('status').textContent).toContain('Submitted 2'))
  expect(sent).toEqual([files[0].path, files[1].path, files[1].path])
  expect(busy).toEqual([true, false, true, false])
  expect(accepted).toBe(2)
  const feedback = screen.getByText(
    'Successfully added 2 files. Processing continues in the background.'
  )
  expect(feedback.classList.contains('text-red-600')).toBe(true)
  expect(
    feedback.parentElement ===
      screen.getByRole('button', { name: 'Import these files' }).parentElement
  ).toBe(true)
})

test('stop lets the current server import finish and retains the rest as pending', async () => {
  let finish!: (value: DocActionResponse) => void
  const sent: string[] = []
  spyOn(api, 'importServerFile').mockImplementation((file) => {
    sent.push(file.path)
    return new Promise((resolve) => {
      finish = resolve
    })
  })
  renderWithProviders(<ServerDirectoryImport onBusyChange={() => {}} />)
  await openPreview()
  fireEvent.click(screen.getByRole('button', { name: 'Import these files' }))
  fireEvent.click(screen.getByRole('button', { name: 'Stop remaining imports' }))
  await act(async () => {
    finish({ status: 'success', message: 'queued' })
  })
  await waitFor(() => expect(screen.getByRole('status').textContent).toContain('Submitted 1'))
  expect(screen.getByRole('status').textContent).toContain('Pending 1')
  expect(sent).toEqual([files[0].path])
})

test('changing the source directory clears a stale preview', async () => {
  renderWithProviders(<ServerDirectoryImport onBusyChange={() => {}} />)
  await openPreview()
  fireEvent.change(screen.getByLabelText('Server directory path'), {
    target: { value: '/srv/other' }
  })
  expect(screen.getByRole('status').textContent).toContain('Pending 0')
  expect(
    (screen.getByRole('button', { name: 'Import these files' }) as HTMLButtonElement).disabled
  ).toBe(true)
})

test('file type filtering is sent to the server and invalidates the previous preview', async () => {
  renderWithProviders(<ServerDirectoryImport onBusyChange={() => {}} />)
  await openPreview()
  const preview = spyOn(api, 'previewServerDirectory').mockResolvedValue({
    path: '/srv/documents',
    files,
    skipped: 0,
    excluded: []
  })
  fireEvent.change(screen.getByLabelText('File types'), { target: { value: 'markdown' } })
  expect(screen.getByRole('status').textContent).toContain('Pending 0')
  fireEvent.click(screen.getByRole('button', { name: 'Preview files' }))
  await waitFor(() => expect(screen.getByRole('status').textContent).toContain('Pending 2'))
  expect(preview.mock.calls.at(-1)?.[3]).toEqual(['.md'])
})

test('a preview limit offers a nonrecursive retry instead of reloading configuration', async () => {
  renderWithProviders(<ServerDirectoryImport onBusyChange={() => {}} />)
  await openPreview()
  const failure = lightrag.toHttpRequestError(422, 'Unprocessable Entity', {
    detail: { code: 'server_import_files_limit', limit: 5000, observed: 5001 }
  })
  const preview = spyOn(api, 'previewServerDirectory')
    .mockRejectedValueOnce(failure)
    .mockResolvedValue({ path: '/srv/documents', files, skipped: 0, excluded: [] })
  fireEvent.click(screen.getByRole('button', { name: 'Preview files' }))
  await waitFor(() => expect(screen.getByRole('alert').textContent).toContain('5000'))
  expect(screen.getByRole('alert').textContent).toContain('Choose a subdirectory')
  expect(screen.queryAllByRole('button', { name: 'Reload configuration' })).toHaveLength(0)
  fireEvent.click(screen.getByRole('button', { name: 'Preview this directory only' }))
  await waitFor(() => expect(screen.getByRole('status').textContent).toContain('Pending 2'))
  expect(preview.mock.calls.at(-1)?.[1]).toBe(false)
  expect(
    (screen.getByRole('checkbox', { name: 'Include subdirectories' }) as HTMLInputElement).checked
  ).toBe(false)
})

test('failed imports never display an added-success message', async () => {
  spyOn(api, 'importServerFile').mockRejectedValue(new Error('Upload refused'))
  renderWithProviders(<ServerDirectoryImport onBusyChange={() => {}} />)
  await openPreview()
  fireEvent.click(screen.getByRole('button', { name: 'Import these files' }))
  await waitFor(() => expect(screen.getByRole('status').textContent).toContain('Failed 2'))
  expect(screen.queryAllByText(/Successfully added/)).toHaveLength(0)
})

test('shows explicit setup guidance when no server roots are configured', async () => {
  spyOn(api, 'getServerImportConfig').mockResolvedValue({ enabled: false, roots: [] })
  renderWithProviders(<ServerDirectoryImport onBusyChange={() => {}} />)
  fireEvent.click(screen.getByRole('button', { name: 'Choose server path' }))
  await waitFor(() =>
    expect(screen.getAllByText(/No server directories are available/)).toHaveLength(1)
  )
  expect(screen.queryAllByRole('button', { name: 'Import these files' })).toHaveLength(0)
})

test('server controls load only after opening the dialog and the preview survives closing it', async () => {
  renderWithProviders(<ServerDirectoryImport onBusyChange={() => {}} />)
  expect(screen.queryAllByRole('dialog')).toHaveLength(0)
  expect(screen.queryAllByLabelText('Server directory path')).toHaveLength(0)
  await openPreview()
  fireEvent.click(screen.getByRole('button', { name: 'Back to upload' }))
  await waitFor(() => expect(screen.queryAllByRole('dialog')).toHaveLength(0))
  fireEvent.click(screen.getByRole('button', { name: 'Choose server path' }))
  expect((screen.getByLabelText('Server directory path') as HTMLInputElement).value).toBe(
    '/srv/documents'
  )
  expect(screen.getByRole('status').textContent).toContain('Pending 2')
})

test('an active server import cannot be dismissed with Escape', async () => {
  let finish!: (value: DocActionResponse) => void
  spyOn(api, 'importServerFile').mockImplementation(
    () =>
      new Promise((resolve) => {
        finish = resolve
      })
  )
  renderWithProviders(<ServerDirectoryImport onBusyChange={() => {}} />)
  await openPreview()
  fireEvent.click(screen.getByRole('button', { name: 'Import these files' }))
  expect(
    (screen.getByRole('button', { name: 'Back to upload' }) as HTMLButtonElement).disabled
  ).toBe(true)
  fireEvent.keyDown(screen.getByRole('dialog'), { key: 'Escape' })
  expect(screen.getAllByRole('dialog', { name: 'Import from server directory' })).toHaveLength(1)
  fireEvent.click(screen.getByRole('button', { name: 'Stop remaining imports' }))
  await act(async () => {
    finish({ status: 'success', message: 'queued' })
  })
  await waitFor(() =>
    expect(
      (screen.getByRole('button', { name: 'Back to upload' }) as HTMLButtonElement).disabled
    ).toBe(false)
  )
  fireEvent.click(screen.getByRole('button', { name: 'Back to upload' }))
  await waitFor(() => expect(screen.queryAllByRole('dialog')).toHaveLength(0))
})

test('the upload dialog places local and server choices together and remains open after returning', async () => {
  spyOn(lightrag, 'getSupportedFileTypes').mockResolvedValue({
    supported_extensions: ['.md'],
    engines: { native: ['.md'] }
  })
  spyOn(api, 'getServerImportConfig').mockResolvedValue({ enabled: false, roots: [] })
  renderWithProviders(<UploadDocumentsDialog />)
  fireEvent.click(screen.getByRole('button', { name: 'Upload' }))
  await waitFor(() =>
    expect(
      (screen.getByRole('button', { name: 'Choose server path' }) as HTMLButtonElement).disabled
    ).toBe(false)
  )
  const local = screen.getByRole('button', { name: 'Choose local folder' })
  const server = screen.getByRole('button', { name: 'Choose server path' })
  expect(local.parentElement === server.parentElement).toBe(true)
  fireEvent.click(server)
  await waitFor(() =>
    expect(screen.getAllByRole('dialog', { name: 'Import from server directory' })).toHaveLength(1)
  )
  fireEvent.click(screen.getByRole('button', { name: 'Back to upload' }))
  await waitFor(() =>
    expect(screen.queryAllByRole('dialog', { name: 'Import from server directory' })).toHaveLength(
      0
    )
  )
  expect(screen.getAllByRole('button', { name: 'Choose local folder' })).toHaveLength(1)
})
