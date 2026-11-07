import type { Plugin } from '@deepseek-ai/cordis'
import { SessionStore } from '../model/session-store'
import type { SessionState } from '../model/state'

export interface SessionsApi {
  getSnapshot(): SessionState
  subscribe(listener: () => void): () => void
  prompt(text: string): void
  cancel(): void
  answer(id: string, ok: boolean): void
}

export const sessionsPlugin: Plugin = {
  name: 'sessions',
  inject: ['connection'],
  apply(ctx) {
    const store = new SessionStore()

    ctx.effect(() => ctx.connection.onFrame(frame => store.apply(frame)))
    ctx.effect(() => ctx.connection.onStatus(status => store.setConnection(status)))

    const sessions: SessionsApi = {
      getSnapshot: store.getSnapshot,
      subscribe: store.subscribe,
      prompt: text => ctx.connection.send({ type: 'session/prompt', text }),
      cancel: () => ctx.connection.send({ type: 'session/cancel' }),
      answer: (id, ok) => {
        store.resolveConfirm(id)
        ctx.connection.send({ type: 'interaction/answer', id, ok })
      },
    }

    ctx.provide('sessions', sessions)
  },
}
