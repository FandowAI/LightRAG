import { useEffect, useMemo, useState } from 'react'
import { useTranslation } from 'react-i18next'
import { toast } from 'sonner'
import { ChevronDownIcon, HistoryIcon, InfoIcon, PencilIcon, RefreshCwIcon } from 'lucide-react'
import { Popover, PopoverContent, PopoverTrigger } from '@/components/ui/Popover'
import { Dialog, DialogContent, DialogTitle } from '@/components/ui/Dialog'
import Button from '@/components/ui/Button'
import PromptSource from './PromptSource'
import {
  getPromptHistory,
  getPromptSnapshot,
  publishPromptDraft,
  resetPromptGroup,
  savePromptDraft
} from '@/api/promptAdmin'
import type { PromptField, PromptGroup, PromptHistoryEntry, PromptSnapshot, PromptValue } from '@/api/promptAdmin'

type PromptCard = { id: string; variants: string[] }
const sections: Array<{ id: string; cards: PromptCard[] }> = [
  {
    id: 'document',
    cards: [
      { id: 'image', variants: ['image_analysis'] },
      { id: 'table', variants: ['table_analysis'] },
      { id: 'equation', variants: ['equation_analysis'] },
      { id: 'docx', variants: ['docx_title'] }
    ]
  },
  {
    id: 'ingestion',
    cards: [
      { id: 'extraction', variants: ['extraction_json', 'extraction_text'] },
      { id: 'gleaning', variants: ['gleaning_json', 'gleaning_text'] },
      { id: 'merge', variants: ['description_merge'] }
    ]
  },
  {
    id: 'query',
    cards: [
      { id: 'keywords', variants: ['keywords'] },
      { id: 'graph', variants: ['graph_answer'] },
      { id: 'naive', variants: ['naive_answer'] }
    ]
  }
]

const jsonExampleFields = new Set(['entity_extraction_json_examples', 'keywords_extraction_examples'])

const errorDetail = (error: unknown): string => {
  if (error && typeof error === 'object' && 'response' in error) {
    const response = (error as { response?: { data?: { detail?: unknown } } }).response
    if (typeof response?.data?.detail === 'string') return response.data.detail
  }
  return error instanceof Error ? error.message : String(error)
}

function InfoNote({ reason, label }: { reason: string; label: string }) {
  return (
    <Popover>
      <PopoverTrigger asChild>
        <button
          type="button"
          aria-label={label}
          className="inline-flex size-6 shrink-0 items-center justify-center rounded-full text-muted-foreground hover:bg-muted focus-visible:ring-2 focus-visible:ring-ring"
          onClick={(event) => event.stopPropagation()}
        >
          <InfoIcon className="size-4" aria-hidden="true" />
        </button>
      </PopoverTrigger>
      <PopoverContent className="w-72 text-sm" side="right">
        {reason}
      </PopoverContent>
    </Popover>
  )
}

