import type { Plugin } from '@deepseek-ai/cordis'
import type { RootProps } from '../render/types'

function StatusBar({ state }: RootProps) {
  return (
    <div className="status-bar">
      <span className={`dot dot-${state.connection}`} />
      <span>{state.connection}</span>
      <span className="sep">·</span>
      <span>{state.sandbox.backend}</span>
      <span className="sep">·</span>
      <span>{state.tools.length} tools</span>
      {state.sandbox.warning && <span className="warn">{state.sandbox.warning}</span>}
    </div>
  )
}

export const statusBarPlugin: Plugin = {
  name: 'status-bar',
  inject: ['slots'],
  apply(ctx) {
    ctx.effect(() =>
      ctx.slots.inject('shell.blocks', () =>
        ctx.slots.register('shell.blocks', {
          id: 'status-bar',
          order: 0,
          component: StatusBar,
        }),
      ),
    )
  },
}
