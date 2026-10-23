import { afterEach, expect, mock, spyOn, test } from 'bun:test'
import { cleanup, fireEvent, screen, waitFor } from '@testing-library/react'
import { renderWithProviders } from '@/test/render'
import FolderUpload from './FolderUpload'
import * as api from '@/api/lightrag'

afterEach(() => {
  cleanup()
  mock.restore()
})

function file(path: string) {
  const result = new File(['fictional content'], path.split('/').pop()!)
  Object.defineProperty(result, 'webkitRelativePath', { value: path })
  return result
}

test('imports nested same-name files separately, skips unsupported files and retries failures', async () => {
  const sent: string[] = []
  let fail = true
  spyOn(api, 'uploadDocument').mockImplementation(async (_file, _progress, relativePath) => {
    const path = relativePath!
    sent.push(path)
    if (path.includes('/b/') && fail) {
      fail = false
      throw api.toHttpRequestError(503, 'Unavailable', { detail: 'Temporary failure' })
    }
    return { status: 'success', message: 'submitted' }
  })
  const busy: boolean[] = []
  const { container } = renderWithProviders(
    <FolderUpload onBusyChange={(value) => busy.push(value)} />
  )
  fireEvent.change(container.querySelector('input')!, {
    target: { files: [file('root/a/budget.md'), file('root/b/budget.md'), file('root/photo.png')] }
  })
  expect(screen.getByRole('status').textContent).toContain('Skipped 1')
  fireEvent.click(screen.getByRole('button', { name: 'Import folder' }))
  await waitFor(() => expect(screen.getByRole('status').textContent).toContain('Failed 1'))
  fireEvent.click(screen.getByRole('button', { name: 'Retry failed files' }))
  await waitFor(() => expect(screen.getByRole('status').textContent).toContain('Submitted 2'))
  expect(sent).toEqual(['root/a/budget.md', 'root/b/budget.md', 'root/b/budget.md'])
  expect(busy).toEqual([true, false, true, false])
})

test('stopping preserves unsubmitted files and lets the current upload finish', async () => {
  let finish!: (response: api.DocActionResponse) => void
  let calls = 0
  spyOn(api, 'uploadDocument').mockImplementation(() => {
    calls++
    return new Promise<api.DocActionResponse>((resolve) => { finish = resolve })
  })
  const { container } = renderWithProviders(<FolderUpload onBusyChange={() => {}} />)
  fireEvent.change(container.querySelector('input')!, {
    target: { files: [file('root/a.md'), file('root/b.md')] }
  })
  fireEvent.click(screen.getByRole('button', { name: 'Import folder' }))
  fireEvent.click(screen.getByRole('button', { name: 'Stop remaining uploads' }))
  finish({ status: 'success', message: 'submitted' })
  await waitFor(() => expect(screen.getByRole('status').textContent).toContain('Submitted 1'))
  expect(screen.getByRole('status').textContent).toContain('Pending 1')
  expect(calls).toBe(1)
})
