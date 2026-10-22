import { useTranslation } from 'react-i18next'

export default function DuplicateSkippedLabel() {
  const { t } = useTranslation()
  return <span className="text-slate-500">{t('documentPanel.documentManager.status.skipped')}</span>
}
