import { useDataWorkflow } from './useDataWorkflow'
import DataWorkflowProgress from './DataWorkflowProgress'
import DataWorkflowSettingsDialog from './DataWorkflowSettingsDialog'
import DistillationStartButton from './DistillationStartButton'

export default function BatchDistillation({ batchId, available }: { batchId: string; available: boolean }) {
  const workflow = useDataWorkflow(batchId)
  return <div className="space-y-4">
    <DataWorkflowSettingsDialog disabled={workflow.submitting} onSaved={workflow.refreshConfiguration} />
    <DistillationStartButton workflow={workflow} available={available} />
    {workflow.job && <DataWorkflowProgress key={workflow.job.job_id} job={workflow.job} />}
  </div>
}
