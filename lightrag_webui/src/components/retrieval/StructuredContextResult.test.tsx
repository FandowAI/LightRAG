/// <reference types="bun" />
import { describe, expect, test } from 'bun:test'
import { screen } from '@testing-library/react'
import { renderWithProviders } from '@/test/render'
import { StructuredContextResult } from './StructuredContextResult'

describe('structured retrieval results', () => {
  test('groups graph results, chunks and sources while preserving raw data', () => {
    renderWithProviders(<StructuredContextResult result={{
      status: 'success',
      message: 'ok',
      data: {
        entities: [{ entity_name: 'A3人群', entity_type: 'concept', description: '可转化人群<SEP>品牌资产' }],
        relationships: [{ src_id: 'A3人群', tgt_id: '品牌资产', description: '转化关系' }],
        chunks: [{ reference_id: '1', content: 'A3人群的原文片段' }],
        references: [{ reference_id: '1', file_path: '5A模型.pdf' }]
      },
      metadata: {}
    }} />)

    expect(screen.getByText('Entities: 1').textContent).toBe('Entities: 1')
    expect(screen.getByText('Relationships: 1').textContent).toBe('Relationships: 1')
    expect(screen.getByText('Text chunks: 1').textContent).toBe('Text chunks: 1')
    expect(screen.getByText('1. 5A模型.pdf').textContent).toContain('5A模型.pdf')
    expect(screen.getByText('Raw JSON').tagName).toBe('SUMMARY')
  })
})
