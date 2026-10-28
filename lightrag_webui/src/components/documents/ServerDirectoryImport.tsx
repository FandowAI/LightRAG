import { useEffect, useId, useRef, useState } from 'react'
import { useTranslation } from 'react-i18next'
import Button from '@/components/ui/Button'
import Input from '@/components/ui/Input'
import {
  Dialog,
  DialogClose,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
  DialogTrigger
} from '@/components/ui/Dialog'
import { ServerIcon } from 'lucide-react'
import * as api from '@/api/serverImport'
import { errorMessage } from '@/lib/utils'

type Row = api.ServerFile & {
  state: 'ready' | 'sending' | 'submitted' | 'exists' | 'failed'
  error?: string
}
interface Props {
  disabled?: boolean
  onBusyChange: (busy: boolean) => void
  onAccepted?: () => void
  onComplete?: () => Promise<void>
}

export default function ServerDirectoryImport({
  disabled,
  onBusyChange,
  onAccepted,
  onComplete
}: Props) {
  const { t } = useTranslation()
  const id = useId()
  const [open, setOpen] = useState(false)
  const [roots, setRoots] = useState<string[] | null>(null)
  const [path, setPath] = useState('')
  const [directory, setDirectory] = useState<api.ServerDirectory | null>(null)
  const [recursive, setRecursive] = useState(true)
  const [fileTypes, setFileTypes] = useState('all')
  const [limits, setLimits] = useState<api.ServerImportConfiguration['limits']>()
  const [limitIssue, setLimitIssue] = useState<api.ServerImportLimitFailure | null>(null)
  const [retryAction, setRetryAction] = useState<'config' | 'browse' | 'preview' | null>(null)
  const [rows, setRows] = useState<Row[]>([])
  const [skipped, setSkipped] = useState(0)
  const [error, setError] = useState('')
  const [loading, setLoading] = useState(false)
  const [running, setRunning] = useState(false)
  const request = useRef<AbortController | null>(null)
  const stopped = useRef(false)
  const runningRef = useRef(false)
  useEffect(
    () => () => {
      stopped.current = true
      request.current?.abort()
    },
    []
  )
  const label = (key: string) => t(`serverImport.${key}`)
  const locked = disabled || loading || running

  function resetPreview() {
    setRows([])
    setSkipped(0)
    setError('')
    setLimitIssue(null)
    setRetryAction(null)
  }

  async function inspect(
    kind: 'config' | 'browse' | 'preview',
    target = path,
    includeSubdirectories = recursive
  ) {
    if (runningRef.current) return
    request.current?.abort()
    const controller = new AbortController()
    request.current = controller
    setLoading(true)
    setError('')
    setRetryAction(null)
    setLimitIssue(null)
    if (kind !== 'config') resetPreview()
    try {
      if (kind === 'config') {
        const result = await api.getServerImportConfig(controller.signal)
        if (!controller.signal.aborted) {
          setRoots(result.roots)
          setLimits(result.limits)
        }
      } else if (kind === 'browse') {
        const result = await api.browseServerDirectory(target, controller.signal)
        if (!controller.signal.aborted) {
          setDirectory(result)
          setPath(result.path)
          setRows([])
          setSkipped(0)
        }
      } else {
        const extensions =
          fileTypes === 'markdown'
            ? ['.md']
            : fileTypes === 'documents'
              ? ['.md', '.txt', '.pdf', '.docx']
              : undefined
        const result = await api.previewServerDirectory(
          target,
          includeSubdirectories,
          controller.signal,
          extensions
        )
        if (!controller.signal.aborted) {
          setRows(result.files.map((file) => ({ ...file, state: 'ready' })))
          setSkipped(result.skipped)
        }
      }
    } catch (failure) {
      if (!controller.signal.aborted) {
        const issue = api.getServerImportLimitFailure(failure)
        setError(
          issue
            ? t(`serverImport.${issue.kind}Limit`, { limit: issue.limit })
            : errorMessage(failure)
        )
        setLimitIssue(issue)
        setRetryAction(kind)
      }
    } finally {
      if (!controller.signal.aborted) setLoading(false)
    }
  }

  async function submit(retry = false) {
    if (runningRef.current || locked) return
    const queue = rows.filter((row) => row.state === (retry ? 'failed' : 'ready'))
    if (!queue.length) return
    runningRef.current = true
    stopped.current = false
    setRunning(true)
    setError('')
    setRetryAction(null)
    setLimitIssue(null)
    onBusyChange(true)
    const controller = new AbortController()
    request.current = controller
    let accepted = false
    try {
      for (const row of queue) {
        if (stopped.current) break
        const update = (state: Row['state'], message?: string) => {
          if (!controller.signal.aborted)
            setRows((all) =>
              all.map((item) =>
                item.path === row.path ? { ...item, state, error: message } : item
              )
            )
        }
        update('sending')
        try {
          const result = await api.importServerFile(row, controller.signal)
          if (result.status !== 'success') throw new Error(result.message)
          update('submitted')
          if (!accepted) onAccepted?.()
          accepted = true
        } catch (failure) {
          const message = errorMessage(failure)
          if (/already contains|already exists|identical content/i.test(message)) update('exists')
          else update('failed', message)
        }
      }
      if (accepted && !controller.signal.aborted) await onComplete?.()
    } catch (failure) {
      if (!controller.signal.aborted) setError(errorMessage(failure))
    } finally {
      runningRef.current = false
      if (!controller.signal.aborted) {
        setRunning(false)
        onBusyChange(false)
      }
    }
  }

  const count = (state: Row['state']) => rows.filter((row) => row.state === state).length
  return (
    <Dialog
      open={open}
      onOpenChange={(nextOpen) => {
        if (runningRef.current) return
        if (nextOpen && roots === null) void inspect('config')
        if (!nextOpen) {
          request.current?.abort()
          setLoading(false)
        }
        setOpen(nextOpen)
      }}
    >
      <DialogTrigger asChild>
        <Button type="button" size="sm" variant="outline" disabled={disabled || running}>
          <ServerIcon className="size-4" />
          {label('choose')}
        </Button>
      </DialogTrigger>
      <DialogContent className="max-h-[85dvh] overflow-y-auto sm:max-w-2xl">
        <DialogHeader>
          <DialogTitle>{label('title')}</DialogTitle>
          <DialogDescription>{label('hint')}</DialogDescription>
        </DialogHeader>
        <div className="space-y-4">
          {roots !== null && !roots.length && (
            <p className="text-muted-foreground text-sm">{label('disabled')}</p>
          )}
          {!!roots?.length && (
            <>
              <div className="flex flex-wrap gap-2">
                {roots.map((root) => (
                  <Button
                    key={root}
                    size="sm"
                    variant="outline"
                    disabled={locked}
                    onClick={() => void inspect('browse', root)}
                  >
                    {root}
                  </Button>
                ))}
              </div>
              <label className="block text-sm" htmlFor={`${id}-path`}>
                {label('path')}
              </label>
              <Input
                id={`${id}-path`}
                value={path}
                placeholder="/data/documents"
                disabled={locked}
                onChange={(event) => {
                  setPath(event.target.value)
                  resetPreview()
                  setDirectory(null)
                }}
              />
              <div className="flex flex-wrap items-center gap-2">
                <label className="text-sm" htmlFor={`${id}-types`}>
                  {label('fileTypes')}
                </label>
                <select
                  id={`${id}-types`}
                  className="border-input bg-background rounded-md border px-3 py-2 text-sm"
                  disabled={locked}
                  value={fileTypes}
                  onChange={(event) => {
                    setFileTypes(event.target.value)
                    resetPreview()
                  }}
                >
                  <option value="all">{label('allTypes')}</option>
                  <option value="documents">{label('documentTypes')}</option>
                  <option value="markdown">{label('markdownOnly')}</option>
                </select>
              </div>
              {limits && (
                <p className="text-muted-foreground text-xs">
                  {t('serverImport.previewLimits', {
                    files: limits.max_files,
                    entries: limits.max_entries
                  })}
                </p>
              )}
              <div className="flex flex-wrap items-center gap-2">
                <Button
                  size="sm"
                  variant="outline"
                  disabled={locked || !path.trim()}
                  onClick={() => void inspect('browse')}
                >
                  {label('browse')}
                </Button>
                {directory?.parent && (
                  <Button
                    size="sm"
                    variant="outline"
                    disabled={locked}
                    onClick={() => void inspect('browse', directory.parent!)}
                  >
                    {label('parent')}
                  </Button>
                )}
                <label className="flex items-center gap-2 text-sm">
                  <input
                    type="checkbox"
                    checked={recursive}
                    disabled={locked}
                    onChange={(event) => {
                      setRecursive(event.target.checked)
                      resetPreview()
                    }}
                  />
                  {label('recursive')}
                </label>
                <Button
                  size="sm"
                  disabled={locked || !path.trim()}
                  onClick={() => {
                    void inspect('preview')
                  }}
                >
                  {label('preview')}
                </Button>
              </div>
              {directory && (
                <div className="max-h-32 overflow-y-auto rounded border p-2">
                  <p className="text-muted-foreground text-xs break-all">{directory.path}</p>
                  {directory.directories.map((item) => (
                    <button
                      key={item.path}
                      type="button"
                      disabled={locked}
                      className="block p-1 text-sm underline"
                      onClick={() => void inspect('browse', item.path)}
                    >
                      {item.name}/
                    </button>
                  ))}
                  {!directory.directories.length && (
                    <p className="text-muted-foreground text-sm">{label('noFolders')}</p>
                  )}
                </div>
              )}
              <p role="status" className="text-sm">
                {t('serverImport.counts', {
                  ready: count('ready'),
                  submitted: count('submitted'),
                  exists: count('exists'),
                  failed: count('failed'),
                  skipped
                })}
              </p>
              {!!rows.length && (
                <ul className="max-h-40 space-y-1 overflow-y-auto text-xs">
                  {rows.slice(0, 80).map((row) => (
                    <li key={row.path} className="break-all">
                      {row.relative_path} · {label(row.state)}
                      {row.error && <p className="text-destructive">{row.error}</p>}
                    </li>
                  ))}
                </ul>
              )}
              {rows.length > 80 && (
                <p className="text-muted-foreground text-xs">{label('limited')}</p>
              )}
              <div className="flex flex-wrap gap-2">
                <Button
                  size="sm"
                  disabled={locked || !count('ready')}
                  onClick={() => void submit()}
                >
                  {label('submit')}
                </Button>
                {!running && count('submitted') > 0 && (
                  <span className="text-xs text-red-600" aria-live="polite">
                    {t('serverImport.addedSuccess', { count: count('submitted') })}
                  </span>
                )}
                {!!count('failed') && (
                  <Button
                    size="sm"
                    variant="outline"
                    disabled={locked}
                    onClick={() => void submit(true)}
                  >
                    {label('retry')}
                  </Button>
                )}
                {running && (
                  <Button
                    size="sm"
                    variant="outline"
                    onClick={() => {
                      stopped.current = true
                    }}
                  >
                    {label('stop')}
                  </Button>
                )}
              </div>
              <p className="text-muted-foreground text-xs">{label('queue')}</p>
            </>
          )}
          {loading && <p className="text-muted-foreground text-sm">{label('loading')}</p>}
          {error && (
            <div role="alert" className="text-destructive space-y-2 text-sm">
              <p>{error}</p>
              {limitIssue && <p>{label('limitHelp')}</p>}
              <div className="flex flex-wrap gap-3">
                {retryAction && (
                  <button
                    type="button"
                    className="underline"
                    disabled={locked}
                    onClick={() => void inspect(retryAction)}
                  >
                    {label(
                      retryAction === 'preview'
                        ? 'retryPreview'
                        : retryAction === 'browse'
                          ? 'retryBrowse'
                          : 'reload'
                    )}
                  </button>
                )}
                {limitIssue && retryAction === 'preview' && recursive && (
                  <button
                    type="button"
                    className="underline"
                    disabled={locked}
                    onClick={() => {
                      setRecursive(false)
                      void inspect('preview', path, false)
                    }}
                  >
                    {label('previewCurrentOnly')}
                  </button>
                )}
              </div>
            </div>
          )}
        </div>
        <DialogFooter>
          <DialogClose asChild>
            <Button type="button" variant="outline" disabled={running}>
              {label('back')}
            </Button>
          </DialogClose>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}
