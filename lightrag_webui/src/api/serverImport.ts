import { lightragAxios, type DocActionResponse } from './lightrag'

const base = '/documents/server-import'
export type ServerFile = { path: string; relative_path: string; size: number; version: string }
export type ServerDirectory = {
  path: string
  parent: string | null
  directories: { name: string; path: string }[]
}
export type ServerPreview = {
  path: string
  files: ServerFile[]
  skipped: number
  excluded: { path: string; reason: string }[]
}
export type ServerImportConfiguration = {
  enabled: boolean
  roots: string[]
  limits?: { max_files: number; max_entries: number }
}
export type ServerImportLimitFailure = { kind: 'files' | 'entries'; limit: number }

export function getServerImportLimitFailure(failure: unknown): ServerImportLimitFailure | null {
  const detail = (failure as { data?: { detail?: unknown } } | null)?.data?.detail
  if (typeof detail === 'string') {
    if (detail.startsWith('Too many files;')) return { kind: 'files', limit: 5000 }
    if (detail.startsWith('Too many entries;') || detail.startsWith('Too many directory entries;'))
      return { kind: 'entries', limit: 20000 }
  }
  if (!detail || typeof detail !== 'object') return null
  const value = detail as Record<string, unknown>
  const kind =
    value.code === 'server_import_files_limit'
      ? 'files'
      : value.code === 'server_import_entries_limit'
        ? 'entries'
        : null
  return kind && typeof value.limit === 'number' && Number.isFinite(value.limit) && value.limit > 0
    ? { kind, limit: value.limit }
    : null
}

export const getServerImportConfig = async (
  signal?: AbortSignal
): Promise<ServerImportConfiguration> =>
  (await lightragAxios.get(`${base}/config`, { signal, timeout: 10000 })).data

export const browseServerDirectory = async (
  path: string,
  signal?: AbortSignal
): Promise<ServerDirectory> =>
  (await lightragAxios.post(`${base}/browse`, { path }, { signal, timeout: 30000 })).data

export const previewServerDirectory = async (
  path: string,
  recursive: boolean,
  signal?: AbortSignal,
  extensions?: string[]
): Promise<ServerPreview> =>
  (
    await lightragAxios.post(
      `${base}/preview`,
      { path, recursive, ...(extensions ? { extensions } : {}) },
      { signal, timeout: 120000 }
    )
  ).data

export const importServerFile = async (
  file: ServerFile,
  signal?: AbortSignal
): Promise<DocActionResponse> =>
  (
    await lightragAxios.post(
      `${base}/file`,
      { path: file.path, version: file.version },
      { signal, timeout: 0 }
    )
  ).data
