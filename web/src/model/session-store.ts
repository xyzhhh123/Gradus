import type { ConnectionStatus, Frame } from '../protocol/frames'
import { reduce } from './reduce'
import { initialState, type SessionState } from './state'

/**
 * 唯一持有会话状态的地方。视图层通过快照 + 订阅读取，不直接改。
 */
export class SessionStore {
  private state: SessionState = initialState()
  private listeners = new Set<() => void>()

  apply(frame: Frame): void {
    this.commit(reduce(this.state, frame))
  }

  setConnection(connection: ConnectionStatus): void {
    if (this.state.connection === connection) return
    this.commit({ ...this.state, connection })
  }

  resolveConfirm(id: string): void {
    if (this.state.pendingConfirm?.id !== id) return
    this.commit({ ...this.state, pendingConfirm: null })
  }

  getSnapshot = (): SessionState => this.state

  subscribe = (listener: () => void): (() => void) => {
    this.listeners.add(listener)
    return () => {
      this.listeners.delete(listener)
    }
  }

  private commit(next: SessionState): void {
    if (next === this.state) return
    this.state = next
    for (const listener of [...this.listeners]) listener()
  }
}
