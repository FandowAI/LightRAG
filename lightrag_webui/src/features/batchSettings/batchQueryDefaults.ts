import { defaultQuerySettings, type QuerySettings } from '@/stores/querySettings'
import { SUPPORTED_QUERY_MODES } from '@/features/retrieval/queryInput'

export function queryDefaults(values: Record<string, unknown>): Partial<QuerySettings> {
  if (typeof values !== 'object' || !values || Array.isArray(values)) throw new Error('Invalid batch query configuration')
  const result: Record<string, unknown> = {}
  for (const [key, fallback] of Object.entries({ ...defaultQuerySettings, response_type: '' })) {
    if (!(key in values) || key === 'history_turns') continue
    const value = values[key]
    if (typeof value !== typeof fallback || (typeof value === 'number' && (!Number.isInteger(value) || value <= 0))) {
      throw new Error(`Invalid batch query parameter: ${key}`)
    }
    if (key === 'mode' && !SUPPORTED_QUERY_MODES.includes(value as typeof SUPPORTED_QUERY_MODES[number])) throw new Error('Invalid batch query mode')
    if (key === 'graph_depth' && (value as number) > 5) throw new Error('Invalid batch graph depth')
    result[key] = value
  }
  return result
}
