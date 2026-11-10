import '@/migrations/runSettingsStorageSplit'
import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import './index.css'
import './i18n'
import StandaloneSettings from '@/features/batchSettings/StandaloneSettings'

createRoot(document.getElementById('root')!).render(<StrictMode><StandaloneSettings /></StrictMode>)
