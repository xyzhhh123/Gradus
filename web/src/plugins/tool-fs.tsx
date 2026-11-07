import type { Plugin } from '@deepseek-ai/cordis'
import type { ToolCard } from '../model/state'
import { ToolCallCard } from '../render/tool-call-card'

function FileCard({ card }: { card: ToolCard }) {
  return <ToolCallCard card={card} />
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
