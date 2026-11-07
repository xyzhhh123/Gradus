import type { Plugin } from '@deepseek-ai/cordis'
import type { RootProps } from '../render/types'

function Shell(props: RootProps) {
  const { state, renderSlot } = props
  return (
    <div className="app">
      <header className="app-header">
        <span className="brand">Gradus</span>
        <span className={`pill pill-${state.status}`}>{state.status}</span>
      </header>
      <main className="app-body">{renderSlot('shell.blocks', props)}</main>
    </div>
  )
}

export const layoutPlugin: Plugin = {
  name: 'layout',
  inject: ['slots'],
  apply(ctx) {
    ctx.effect(() => ctx.slots.declare('app.root', 'single'))
    ctx.effect(() => ctx.slots.declare('shell.blocks', 'list'))
    ctx.effect(() => ctx.slots.register('app.root', { id: 'shell', component: Shell }))
  },
}
