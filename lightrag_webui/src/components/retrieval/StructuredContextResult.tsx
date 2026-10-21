import { useTranslation } from 'react-i18next'
import type { QueryDataResponse } from '@/api/lightrag'

type Item = Record<string, unknown>

const value = (item: Item, ...keys: string[]): string => {
  for (const key of keys) {
    const candidate = item[key]
    if (typeof candidate === 'string' && candidate.trim()) return candidate
  }
  return ''
}

const readable = (text: string): string => text.replace(/<SEP>/g, '\n')

export function StructuredContextResult({ result }: { result: QueryDataResponse }) {
  const { t } = useTranslation()
  const entities = result.data.entities ?? []
  const relationships = result.data.relationships ?? []
  const chunks = result.data.chunks ?? []
  const references = result.data.references ?? []
  const sourceById = new Map(
    references.map((item) => [value(item, 'reference_id'), value(item, 'file_path')])
  )

  const groups: { key: string; items: Item[]; title: (item: Item, index: number) => string; body: (item: Item) => string }[] = [
    {
      key: 'entities', items: entities,
      title: (item, index) => value(item, 'entity_name', 'entity') || `#${index + 1}`,
      body: (item) => [value(item, 'entity_type', 'type'), value(item, 'description')].filter(Boolean).join('\n')
    },
    {
      key: 'relationships', items: relationships,
      title: (item, index) => {
        const from = value(item, 'src_id', 'entity1')
        const to = value(item, 'tgt_id', 'entity2')
        return from && to ? `${from} → ${to}` : `#${index + 1}`
      },
      body: (item) => value(item, 'description')
    },
    {
      key: 'chunks', items: chunks,
      title: (item, index) => {
        const source = sourceById.get(value(item, 'reference_id')) || value(item, 'file_path')
        return `${index + 1}. ${source || t('retrievePanel.querySettings.structuredChunks')}`
      },
      body: (item) => value(item, 'content')
    },
    {
      key: 'sources', items: references,
      title: (item, index) => value(item, 'file_path') || `#${index + 1}`,
      body: (item) => value(item, 'reference_id')
    }
  ]

  return (
    <div className="space-y-2 text-sm" data-testid="structured-context-result">
      <div className="flex flex-wrap gap-2 text-xs text-muted-foreground">
        {groups.map(({ key, items }) => (
          <span key={key}>{t(`retrievePanel.querySettings.structured${key[0].toUpperCase()}${key.slice(1)}`)}: {items.length}</span>
        ))}
      </div>
      {groups.filter(({ items }) => items.length > 0).map(({ key, items, title, body }) => (
        <details key={key} className="rounded-md border border-border p-2" open={key === 'chunks'}>
          <summary className="cursor-pointer font-medium">
            {t(`retrievePanel.querySettings.structured${key[0].toUpperCase()}${key.slice(1)}`)} ({items.length})
          </summary>
          <div className="mt-2 space-y-2">
            {items.map((item, index) => {
              const detail = readable(body(item))
              return (
                <details key={`${key}-${index}`} className="rounded border border-border/70 p-2">
                  <summary className="cursor-pointer break-words">
                    <span className="font-medium">{title(item, index)}</span>
                    {detail && <span className="ml-2 text-muted-foreground">{detail.slice(0, 100).replace(/\s+/g, ' ')}{detail.length > 100 ? '…' : ''}</span>}
                  </summary>
                  <pre className="mt-2 max-h-72 overflow-auto whitespace-pre-wrap break-words font-sans text-sm">{detail || JSON.stringify(item, null, 2)}</pre>
                </details>
              )
            })}
          </div>
        </details>
      ))}
      {groups.every(({ items }) => items.length === 0) && (
        <p className="text-muted-foreground">{t('retrievePanel.querySettings.structuredEmpty')}</p>
      )}
      <details className="rounded-md border border-border p-2">
        <summary className="cursor-pointer">{t('retrievePanel.querySettings.structuredRaw')}</summary>
        <pre className="mt-2 max-h-96 overflow-auto whitespace-pre-wrap break-all text-xs">{JSON.stringify(result, null, 2)}</pre>
      </details>
    </div>
  )
}
