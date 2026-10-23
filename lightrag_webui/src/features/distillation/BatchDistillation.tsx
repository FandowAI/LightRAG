import { useTranslation } from 'react-i18next'
import Button from '@/components/ui/Button'
import { useDataWorkflow } from './useDataWorkflow'
import DataWorkflowProgress from './DataWorkflowProgress'

export default function BatchDistillation({ batchId, available }: { batchId: string; available: boolean }) {
  const { t } = useTranslation()
  const workflow = useDataWorkflow(batchId)
  return <div className="space-y-4">
    <Button type="button" disabled={!available || workflow.restoring || workflow.submitting || !workflow.configuration?.ready || (!!workflow.job && !workflow.job.can_start)} onClick={() => void workflow.submit()}>
      {t(workflow.submitting ? 'distillation.submitting' : workflow.job?.can_start ? 'distillation.start_prepared' : workflow.job ? 'distillation.submitted' : 'batch.submit')}
    </Button>
    {workflow.restoring && <p role="status" className="text-sm text-muted-foreground">{t('distillation.restoring')}</p>}
    {(!workflow.configuration || !workflow.configuration.ready) && <p className="text-sm text-muted-foreground">{t('distillation.configuration_pending')}</p>}
    {workflow.error && <p role="alert" className="text-sm text-destructive">{t(`distillation.errors.${workflow.error}`)}</p>}
    {workflow.job && <DataWorkflowProgress key={workflow.job.job_id} job={workflow.job} />}
  </div>
}
