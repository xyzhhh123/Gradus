import type { Plugin } from '@deepseek-ai/cordis'
import type { ToolCard } from '../model/state'

function BashCard({ card }: { card: ToolCard }) {
  const output = card.content ?? card.partial
  return (
    <div className={`card card-${card.status}`}>
      <div className="card-title">
        bash
        {card.durationMs !== undefined && <span className="card-meta">{card.durationMs}ms</span>}
      </div>
      <pre className="card-output">{output || '…'}</pre>
    </div>
  )
}

export const toolBashPlugin: Plugin = {
  name: 'tool-bash',
  inject: ['slots'],
  apply(ctx) {
    ctx.effect(() =>
      ctx.slots.inject('tool.call.view', () =>
        ctx.slots.register('tool.call.view', { id: 'bash', key: 'bash', component: BashCard }),
      ),
    )
  },
}
