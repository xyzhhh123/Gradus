import { useSyncExternalStore } from 'react'
import { createRoot } from 'react-dom/client'
import type { Plugin } from '@deepseek-ai/cordis'
import { CellView } from '../render/slot'
import type { RootProps, SessionActions } from '../render/types'

/**
 * 唯一把 ctx 与 React 绑在一起的地方。
 * 业务组件拿不到 ctx，只拿到推导好的 props。
 */
export const rendererPlugin: Plugin = {
  name: 'renderer',
  inject: ['slots', 'sessions'],
  apply(ctx) {
    const container = document.getElementById('root')
    if (!container) throw new Error('#root not found')

    const actions: SessionActions = {
      prompt: text => ctx.sessions.prompt(text),
      cancel: () => ctx.sessions.cancel(),
      answer: (id, ok) => ctx.sessions.answer(id, ok),
      createSession: workspace => ctx.sessions.createSession(workspace),
      openSession: id => ctx.sessions.openSession(id),
      renameSession: (id, title) => ctx.sessions.renameSession(id, title),
      deleteSession: id => ctx.sessions.deleteSession(id),
      browseWorkspace: path => ctx.sessions.browseWorkspace(path),
      selectModel: id => ctx.sessions.selectModel(id),
    }

    const renderSlot = (name: string, props: unknown, entryKey?: string) => {
      const cells =
        entryKey === undefined
          ? ctx.slots.get(name)
          : ctx.slots.get(name).filter(cell => cell.key === entryKey)
      if (cells.length === 0) return null
      return (
        <>
          {cells.map(cell => (
            <CellView key={cell.id} cell={cell} props={props} />
          ))}
        </>
      )
    }

    function App() {
      const state = useSyncExternalStore(ctx.sessions.subscribe, ctx.sessions.getSnapshot)
      useSyncExternalStore(ctx.slots.subscribe, ctx.slots.getVersion)

      const props: RootProps = { state, actions, renderSlot }

      return (
        <>
          {ctx.slots.get('app.root').map(cell => (
            <CellView key={cell.id} cell={cell} props={props} />
          ))}
        </>
      )
    }

    const root = createRoot(container)
    ctx.effect(() => {
      root.render(<App />)
      return () => root.unmount()
    })
  },
}
