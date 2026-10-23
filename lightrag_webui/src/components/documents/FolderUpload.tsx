import { useRef, useState } from 'react'
import { useTranslation } from 'react-i18next'
import { FolderOpenIcon } from 'lucide-react'
import Button from '@/components/ui/Button'
import { uploadDocument } from '@/api/lightrag'

type Row = {
  file: File
  path: string
  state: 'ready' | 'skipped' | 'sending' | 'submitted' | 'exists' | 'failed'
  error?: string
}
interface Props {
  disabled?: boolean
  onBusyChange: (busy: boolean) => void
  onAccepted?: () => void
  onComplete?: () => Promise<void>
}
const allowed = new Set(['txt', 'md', 'pdf', 'docx'])

export default function FolderUpload({ disabled, onBusyChange, onAccepted, onComplete }: Props) {
  const { t } = useTranslation()
  const copy = t('folderUpload', { returnObjects: true }) as Record<string, string>
  const input = useRef<HTMLInputElement>(null)
  const stopped = useRef(false)
  const [rows, setRows] = useState<Row[]>([])
  const [running, setRunning] = useState(false)
  const [batchError, setBatchError] = useState('')

  function choose(files: FileList | null) {
    if (!files?.length) return
    setBatchError('')
    setRows(
      Array.from(files).map((file) => {
        const supported = allowed.has(file.name.split('.').pop()?.toLowerCase() || '')
        const large = file.size > 20 * 1024 * 1024
        return {
          file,
          path: file.webkitRelativePath || file.name,
          state: !supported || large ? 'skipped' : 'ready',
          error: !supported ? copy.unsupported : large ? copy.large : undefined
        }
      })
    )
  }

  async function upload(retry = false) {
    if (running) return
    const queue = rows.filter((row) => row.state === (retry ? 'failed' : 'ready'))
    if (!queue.length) return
    setRunning(true)
    onBusyChange(true)
    stopped.current = false
    setBatchError('')
    let accepted = false
    try {
      for (const row of queue) {
        if (stopped.current) break
        const update = (state: Row['state'], error?: string) =>
          setRows((all) =>
            all.map((item) => (item.path === row.path ? { ...item, state, error } : item))
          )
        update('sending')
        try {
          await uploadDocument(row.file, undefined, row.path)
          update('submitted')
          if (!accepted) onAccepted?.()
          accepted = true
        } catch (error) {
          const failure = error as Error & { status?: number }
          if (failure.status === 409 && /already contains|already exists|identical content/i.test(failure.message)) update('exists')
          else update('failed', failure.message || String(error))
        }
      }
      if (accepted) await onComplete?.()
    } catch (error) {
      setBatchError(error instanceof Error ? error.message : String(error))
    } finally {
      setRunning(false)
      onBusyChange(false)
    }
  }

  const count = (state: Row['state']) => rows.filter((row) => row.state === state).length
  return (
    <section className="border-border space-y-3 rounded-md border p-3" aria-label={copy.choose}>
      <p className="text-muted-foreground text-sm">{copy.hint}</p>
      <input
        ref={(node) => {
          input.current = node
          node?.setAttribute('webkitdirectory', '')
        }}
        aria-label={copy.choose}
        type="file"
        multiple
        hidden
        onChange={(event) => {
          choose(event.currentTarget.files)
          event.currentTarget.value = ''
        }}
      />
      <div className="flex flex-wrap gap-2">
        <Button
          variant="outline"
          size="sm"
          disabled={disabled || running}
          onClick={() => input.current?.click()}
        >
          <FolderOpenIcon className="size-4" />
          {copy.choose}
        </Button>
        {rows.length > 0 && (
          <Button
            size="sm"
            disabled={disabled || running || !count('ready')}
            onClick={() => upload()}
          >
            {copy.submit}
          </Button>
        )}
        {!!count('failed') && (
          <Button
            variant="outline"
            size="sm"
            disabled={disabled || running}
            onClick={() => upload(true)}
          >
            {copy.retry}
          </Button>
        )}
        {running && (
          <Button
            variant="outline"
            size="sm"
            onClick={() => {
              stopped.current = true
            }}
          >
            {copy.stop}
          </Button>
        )}
      </div>
      {!!rows.length && (
        <>
          <p className="text-sm" role="status">
            {copy.ready} {count('ready')} · {copy.submitted} {count('submitted')} · {copy.exists}{' '}
            {count('exists')} · {copy.failed} {count('failed')} · {copy.skipped} {count('skipped')}
          </p>
          <ul className="max-h-40 space-y-2 overflow-y-auto text-xs">
            {rows.slice(0, 80).map((row) => (
              <li key={row.path} className="break-all">
                <span>{row.path}</span>
                <span className="text-muted-foreground ml-2">{copy[row.state]}</span>
                {row.error && <p className="text-red-600">{row.error}</p>}
              </li>
            ))}
          </ul>
          {rows.length > 80 && <p className="text-muted-foreground text-xs">{copy.limited}</p>}
          <p className="text-muted-foreground text-xs">{copy.queue}</p>
        </>
      )}
      {batchError && (
        <p role="alert" className="text-sm text-red-600">
          {batchError}
        </p>
      )}
    </section>
  )
}
