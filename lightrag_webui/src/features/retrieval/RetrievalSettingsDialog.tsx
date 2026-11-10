import { useState } from 'react'
import { useTranslation } from 'react-i18next'
import Button from '@/components/ui/Button'
import Input from '@/components/ui/Input'
import { Dialog, DialogContent, DialogDescription, DialogHeader, DialogTitle, DialogTrigger } from '@/components/ui/Dialog'

export type RetrievalSettings = { min_neighbors: number; min_documents: number; graph_depth: number; rag_top_k: number }
export default function RetrievalSettingsDialog({ value, onSave }: { value: RetrievalSettings; onSave: (value: RetrievalSettings) => void }) {
  const { t } = useTranslation()
  const [open, setOpen] = useState(false)
  const [draft, setDraft] = useState(value)
  return <Dialog open={open} onOpenChange={next => { if (next) setDraft(value); setOpen(next) }}>
    <DialogTrigger asChild><Button variant="outline" type="button">{t('batchFlow.retrievalSettings')}</Button></DialogTrigger>
    <DialogContent><DialogHeader><DialogTitle>{t('batchFlow.retrievalSettings')}</DialogTitle><DialogDescription>{t('batchFlow.settingsHint')}</DialogDescription></DialogHeader>
      <form className="space-y-4" onSubmit={event => { event.preventDefault(); onSave(draft); setOpen(false) }}>
        {(['min_neighbors', 'min_documents', 'graph_depth', 'rag_top_k'] as const).map(key => <label className="block space-y-1 text-sm" key={key}>{t(`batch.${key}`)}<Input type="number" required min={key === 'graph_depth' ? 0 : 1} max={{ min_neighbors: 1000, min_documents: 100, graph_depth: 5, rag_top_k: 100 }[key]} value={draft[key]} onChange={event => setDraft(previous => ({ ...previous, [key]: Number(event.target.value) }))} /></label>)}
        <Button type="submit">{t('batchFlow.saveSettings')}</Button>
      </form>
    </DialogContent>
  </Dialog>
}
