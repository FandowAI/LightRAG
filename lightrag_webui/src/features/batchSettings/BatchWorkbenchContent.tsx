import type { ReactNode } from 'react'
import { useTranslation } from 'react-i18next'
import type { BatchClient, BatchRecord } from '@/api/batchControl'
import { isBatchOnlyDeployment } from '@/lib/runtimeConfig'

export default function BatchWorkbenchContent({ batch, children }: {
  batch: BatchRecord | null
  client: BatchClient
  children: ReactNode
}) {
  const { t } = useTranslation()
  if (isBatchOnlyDeployment() && !batch) {
    return <p role="status" className="p-6 text-muted-foreground">{t('batchWorkbench.chooseHint')}</p>
  }
  return children
}
