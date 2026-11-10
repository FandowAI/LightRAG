import { create } from 'zustand'
import type { BatchRecord } from '@/api/batchControl'
import type { GenericAbortSignal } from 'axios'

type BatchContext = {
  batch: BatchRecord | null
  epoch: number
  signal: AbortSignal
  unsaved: boolean
  select: (batch: BatchRecord | null) => void
}

let controller = new AbortController()

export const useBatchContext = create<BatchContext>((set) => ({
  batch: null,
  epoch: 0,
  signal: controller.signal,
  unsaved: false,
  select: (batch) => {
    controller.abort()
    controller = new AbortController()
    set((current) => ({ batch, epoch: current.epoch + 1, signal: controller.signal, unsaved: false }))
  }
}))

const business = /^\/(?:health(?:[/?]|$)|documents(?:[/?]|$)|graph(?:s)?(?:[/?]|$)|query(?:[/?]|$)|prompt-admin(?:[/?]|$)|integrations\/data-work-flow(?:[/?]|$)|entity-merge(?:[/?]|$)|entity-centers(?:[/?]|$)|batch-retrieval(?:[/?]|$))/

export function isBatchBusinessPath(path: string): boolean {
  return business.test(path)
}

export function selectedBusinessPath(path: string): string {
  const { batch } = useBatchContext.getState()
  return batch && isBatchBusinessPath(path)
    ? `/data-batches/${encodeURIComponent(batch.id)}/api${path}`
    : path
}

const requests = new WeakMap<object, { epoch: number; dispose: () => void }>()

export function batchRequestSignal<T extends GenericAbortSignal>(existing?: T): { signal: T | AbortSignal | undefined; dispose: () => void } {
  const context = useBatchContext.getState()
  if (!context.batch) return { signal: existing, dispose: () => {} }
  const joined = new AbortController()
  const signals: GenericAbortSignal[] = existing ? [existing, context.signal] : [context.signal]
  const dispose = () => signals.forEach((signal) => signal.removeEventListener?.('abort', abort))
  const abort = () => { joined.abort(); dispose() }
  signals.forEach((signal) => {
    if (signal.aborted) abort()
    else signal.addEventListener?.('abort', abort, { once: true })
  })
  return { signal: joined.signal, dispose }
}

export function bindBatchRequest<T extends { url?: string; signal?: GenericAbortSignal }>(config: T): T {
  if (!config.url || !isBatchBusinessPath(config.url)) return config
  const context = useBatchContext.getState()
  const bound = batchRequestSignal(config.signal)
  requests.set(config, { epoch: context.epoch, dispose: bound.dispose })
  config.url = selectedBusinessPath(config.url)
  config.signal = bound.signal
  return config
}

export function assertBatchResponse(config?: object): void {
  const captured = config ? requests.get(config) : undefined
  captured?.dispose()
  if (captured && captured.epoch !== useBatchContext.getState().epoch) {
    throw new DOMException('The selected batch changed', 'AbortError')
  }
}
