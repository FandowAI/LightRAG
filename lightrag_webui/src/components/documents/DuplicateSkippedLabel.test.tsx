import { expect, test } from 'bun:test'
import { screen } from '@testing-library/react'
import { renderWithProviders } from '@/test/render'
import DuplicateSkippedLabel from './DuplicateSkippedLabel'

test('duplicates render as a neutral skip instead of a failure', () => {
  renderWithProviders(<DuplicateSkippedLabel />)
  expect(screen.getByText('Duplicate skipped').className).toContain('text-slate-500')
  expect(screen.queryAllByText('Failed')).toHaveLength(0)
})
