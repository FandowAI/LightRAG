import { describe, expect, test } from 'bun:test'
import { render, screen } from '@testing-library/react'
import PromptSource from './PromptSource'

describe('prompt source view', () => {
  test('shows Markdown syntax, separators, and placeholders literally', () => {
    const source = '# Role\n\n**Important**\n======\nUse <entity_name> and {input_text}.'
    const { container } = render(<PromptSource content={source} />)

    expect(container.querySelector('pre')?.textContent).toBe(source)
    expect(screen.queryByRole('heading', { name: 'Role' })).toBeNull()
    expect(container.querySelector('strong')).toBeNull()
  })

  test('shows each JSON example as its original text', () => {
    const source = '{\n  "entities": ["<entity_name>"]\n}'
    const { container } = render(<PromptSource content={source} />)
    expect(container.querySelector('pre')?.textContent).toBe(source)
  })
})
