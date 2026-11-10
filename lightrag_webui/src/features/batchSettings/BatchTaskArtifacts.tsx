import { useMemo, useState } from 'react'
import { useTranslation } from 'react-i18next'
import type { BatchArtifactInfo, BatchClient, BatchLog, BatchLogStream, BatchTask } from '@/api/batchControl'
import Button from '@/components/ui/Button'
import useRequestScope from '@/hooks/useRequestScope'
import BatchTrainingExports from './BatchTrainingExports'

export default function BatchTaskArtifacts({ client, batch, task }: { client: BatchClient; batch: string; task: BatchTask }) {
  const { t } = useTranslation()
  const scope = useMemo(() => ({ batch, task: task.task_id, client }), [batch, task.task_id, client])
  const begin = useRequestScope(scope)
  const [display, setDisplay] = useState<{ scope: typeof scope; text?: string; files?: BatchArtifactInfo[]; logs?: BatchLog; stream?: BatchLogStream; error?: string; busy?: boolean } | null>(null)
  const current = display?.scope === scope ? display : null
  async function run(operation: (signal: AbortSignal) => Promise<Partial<NonNullable<typeof display>>>) {
    const request = begin()
    if (!request.current()) { request.finish(); return }
    setDisplay((value) => ({ ...(value?.scope === scope ? value : {}), scope, busy: true, error: undefined }))
    try {
      const result = await operation(request.signal)
      if (request.current()) setDisplay((value) => ({ ...value, ...result, scope, busy: false }))
    } catch (failure) {
      if (request.current()) setDisplay((value) => ({ ...value, scope, busy: false, error: String(failure) }))
    } finally { request.finish() }
  }
  async function logs(stream: BatchLogStream, more = false) {
    await run(async (signal) => {
      const result = await client.taskLogs(batch, task.task_id, stream, more ? current?.logs?.next_offset : 0, signal)
      const text = (more ? current?.text ?? '' : '') + result.text
      return { text, logs: result, stream, files: undefined }
    })
  }
  async function download(file: BatchArtifactInfo) {
    await run(async (signal) => {
      const blob = await client.taskDownload(batch, task.task_id, file.artifact, signal)
      if (signal.aborted) return {}
      const url = URL.createObjectURL(blob)
      try {
        const link = document.createElement('a')
        link.href = url
        link.download = file.name
        document.body.appendChild(link)
        link.click()
        link.remove()
      } finally { URL.revokeObjectURL(url) }
      return {}
    })
  }
  return <section aria-label={t('batchWorkbench.outputs')} className="space-y-2">
    <div className="flex flex-wrap gap-2">
      {task.terminal && <>
        <Button size="sm" variant="outline" disabled={current?.busy} onClick={() => void run(async (signal) => ({ text: JSON.stringify(await client.taskResult(batch, task.task_id, signal), null, 2), logs: undefined, files: undefined }))}>{t('batchWorkbench.viewResult')}</Button>
        <Button size="sm" variant="outline" disabled={current?.busy} onClick={() => void run(async (signal) => ({ files: (await client.taskArtifacts(batch, task.task_id, signal)).items, text: undefined, logs: undefined }))}>{t('batchWorkbench.viewFiles')}</Button>
      </>}
      <Button size="sm" variant="outline" disabled={current?.busy} onClick={() => void logs('worker')}>{t('batchWorkbench.workerLog')}</Button>
      {['chain', 'distillation'].includes(task.kind) && <>
        <Button size="sm" variant="outline" disabled={current?.busy} onClick={() => void logs('stderr')}>{t('batchWorkbench.stderrLog')}</Button>
        <Button size="sm" variant="outline" disabled={current?.busy} onClick={() => void logs('stdout')}>{t('batchWorkbench.stdoutLog')}</Button>
      </>}
    </div>
    {current?.error && <p role="alert" className="text-destructive">{current.error}</p>}
    {current?.text !== undefined && <pre aria-label={t('batchWorkbench.outputContent')} className="max-h-80 overflow-auto whitespace-pre-wrap break-words rounded bg-muted p-3 text-xs">{current.text || t('batchWorkbench.emptyLog')}</pre>}
    {current?.logs?.has_more && <Button size="sm" disabled={current.busy} onClick={() => void logs(current.stream!, true)}>{t('batchWorkbench.moreLog')}</Button>}
    {current?.files && <div className="flex flex-wrap gap-2">{current.files.length ? current.files.map((file) => <Button key={file.artifact} size="sm" variant="outline" disabled={current.busy} onClick={() => void download(file)}>{file.name} ({file.bytes} B)</Button>) : <p className="text-muted-foreground">{t('batchWorkbench.noFiles')}</p>}</div>}
    {task.can_export_training && <BatchTrainingExports client={client} batch={batch} task={task.task_id} />}
  </section>
}
