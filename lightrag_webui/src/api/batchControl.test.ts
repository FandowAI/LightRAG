import { afterEach, expect, mock, test } from 'bun:test'
import { createBatchClient, type BatchHttpError } from './batchControl'

const originalFetch = globalThis.fetch
const originalToken = localStorage.getItem('LIGHTRAG-API-TOKEN')

test('index rebuild API pins batch, model, revision and recovery version with cancellation signals', async () => {
  const fetcher = mock(async (_input: RequestInfo | URL, init?: RequestInit) => {
    expect((init?.headers as Record<string, string>)['X-API-Key']).toBe('fixture-key')
    return new Response('{}')
  })
  globalThis.fetch = Object.assign(fetcher, { preconnect: originalFetch.preconnect })
  const client = createBatchClient('fixture-key')
  const controller = new AbortController()
  await client.startIndexRebuild('batch/a', 'rev-old', 'embed-new', 'request-a', controller.signal)
  expect(JSON.parse(String(fetcher.mock.calls[0][1]?.body))).toEqual({ expected_revision: 'rev-old', embedding_profile: 'embed-new', request_id: 'request-a' })
  expect(String(fetcher.mock.calls[0][0]).endsWith('/data-batches/batch%2Fa/index-rebuilds')).toBe(true)
  expect(fetcher.mock.calls[0][1]?.signal === controller.signal).toBe(true)
  await client.cancelIndexRebuild('batch/a', 'op/b', controller.signal)
  expect(String(fetcher.mock.calls[1][0]).endsWith('/index-rebuilds/op%2Fb/cancel')).toBe(true)
  await client.recoverIndexRebuild('batch/a', 'op/b', 9, controller.signal)
  expect(JSON.parse(String(fetcher.mock.calls[2][1]?.body))).toEqual({ expected_version: 9 })
})
afterEach(() => {
  globalThis.fetch = originalFetch
  if (originalToken === null) localStorage.removeItem('LIGHTRAG-API-TOKEN')
  else localStorage.setItem('LIGHTRAG-API-TOKEN', originalToken)
})

test('batch API uses same-origin scope and key authentication without an unrelated JWT', async () => {
  localStorage.setItem('LIGHTRAG-API-TOKEN', 'unrelated-token')
  const fetcher = mock(async (_input: RequestInfo | URL, init?: RequestInit) => {
    expect((init?.headers as Record<string, string>)['X-API-Key']).toBe('fixture-key')
    expect((init?.headers as Record<string, string>).Authorization).toBeUndefined()
    return new Response(JSON.stringify({ items: [] }))
  })
  globalThis.fetch = Object.assign(fetcher, { preconnect: originalFetch.preconnect })
  await createBatchClient('fixture-key').list()
  expect(String(fetcher.mock.calls[0][0]).endsWith('/data-batches')).toBe(true)
  expect(localStorage.getItem('LIGHTRAG-API-TOKEN')).toBe('unrelated-token')
})

test('batch API carries validation/conflict status for draft preservation', async () => {
  globalThis.fetch = Object.assign(mock(async () => new Response(JSON.stringify({ detail: 'Revision changed' }), { status: 409 })), { preconnect: originalFetch.preconnect })
  let failure: BatchHttpError | undefined
  try { await createBatchClient('fixture-key').discard('batch-a', 'op-a') } catch (error) { failure = error as BatchHttpError }
  expect(failure?.status).toBe(409)
  expect(failure?.message.includes('Revision changed')).toBe(true)
})

test('batch application request pins desired revision and retry identity', async () => {
  const fetcher = mock(async (_input: RequestInfo | URL, init?: RequestInit) => {
    expect(JSON.parse(String(init?.body))).toEqual({ expected_revision: 'rev-new', request_id: 'retry-1' })
    return new Response(JSON.stringify({ state: 'queued' }))
  })
  globalThis.fetch = Object.assign(fetcher, { preconnect: originalFetch.preconnect })
  await createBatchClient('fixture-key').apply('batch-a', 'op-a', 'rev-new', 'retry-1')
  expect(String(fetcher.mock.calls[0][0]).endsWith('/data-batches/batch-a/applications/op-a/start')).toBe(true)
})

test('artifact download uses batch/task scoping and authentication for binary data', async () => {
  const fetcher = mock(async (_input: RequestInfo | URL, init?: RequestInit) => {
    expect((init?.headers as Record<string, string>)['X-API-Key']).toBe('fixture-key')
    return new Response('source evidence', { headers: { 'Content-Type': 'application/x-ndjson' } })
  })
  globalThis.fetch = Object.assign(fetcher, { preconnect: originalFetch.preconnect })
  const blob = await createBatchClient('fixture-key').taskDownload('batch-a', 'task-a', 'retrieval-sources')
  expect(await blob.text()).toBe('source evidence')
  expect(String(fetcher.mock.calls[0][0]).endsWith('/data-batches/batch-a/tasks/task-a/artifacts/retrieval-sources')).toBe(true)
})

test('training export requests and downloads preserve task identity, policy and authentication', async () => {
  const fetcher = mock(async (_input: RequestInfo | URL, init?: RequestInit) => {
    expect((init?.headers as Record<string, string>)['X-API-Key']).toBe('fixture-key')
    return new Response('{}')
  })
  globalThis.fetch = Object.assign(fetcher, { preconnect: originalFetch.preconnect })
  const client = createBatchClient('fixture-key')
  const controller = new AbortController()
  await client.createTrainingExport('batch/a', 'task/b', 'sharegpt', 'machine_passed', 'request-a', controller.signal)
  const first = fetcher.mock.calls[0]
  expect(String(first[0]).endsWith('/data-batches/batch%2Fa/tasks/task%2Fb/training-exports')).toBe(true)
  expect(JSON.parse(String(first[1]?.body))).toEqual({ format: 'sharegpt', selection: 'machine_passed', request_id: 'request-a' })
  expect(first[1]?.signal === controller.signal).toBe(true)
  await client.trainingExports('batch/a', 'task/b')
  await client.trainingExportStatus('batch/a', 'task/b', 'export/c')
  expect(String(fetcher.mock.calls[2][0]).endsWith('/training-exports/export%2Fc')).toBe(true)
  const blob = await client.trainingExportDownload('batch/a', 'task/b', 'export/c', 'provenance', controller.signal)
  expect(await blob.text()).toBe('{}')
  expect(String(fetcher.mock.calls[3][0]).endsWith('/training-exports/export%2Fc/files/provenance')).toBe(true)
})