export default function PromptManager() {
  const { t } = useTranslation()
  const [snapshot, setSnapshot] = useState<PromptSnapshot | null>(null)
  const [selectedCard, setSelectedCard] = useState('extraction')
  const [selectedGroup, setSelectedGroup] = useState('extraction_json')
  const [edits, setEdits] = useState<Record<string, Record<string, PromptValue>>>({})
  const [dirty, setDirty] = useState<Record<string, boolean>>({})
  const [isEditing, setIsEditing] = useState(false)
  const [historyOpen, setHistoryOpen] = useState(false)
  const [historyItems, setHistoryItems] = useState<PromptHistoryEntry[]>([])
  const [historyLoading, setHistoryLoading] = useState(false)
  const [busy, setBusy] = useState(false)
  const [publishMenuOpen, setPublishMenuOpen] = useState(false)
  const [resetMenuOpen, setResetMenuOpen] = useState(false)
  const [loadError, setLoadError] = useState('')

  useEffect(() => {
    let alive = true
    const refresh = async () => {
      try {
        const result = await getPromptSnapshot()
        if (alive) {
          setSnapshot(result)
          setLoadError('')
        }
      } catch (error) {
        if (alive) setLoadError(errorDetail(error))
      }
    }
    void refresh()
    const timer = window.setInterval(() => void refresh(), 15000)
    return () => {
      alive = false
      window.clearInterval(timer)
    }
  }, [])

  const groupMap = useMemo(() =>
    new Map((snapshot?.groups || []).map((group) => [group.id, group])),
  [snapshot])
  const current = groupMap.get(selectedGroup)
  const currentCard = sections.flatMap((section) => section.cards).find((card) => card.id === selectedCard)
  const examplesFor = (field: PromptField): string[] => {
    const value = edits[selectedGroup]?.[field.key] ?? field.draft
    return Array.isArray(value) ? value : []
  }

  const fieldsFor = (group: PromptGroup): Record<string, PromptValue> => {
    const values: Record<string, PromptValue> = {}
    for (const field of group.fields) {
      const edited = edits[group.id]?.[field.key]
      if (edited === undefined) {
        values[field.key] = field.draft
      } else if (Array.isArray(field.default)) {
        if (!Array.isArray(edited) || !edited.length || edited.some((item) => !item.trim())) {
          throw new Error(`${field.key}: ${t('promptAdmin.jsonArray', 'Expected a JSON array of strings')}`)
        }
        if (jsonExampleFields.has(field.key)) {
          edited.forEach((item, index) => {
            try {
              JSON.parse(item)
            } catch {
              throw new Error(`${field.key} #${index + 1}: ${t('promptAdmin.validJson', 'Expected valid JSON')}`)
            }
          })
        }
        values[field.key] = edited
      } else {
        if (typeof edited !== 'string') throw new Error(`${field.key}: expected text`)
        values[field.key] = edited
      }
    }
    return values
  }

  const clearEdits = (group: string) => {
    setEdits((state) => {
      const next = { ...state }
      delete next[group]
      return next
    })
    setDirty((state) => ({ ...state, [group]: false }))
  }

  const changeField = (group: string, key: string, value: PromptValue) => {
    setEdits((state) => ({ ...state, [group]: { ...state[group], [key]: value } }))
    setDirty((state) => ({ ...state, [group]: true }))
  }

  const run = async (task: () => Promise<PromptSnapshot>, message: string, closeEditor = true) => {
    setBusy(true)
    try {
      const result = await task()
      setSnapshot(result)
      clearEdits(selectedGroup)
      if (closeEditor) setIsEditing(false)
      toast.success(message)
    } catch (error) {
      toast.error(errorDetail(error))
      // Publishing may fail after its draft save succeeds; reload the new revision.
      try { setSnapshot(await getPromptSnapshot()) } catch { /* Keep the current editor contents. */ }
    } finally {
      setBusy(false)
    }
  }

  const statusLabel = (state: PromptGroup['state']) =>
    t(`promptAdmin.status.${state}`, state)
  const reasonFor = (group: PromptGroup) =>
    t(`promptAdmin.reason.${group.id}.${group.state}`, group.reason)
  const fieldLabel = (key: string) => {
    if (selectedCard === 'gleaning' && key.endsWith('_system_prompt')) {
      return t('promptAdmin.inheritedSystem', 'Inherited system instructions (shared with initial extraction)')
    }
    if (selectedCard === 'gleaning' && key.endsWith('_user_prompt') && !key.includes('continue')) {
      return t('promptAdmin.inheritedUser', 'Previous user instructions (shared with initial extraction)')
    }
    return t(`promptAdmin.field.${key}`, key)
  }
  const openHistory = () => {
    setHistoryOpen(true)
    setHistoryLoading(true)
    void getPromptHistory(selectedGroup)
      .then(setHistoryItems)
      .catch((error) => toast.error(errorDetail(error)))
      .finally(() => setHistoryLoading(false))
  }
  const restoreHistory = (entry: PromptHistoryEntry) => {
    setEdits((state) => ({ ...state, [selectedGroup]: entry.fields }))
    setDirty((state) => ({ ...state, [selectedGroup]: true }))
    setHistoryOpen(false)
    setIsEditing(true)
  }
  const saveDraft = () => {
    if (!current || !snapshot || !dirty[selectedGroup]) return
    try {
      const fields = fieldsFor(current)
      setPublishMenuOpen(false)
      void run(() => savePromptDraft(selectedGroup, snapshot.revision, fields), t('promptAdmin.draftSaved', 'Draft saved'), false)
    } catch (error) {
      toast.error(errorDetail(error))
    }
  }
  const publish = () => {
    if (!current || !snapshot || (!dirty[selectedGroup] && !current.has_draft)) return
    try {
      const fields = dirty[selectedGroup] ? fieldsFor(current) : null
      void run(async () => {
        const revision = fields
          ? (await savePromptDraft(selectedGroup, snapshot.revision, fields)).revision
          : snapshot.revision
        return publishPromptDraft(selectedGroup, revision)
      }, t('promptAdmin.published', 'Published for new requests'))
    } catch (error) {
      toast.error(errorDetail(error))
    }
  }
  const restoreDefault = (language: 'zh' | 'en') => {
    if (!snapshot) return
    setResetMenuOpen(false)
    void run(
      () => resetPromptGroup(selectedGroup, snapshot.revision, language),
      language === 'zh'
        ? t('promptAdmin.resetDone', 'Configured defaults restored')
        : t('promptAdmin.resetDoneEnglish', 'Project English original restored')
    )
  }

  return (
    <div className="flex h-full flex-col overflow-hidden bg-background">
      <div className="flex items-center justify-between border-b px-5 py-4">
        <div>
          <h1 className="text-lg font-semibold">{t('promptAdmin.title', 'Prompt management')}</h1>
          <p className="text-sm text-muted-foreground">
            {t('promptAdmin.subtitle', 'Edit complete model requests; status follows the running server configuration.')}
          </p>
        </div>
        <Button variant="ghost" size="icon" side="bottom" tooltip={t('promptAdmin.refresh', 'Refresh status')} onClick={() => void getPromptSnapshot().then(setSnapshot).catch((error) => setLoadError(errorDetail(error)))}>
          <RefreshCwIcon className="size-4" />
        </Button>
      </div>
      {loadError && <div role="alert" className="border-b px-5 py-2 text-sm text-red-600">{loadError}</div>}
      {!snapshot ? (
        <div className="p-6 text-sm text-muted-foreground">{t('promptAdmin.loading', 'Loading prompts…')}</div>
      ) : (
        <div className="flex min-h-0 flex-1 flex-col md:flex-row">
          <nav className="w-full shrink-0 overflow-y-auto border-b p-3 md:w-64 md:border-r md:border-b-0" aria-label={t('promptAdmin.cards', 'Prompt cards')}>
            {sections.map((section) => (
              <div key={section.id} className="mb-4">
                <div className="px-2 py-1 text-xs font-semibold text-muted-foreground uppercase">
                  {t(`promptAdmin.section.${section.id}`, section.id)}
                </div>
                {section.cards.map((card) => {
                  const groups = card.variants.map((id) => groupMap.get(id)).filter((item): item is PromptGroup => !!item)
                  const primary = groups.find((item) => item.state !== 'inactive') || groups[0]
                  return (
                    <div key={card.id} className={`mb-1 flex items-center rounded-md ${selectedCard === card.id ? 'bg-emerald-50 dark:bg-emerald-950' : 'hover:bg-muted'}`}>
                      <button
                        type="button"
                        className="min-w-0 flex-1 px-3 py-2 text-left text-sm"
                        onClick={() => {
                          setSelectedCard(card.id)
                          setSelectedGroup(primary?.id || card.variants[0])
                          setIsEditing(false)
                          setHistoryOpen(false)
                        }}
                      >
                        <span className="block truncate font-medium">{t(`promptAdmin.card.${card.id}`, card.id)}</span>
                        {primary && <span className="text-xs text-muted-foreground">{statusLabel(primary.state)}</span>}
                      </button>
                      {primary?.state === 'inactive' && <InfoNote reason={reasonFor(primary)} label={t('promptAdmin.info', 'Usage details')} />}
                    </div>
                  )
                })}
              </div>
            ))}
          </nav>
          <div className="min-w-0 flex-1 overflow-y-auto p-4 md:p-6">
            {current && (
              <div className="mx-auto max-w-5xl space-y-5">
                <div className="flex flex-wrap items-center gap-3">
                  <h2 className="text-xl font-semibold">{t(`promptAdmin.card.${selectedCard}`, selectedCard)}</h2>
                  <span className="rounded-full border px-2 py-0.5 text-xs">{statusLabel(current.state)}</span>
                  {current.state !== 'active' && <InfoNote reason={reasonFor(current)} label={t('promptAdmin.info', 'Usage details')} />}
                  {current.has_draft && <span className="text-xs text-amber-700">{t('promptAdmin.savedDraft', 'Saved draft')}</span>}
                  {dirty[selectedGroup] && <span className="text-xs text-amber-700">{t('promptAdmin.unsaved', 'Unsaved edits')}</span>}
                  <div className="ml-auto flex flex-wrap gap-2">
                    <div className="flex">
                      <Button variant="outline" size="sm" className="rounded-r-none" disabled={!snapshot.write_enabled || busy || !!dirty[selectedGroup] || !(current.has_draft || current.fields.some((field) => field.customized))} onClick={() => restoreDefault('zh')}>
                        {t('promptAdmin.reset', 'Restore configured defaults')}
                      </Button>
                      <Popover open={resetMenuOpen} onOpenChange={setResetMenuOpen}>
                        <PopoverTrigger asChild>
                          <Button variant="outline" size="sm" className="rounded-l-none border-l-0 px-2" aria-label={t('promptAdmin.resetOptions', 'Other restore options')} disabled={!snapshot.write_enabled || busy || !!dirty[selectedGroup]}>
                            <ChevronDownIcon />
                          </Button>
                        </PopoverTrigger>
                        <PopoverContent align="end" className="w-auto p-1">
                          <button type="button" className="w-full rounded-sm px-3 py-2 text-left text-sm hover:bg-accent focus-visible:bg-accent" onClick={() => restoreDefault('en')}>
                            {t('promptAdmin.resetEnglish', 'Restore project English original')}
                          </button>
                        </PopoverContent>
                      </Popover>
                    </div>
                    <Button variant="outline" size="sm" disabled={!snapshot.write_enabled} onClick={openHistory}><HistoryIcon />{t('promptAdmin.history', 'History')}</Button>
                    {isEditing ? (
                      <div className="flex">
                        <Button size="sm" className="rounded-r-none" disabled={!snapshot.write_enabled || busy || (!dirty[selectedGroup] && !current.has_draft)} onClick={publish}>
                          {t('promptAdmin.publish', 'Publish')}
                        </Button>
                        <Popover open={publishMenuOpen} onOpenChange={setPublishMenuOpen}>
                          <PopoverTrigger asChild>
                            <Button size="sm" className="rounded-l-none border-l border-primary-foreground/25 px-2" aria-label={t('promptAdmin.publishOptions', 'Other edit actions')} disabled={!snapshot.write_enabled || busy}>
                              <ChevronDownIcon />
                            </Button>
                          </PopoverTrigger>
                          <PopoverContent align="end" className="w-auto p-1">
                            <button type="button" className="w-full rounded-sm px-3 py-2 text-left text-sm hover:bg-accent focus-visible:bg-accent disabled:opacity-50" disabled={!dirty[selectedGroup]} onClick={saveDraft}>
                              {t('promptAdmin.saveDraft', 'Save draft')}
                            </button>
                            <button type="button" className="w-full rounded-sm px-3 py-2 text-left text-sm hover:bg-accent focus-visible:bg-accent" onClick={() => { clearEdits(selectedGroup); setIsEditing(false); setPublishMenuOpen(false) }}>
                              {t('promptAdmin.cancelEditing', 'Cancel editing')}
                            </button>
                          </PopoverContent>
                        </Popover>
                      </div>
                    ) : (
                      <Button size="sm" disabled={!snapshot.write_enabled || busy} onClick={() => setIsEditing(true)}><PencilIcon />{t('promptAdmin.edit', 'Edit')}</Button>
                    )}
                  </div>
                </div>
                {currentCard && currentCard.variants.length > 1 && (
                  <div className="flex gap-2 border-b pb-2">
                    {currentCard.variants.map((variant) => {
                      const option = groupMap.get(variant)
                      return option && (
                        <div key={variant} className={`flex items-center rounded-md border ${variant === selectedGroup ? 'border-emerald-500' : ''}`}>
                          <button type="button" className="px-3 py-1.5 text-sm" onClick={() => { setSelectedGroup(variant); setIsEditing(false); setHistoryOpen(false) }}>
                            {t(`promptAdmin.variant.${variant.endsWith('json') ? 'json' : 'text'}`, variant)}
                          </button>
                          {option.state === 'inactive' && <InfoNote reason={reasonFor(option)} label={t('promptAdmin.info', 'Usage details')} />}
                        </div>
                      )
                    })}
                  </div>
                )}
                {!snapshot.write_enabled && (
                  <div className="rounded-md border border-amber-300 bg-amber-50 p-3 text-sm text-amber-900 dark:bg-amber-950">
                    {t('promptAdmin.readOnly', 'Editing requires authentication and a single server worker.')}
                  </div>
                )}
                <p className="text-sm text-muted-foreground">{reasonFor(current)}</p>
                {selectedCard === 'gleaning' && (
                  <p className="rounded-md border border-blue-200 bg-blue-50 p-3 text-sm text-blue-900 dark:border-blue-900 dark:bg-blue-950 dark:text-blue-100">
                    {t('promptAdmin.gleaningShared', 'The system prompt, extraction examples, and previous user instructions are shared with initial extraction. Publishing changes here also changes initial extraction.')}
                  </p>
                )}
                {!isEditing && current.fields.map((field) => (
                  <section key={field.key} className="space-y-3 rounded-md border bg-card p-4">
                    <div className="text-sm font-semibold">
                      {fieldLabel(field.key)}
                      <span className="ml-2 font-normal text-muted-foreground">{field.key}</span>
                    </div>
                    {Array.isArray(field.default) ? (
                      <div className="space-y-3">
                        {examplesFor(field).map((example, index) => (
                          <div key={`${field.key}-${index}`} className="rounded-md border bg-background p-3">
                            <p className="mb-2 text-xs font-semibold text-muted-foreground">{t('promptAdmin.example', 'Example')} {index + 1}</p>
                            <PromptSource content={example} />
                          </div>
                        ))}
                      </div>
                    ) : (
                      <PromptSource content={String(edits[selectedGroup]?.[field.key] ?? field.draft)} />
                    )}
                  </section>
                ))}
                {isEditing && current.fields.map((field) => (
                  <div key={field.key} className="space-y-1.5">
                    <div className="block text-sm font-medium">
                      {fieldLabel(field.key)}
                      <span className="ml-2 font-normal text-muted-foreground">{field.key}</span>
                    </div>
                    {Array.isArray(field.default) ? (
                      <div className="space-y-3">
                        {examplesFor(field).map((example, index, examples) => (
                          <div key={`${field.key}-${index}`} className="rounded-md border p-3">
                            <div className="mb-2 flex items-center justify-between gap-2">
                              <label htmlFor={`prompt-${field.key}-${index}`} className="text-sm font-medium">
                                {t('promptAdmin.example', 'Example')} {index + 1}
                              </label>
                              <Button type="button" size="sm" variant="ghost" disabled={!snapshot.write_enabled || busy || examples.length <= 1} onClick={() => changeField(selectedGroup, field.key, examples.filter((_, itemIndex) => itemIndex !== index))}>
                                {t('promptAdmin.removeExample', 'Remove')}
                              </Button>
                            </div>
                            <textarea
                              id={`prompt-${field.key}-${index}`}
                              value={example}
                              onChange={(event) => changeField(selectedGroup, field.key, examples.map((item, itemIndex) => itemIndex === index ? event.target.value : item))}
                              disabled={!snapshot.write_enabled || busy}
                              spellCheck={false}
                              rows={Math.min(24, Math.max(7, example.split('\n').length + 1))}
                              className="w-full resize-y rounded-md border bg-background p-3 font-mono text-xs leading-relaxed focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring disabled:opacity-60"
                            />
                          </div>
                        ))}
                        <Button type="button" size="sm" variant="outline" disabled={!snapshot.write_enabled || busy} onClick={() => {
                          changeField(selectedGroup, field.key, [...examplesFor(field), jsonExampleFields.has(field.key) ? '{\n  \n}' : ''])
                        }}>
                          {t('promptAdmin.addExample', 'Add example')}
                        </Button>
                        <p className="text-xs text-muted-foreground">{jsonExampleFields.has(field.key)
                          ? t('promptAdmin.jsonExampleHelp', 'Edit one JSON object per example; the list wrapper is handled automatically.')
                          : t('promptAdmin.arrayHelp', 'Each example is edited separately; the list is saved automatically.')}</p>
                      </div>
                    ) : (
                      <textarea
                        id={`prompt-${field.key}`}
                        aria-label={fieldLabel(field.key)}
                        value={edits[selectedGroup]?.[field.key] ?? field.draft}
                        onChange={(event) => changeField(selectedGroup, field.key, event.target.value)}
                        disabled={!snapshot.write_enabled || busy}
                        spellCheck={false}
                        rows={Math.min(16, Math.max(6, String(field.draft).split('\n').length + 1))}
                        className="w-full resize-y rounded-md border bg-background p-3 font-mono text-xs leading-relaxed focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring disabled:opacity-60"
                      />
                    )}
                  </div>
                ))}
                <p className="text-xs text-muted-foreground">{t('promptAdmin.lifecycleHelp', 'Save draft keeps an unpublished copy. Publish applies it to new requests. Restore defaults removes published overrides; version history remains available.')}</p>
              </div>
            )}
          </div>
        </div>
      )}
      <Dialog open={historyOpen} onOpenChange={setHistoryOpen}>
        <DialogContent className="max-h-[85vh] max-w-2xl overflow-y-auto">
          <DialogTitle>{t('promptAdmin.history', 'History')}</DialogTitle>
          <p className="text-sm text-muted-foreground">{t('promptAdmin.historyHelp', 'Each saved draft, publication, and reset is recorded. Loading a version opens it in the editor without changing the live prompt.')}</p>
          {historyLoading ? <p className="text-sm">{t('promptAdmin.loading', 'Loading…')}</p> : historyItems.length === 0 ? (
            <p className="text-sm text-muted-foreground">{t('promptAdmin.noHistory', 'No saved versions yet.')}</p>
          ) : historyItems.map((entry) => (
            <div key={entry.revision} className="flex items-center justify-between gap-3 rounded-md border p-3">
              <div className="min-w-0 text-sm">
                <p className="font-medium">#{entry.revision} · {t(`promptAdmin.historyAction.${entry.action}`, entry.action)}</p>
                <p className="text-xs text-muted-foreground">{new Date(entry.at).toLocaleString()} · {entry.actor}</p>
              </div>
              <Button variant="outline" size="sm" disabled={!snapshot?.write_enabled} onClick={() => restoreHistory(entry)}>
                {t('promptAdmin.loadVersion', 'Load into editor')}
              </Button>
            </div>
          ))}
        </DialogContent>
      </Dialog>
    </div>
  )
}
