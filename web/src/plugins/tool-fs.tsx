import type { Plugin } from '@deepseek-ai/cordis'
import type { ToolCard } from '../model/state'

function pathOf(args: unknown): string {
  if (args && typeof args === 'object' && 'path' in args) {
    const value = (args as { path?: unknown }).path
    if (typeof value === 'string') return value
  }
  return ''
}

function FileCard({ card }: { card: ToolCard }) {
  return (
    <div className={`card card-${card.status}`}>
      <div className="card-title">
        {card.name}
        <span className="card-meta">{pathOf(card.args)}</span>
      </div>
      <pre className="card-output">{card.content ?? card.partial ?? '…'}</pre>
    </div>
  )
}

export const toolFsPlugin: Plugin = {
  name: 'tool-fs',
  inject: ['slots'],
  apply(ctx) {
    for (const name of ['read_file', 'write_file']) {
      ctx.effect(() =>
        ctx.slots.inject('tool.call.view', () =>
          ctx.slots.register('tool.call.view', { id: `fs-${name}`, key: name, component: FileCard }),
        ),
      )
    }
  },
}
