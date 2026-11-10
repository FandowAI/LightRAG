export const sections = ['rag', 'retrieval', 'recipe', 'prompts', 'bridge'] as const
export type Section = (typeof sections)[number]
