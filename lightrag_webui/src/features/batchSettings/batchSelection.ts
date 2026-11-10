import { backendBaseUrl } from '@/lib/constants'
import type { BatchRecord } from '@/api/batchControl'

export const lastBatchStorageKey = (username: string | null) =>
  `lightrag-last-batch:${backendBaseUrl}:${username ?? 'guest'}`

export function findRememberedBatch(batches: BatchRecord[], rememberedId: string | null): BatchRecord | undefined {
  return rememberedId ? batches.find((batch) => batch.id === rememberedId) : undefined
}
