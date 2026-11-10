import { afterEach, expect, test } from 'bun:test'
import { batchSettingsFixture } from '@/test/batchControl'
import { assertBatchResponse, batchRequestSignal, bindBatchRequest, selectedBusinessPath, useBatchContext } from './batchContext'

afterEach(() => useBatchContext.getState().select(null))

test('business requests route to the selected batch and authentication remains at the manager', () => {
  expect(selectedBusinessPath('/documents/paginated')).toBe('/documents/paginated')
  useBatchContext.getState().select(batchSettingsFixture().batch)
  expect(selectedBusinessPath('/documents/paginated')).toBe('/data-batches/batch-a/api/documents/paginated')
  expect(selectedBusinessPath('/graphs?label=*')).toBe('/data-batches/batch-a/api/graphs?label=*')
  expect(selectedBusinessPath('/entity-merge')).toBe('/data-batches/batch-a/api/entity-merge')
  expect(selectedBusinessPath('/entity-merge/preview')).toBe('/data-batches/batch-a/api/entity-merge/preview')
  expect(selectedBusinessPath('/auth-status')).toBe('/auth-status')
  expect(selectedBusinessPath('/data-batches')).toBe('/data-batches')
})

test('entity merge requests abort on a batch change and cannot publish old responses', () => {
  useBatchContext.getState().select(batchSettingsFixture().batch)
  const call = bindBatchRequest({ url: '/entity-merge/preview', signal: new AbortController().signal })
  useBatchContext.getState().select(batchSettingsFixture('batch-b').batch)
  expect(call.url).toBe('/data-batches/batch-a/api/entity-merge/preview')
  expect(call.signal?.aborted).toBe(true)
  expect(() => assertBatchResponse(call)).toThrow('The selected batch changed')
})

test('switching batches aborts owned calls and rejects an old default-workspace response', () => {
  const legacy = bindBatchRequest({ url: '/documents/paginated' })
  useBatchContext.getState().select(batchSettingsFixture().batch)
  const owned = bindBatchRequest({ url: '/graphs', signal: new AbortController().signal })
  const oldSignal = owned.signal!
  useBatchContext.getState().select(batchSettingsFixture('batch-b').batch)
  expect(oldSignal.aborted).toBe(true)
  expect(() => assertBatchResponse(legacy)).toThrow('The selected batch changed')
  expect(() => assertBatchResponse(owned)).toThrow('The selected batch changed')
  expect(owned.url).toBe('/data-batches/batch-a/api/graphs')
})

test('explicit query cancellation remains connected after a batch signal is added', () => {
  useBatchContext.getState().select(batchSettingsFixture().batch)
  const user = new AbortController()
  const joined = batchRequestSignal(user.signal)
  user.abort()
  expect(joined.signal!.aborted).toBe(true)
  joined.dispose()
})
