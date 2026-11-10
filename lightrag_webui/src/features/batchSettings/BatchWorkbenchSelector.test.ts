import { expect, test } from 'bun:test'
import { batchSettingsFixture } from '@/test/batchControl'
import { findRememberedBatch, lastBatchStorageKey } from './batchSelection'

test('remembered batch lookup ignores missing batches', () => {
  const available = [batchSettingsFixture('batch-a').batch, batchSettingsFixture('batch-b').batch]

  expect(findRememberedBatch(available, 'batch-b')?.id).toBe('batch-b')
  expect(findRememberedBatch(available, 'batch-missing')).toBeUndefined()
  expect(lastBatchStorageKey('alice')).not.toBe(lastBatchStorageKey('bob'))
})
