import { useEffect, useRef, useState } from 'react'
import { useTranslation } from 'react-i18next'
import Button from '@/components/ui/Button'
import { lightragAxios, type BatchJob } from '@/api/lightrag'

type Package = { package_id?: string; center?: string; entities: unknown[]; relationships: unknown[]; original_texts: { text?: string }[] }
type Page = { items: Package[]; offset: number; next_offset: number; has_more: boolean }
export default function PackagePreview({ job }: { job: BatchJob }) {
  const { t } = useTranslation()
  const [offset, setOffset] = useState(0)
  const [page, setPage] = useState<Page | null>(null)
  const [error, setError] = useState('')
  const [downloading, setDownloading] = useState(false)
  useEffect(() => {
    const controller = new AbortController()
    void lightragAxios.get(`/batch-retrieval/jobs/${encodeURIComponent(job.id)}/preview`, { params: { offset, limit: 10 }, signal: controller.signal }).then(response => { if (!controller.signal.aborted) { setPage(response.data); setError('') } }).catch(failure => { if (!controller.signal.aborted) setError(String(failure)) })
    return () => controller.abort()
  }, [job.id, offset])
  const controller = useRef<AbortController | null>(null)
  useEffect(() => () => controller.current?.abort(), [])
  async function download() {
    controller.current?.abort()
    const request = new AbortController(); controller.current = request
    setDownloading(true)
    try {
      const response = await lightragAxios.get(`/batch-retrieval/jobs/${encodeURIComponent(job.id)}/packages.jsonl`, { responseType: 'blob', signal: request.signal })
      if (request.signal.aborted) return
      const url = URL.createObjectURL(response.data)
      const link = document.createElement('a'); link.href = url; link.download = `${job.id}-packages.jsonl`; document.body.appendChild(link); link.click(); link.remove(); setTimeout(() => URL.revokeObjectURL(url), 0)
    } catch (failure) { if (!request.signal.aborted) setError(String(failure)) }
    finally { if (!request.signal.aborted) setDownloading(false) }
  }
  return <div className="space-y-4">
    <p className="text-sm text-muted-foreground">{t('batchFlow.packageHint')}</p>
    <Button type="button" variant="outline" disabled={downloading} onClick={() => void download()}>{t('batchFlow.downloadPackages')}</Button>
    {error && <p role="alert" className="text-sm text-destructive">{error}</p>}
    {!page && !error && <p role="status">{t('batchFlow.loadingResults')}</p>}
    {page?.items.map((item, index) => <article key={item.package_id ?? offset + index} className="space-y-2 rounded-lg border p-4">
      <h3 className="font-medium">{item.center || t('batchFlow.packageNumber', { count: offset + index + 1 })}</h3>
      <p className="text-sm text-muted-foreground">{t('batchFlow.packageFields', { entities: item.entities.length, relationships: item.relationships.length, texts: item.original_texts.length })}</p>
      <details className="text-sm"><summary>{t('batchFlow.graphFields')}</summary><pre className="max-h-64 overflow-auto whitespace-pre-wrap break-words text-xs">{JSON.stringify({ entities: item.entities, relationships: item.relationships }, null, 2)}</pre></details>
      <details className="text-sm"><summary>{t('batchFlow.originalTexts')}</summary><div className="max-h-96 space-y-3 overflow-auto pt-2">{item.original_texts.map((text, row) => <p className="whitespace-pre-wrap break-words" key={row}>{text.text}</p>)}</div></details>
    </article>)}
    {page && <div className="flex gap-2"><Button variant="outline" disabled={!offset} onClick={() => { setPage(null); setOffset(Math.max(0, offset - 10)) }}>{t('batchFlow.previous')}</Button><Button variant="outline" disabled={!page.has_more} onClick={() => { setPage(null); setOffset(page.next_offset) }}>{t('batchFlow.next')}</Button></div>}
  </div>
}
