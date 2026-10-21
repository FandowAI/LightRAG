import { lightragAxios } from './lightrag'

export type PromptValue = string | string[]

export type PromptField = {
  key: string
  project_default: PromptValue
  zh_default: PromptValue
  default: PromptValue
  active: PromptValue
  draft: PromptValue
  customized: boolean
}

export type PromptGroup = {
  id: string
  fields: PromptField[]
  state: 'active' | 'conditional' | 'inactive'
  reason: string
  has_draft: boolean
}

export type PromptSnapshot = {
  revision: number
  write_enabled: boolean
  groups: PromptGroup[]
}

export type PromptHistoryEntry = {
  revision: number
  at: string
  action: 'draft' | 'publish' | 'reset' | 'reset_english'
  actor: string
  fields: Record<string, PromptValue>
}

const root = '/prompt-admin'

export const getPromptSnapshot = async (): Promise<PromptSnapshot> =>
  (await lightragAxios.get<PromptSnapshot>(root)).data

export const getPromptHistory = async (group: string): Promise<PromptHistoryEntry[]> =>
  (await lightragAxios.get<PromptHistoryEntry[]>(`${root}/${group}/history`)).data

export const savePromptDraft = async (
  group: string,
  revision: number,
  fields: Record<string, PromptValue>
): Promise<PromptSnapshot> =>
  (await lightragAxios.put<PromptSnapshot>(`${root}/${group}/draft`, { revision, fields })).data

export const publishPromptDraft = async (group: string, revision: number): Promise<PromptSnapshot> =>
  (await lightragAxios.post<PromptSnapshot>(`${root}/${group}/publish`, { revision })).data

export const resetPromptGroup = async (group: string, revision: number, language: 'zh' | 'en' = 'zh'): Promise<PromptSnapshot> =>
  (await lightragAxios.post<PromptSnapshot>(`${root}/${group}/reset`, { revision, language })).data

