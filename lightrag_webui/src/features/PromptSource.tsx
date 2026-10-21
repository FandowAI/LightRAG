/** Show prompt text exactly as stored, including Markdown markers and placeholders. */
export default function PromptSource({ content }: { content: string }) {
  return (
    <pre className="max-w-full overflow-x-auto whitespace-pre-wrap break-words rounded-md border bg-muted/40 p-3 font-mono text-xs leading-relaxed">
      {content}
    </pre>
  )
}
