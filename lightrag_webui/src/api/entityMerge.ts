import { lightragAxios } from './lightrag'

export type EntityMergeSettings = {
  enabled: boolean
  similarity_threshold: number
  margin_threshold: number
  top_k: number
}

export type EntityMergeSnapshot = {
  workspace: string
  revision: number
  settings: EntityMergeSettings
  embedding_model: string | null
  embedding_dim: number
  write_enabled: boolean
  identity_verification?: {
    enabled: boolean
    uncertain_policy: 'keep_separate'
    registry: { version: string | null; sha256: string; entries: number } | null
  }
}

export type EntityMergePreview = {
  existing_exact_name: boolean
  candidates: { name: string; type: string; score: number }[]
  decision: 'exact_name' | 'merge' | 'create' | 'candidate'
  target: string | null
}

export const getEntityMergeSettings = async (signal?: AbortSignal): Promise<EntityMergeSnapshot> =>
  (await lightragAxios.get<EntityMergeSnapshot>('/entity-merge', { signal })).data

export const saveEntityMergeSettings = async (
  revision: number,
  settings: EntityMergeSettings,
  signal?: AbortSignal
): Promise<EntityMergeSnapshot> =>
  (await lightragAxios.put<EntityMergeSnapshot>('/entity-merge', { revision, settings }, { signal })).data

export const previewEntityMerge = async (
  name: string,
  entity_type: string,
  signal?: AbortSignal
): Promise<EntityMergePreview> =>
  (await lightragAxios.post<EntityMergePreview>('/entity-merge/preview', { name, entity_type }, { signal })).data
