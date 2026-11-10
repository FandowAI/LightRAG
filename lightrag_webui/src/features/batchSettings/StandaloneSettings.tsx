import { useMemo, useState } from 'react'
import { useTranslation } from 'react-i18next'
import ThemeProvider from '@/components/ThemeProvider'
import Input from '@/components/ui/Input'
import Button from '@/components/ui/Button'
import { createBatchClient } from '@/api/batchControl'
import BatchSettingsView from './BatchSettingsView'

export default function StandaloneSettings() {
  const { t, i18n } = useTranslation()
  const [key, setKey] = useState('')
  const [connection, setConnection] = useState<{ key: string; epoch: number } | null>(null)
  const client = useMemo(() => createBatchClient(connection?.key ?? null), [connection])
  return (
    <ThemeProvider>
      <main className="bg-background text-foreground min-h-screen">
        <header className="flex flex-wrap items-center gap-3 border-b p-4">
          <strong>LightRAG</strong>
          <form
            className="flex flex-1 flex-wrap gap-2"
            onSubmit={(event) => {
              event.preventDefault()
              setConnection((current) => ({ key, epoch: (current?.epoch ?? 0) + 1 }))
            }}
          >
            <Input
              type="password"
              autoComplete="off"
              aria-label={t('batchSettings.apiKey')}
              placeholder={t('batchSettings.apiKey')}
              value={key}
              onChange={(event) => setKey(event.target.value)}
            />
            <Button type="submit">{t('batchSettings.connect')}</Button>
          </form>
          <select
            aria-label={t('batchSettings.language')}
            className="bg-background rounded-md border p-2"
            value={i18n.language.startsWith('zh') ? 'zh' : 'en'}
            onChange={(event) => void i18n.changeLanguage(event.target.value)}
          >
            <option value="zh">中文</option>
            <option value="en">English</option>
          </select>
        </header>
        {connection ? (
          <BatchSettingsView key={connection.epoch} client={client} />
        ) : (
          <p className="mx-auto max-w-6xl p-6 text-sm">{t('batchSettings.connectHint')}</p>
        )}
      </main>
    </ThemeProvider>
  )
}
