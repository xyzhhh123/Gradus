import type { Plugin } from '@deepseek-ai/cordis'
import type { ToolCard } from '../model/state'
import { ToolCallCard } from '../render/tool-call-card'

function BashCard({ card }: { card: ToolCard }) {
  return <ToolCallCard card={card} />
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
