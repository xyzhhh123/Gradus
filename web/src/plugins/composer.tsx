import { useState } from 'react'
import type { Plugin } from '@deepseek-ai/cordis'
import type { RootProps } from '../render/types'

function ComposerBlock({ state, actions }: RootProps) {
  const [text, setText] = useState('')
  const running = state.status === 'running'

  const submit = () => {
    const value = text.trim()
    if (!value || running) return
    actions.prompt(value)
    setText('')
  }

  return (
    <form
      className="composer"
      onSubmit={event => {
        event.preventDefault()
        submit()
      }}
    >
      <textarea
        value={text}
        rows={2}
        placeholder="输入指令…"
        onChange={event => setText(event.target.value)}
      />
      <div className="composer-actions">
        {running ? (
          <button type="button" onClick={actions.cancel}>
            停止
          </button>
        ) : (
          <button type="submit" disabled={text.trim() === ''}>
            发送
          </button>
        )}
      </div>
    </form>
  )
}

export const composerPlugin: Plugin = {
  name: 'composer',
  inject: ['slots'],
  apply(ctx) {
    ctx.effect(() =>
      ctx.slots.inject('shell.blocks', () =>
        ctx.slots.register('shell.blocks', {
          id: 'composer',
          order: 30,
          component: ComposerBlock,
        }),
      ),
    )
  },
}
