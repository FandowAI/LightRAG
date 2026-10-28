import { useTranslation } from 'react-i18next'
import Button from '@/components/ui/Button'
import { useDataWorkflow } from './useDataWorkflow'
import DataWorkflowProgress from './DataWorkflowProgress'
import DataWorkflowSettingsDialog from './DataWorkflowSettingsDialog'

export default function BatchDistillation({
  batchId,
  available
}: {
  batchId: string
  available: boolean
}) {
  const { t } = useTranslation()
  const workflow = useDataWorkflow(batchId)
  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-center gap-3">
        <Button
          type="button"
          disabled={
            !available ||
            workflow.restoring ||
            workflow.submitting ||
            !workflow.configuration?.ready ||
            (!!workflow.job && !workflow.job.can_start && !workflow.canRetry)
          }
          onClick={() => void workflow.submit()}
        >
          {t(
            workflow.submitting
              ? 'distillation.submitting'
              : workflow.canRetry
                ? 'distillation.retry'
                : workflow.job?.can_start
                  ? 'distillation.start_prepared'
                  : workflow.job
                    ? 'distillation.submitted'
                    : 'batch.submit'
          )}
        </Button>
        <DataWorkflowSettingsDialog
          disabled={workflow.submitting}
          onSaved={workflow.refreshConfiguration}
        />
      </div>
      {workflow.canRetry && (
        <p className="text-muted-foreground text-xs">{t('distillation.retry_hint')}</p>
      )}
      {workflow.restoring && (
        <p role="status" className="text-muted-foreground text-sm">
          {t('distillation.restoring')}
        </p>
      )}
      {(!workflow.configuration || !workflow.configuration.ready) && (
        <p className="text-muted-foreground text-sm">{t('distillation.configuration_pending')}</p>
      )}
      {workflow.error && (
        <p role="alert" className="text-destructive text-sm">
          {t(`distillation.errors.${workflow.error}`)}
        </p>
      )}
      {workflow.job && <DataWorkflowProgress key={workflow.job.job_id} job={workflow.job} />}
    </div>
  )
}
