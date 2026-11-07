import type { Plugin } from '@deepseek-ai/cordis'
import { SessionStore } from '../model/session-store'
import type { SessionState } from '../model/state'

export interface SessionsApi {
  getSnapshot(): SessionState
  subscribe(listener: () => void): () => void
  prompt(text: string): void
  cancel(): void
  answer(id: string, ok: boolean): void
  createSession(workspace?: string): void
  openSession(id: string): void
  renameSession(id: string, title: string): void
  deleteSession(id: string): void
  browseWorkspace(path: string): void
  pickWorkspace(path: string): void
  selectModel(id: string): void
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
      createSession: workspace =>
        ctx.connection.send(workspace ? { type: 'session/create', workspace } : { type: 'session/create' }),
      openSession: id => ctx.connection.send({ type: 'session/open', id }),
      renameSession: (id, title) => ctx.connection.send({ type: 'session/rename', id, title }),
      deleteSession: id => ctx.connection.send({ type: 'session/delete', id }),
      browseWorkspace: path => ctx.connection.send({ type: 'workspace/browse', path }),
      pickWorkspace: path => ctx.connection.send({ type: 'workspace/pick', path }),
      selectModel: id => ctx.connection.send({ type: 'model/select', id }),
    }

    ctx.provide('sessions', sessions)
  },
}
