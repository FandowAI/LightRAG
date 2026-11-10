import { useTranslation } from 'react-i18next'
import Button from '@/components/ui/Button'
import { useDataWorkflow } from './useDataWorkflow'

export default function DistillationStartButton({ workflow, available }: { workflow: ReturnType<typeof useDataWorkflow>; available: boolean }) {
  const { t } = useTranslation()
  return <div className="space-y-3">
    <Button type="button" disabled={!available || workflow.restoring || workflow.submitting || workflow.monitoringOther || !workflow.configuration?.ready || (!!workflow.sourceJob && !workflow.sourceJob.can_start && !workflow.canRetry)} onClick={() => void workflow.submit()}>
      {t(workflow.submitting ? 'distillation.submitting' : workflow.canRetry ? 'distillation.retry' : workflow.sourceJob?.can_start ? 'distillation.start_prepared' : workflow.sourceJob ? 'distillation.submitted' : 'batch.submit')}
    </Button>
    {workflow.canRetry && <p className="text-xs text-muted-foreground">{t('distillation.retry_hint')}</p>}
    {workflow.restoring && <p role="status" className="text-sm text-muted-foreground">{t('distillation.restoring')}</p>}
    {!workflow.configuration?.ready && <p className="text-sm text-muted-foreground">{t('distillation.configuration_pending')}</p>}
    {workflow.error && <p role="alert" className="text-sm text-destructive">{t(`distillation.errors.${workflow.error}`)}</p>}
  </div>
}
