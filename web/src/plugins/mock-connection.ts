import type { Plugin } from '@deepseek-ai/cordis'
import type { ClientCommand, ConnectionStatus, Frame } from '../protocol/frames'
import type { ConnectionApi } from './connection'

/**
 * 不连后端也能看界面：`?mock=1`。
 * 它只实现 ConnectionApi，因此 sessions / 视图层完全不知道自己在跑假数据。
 */
class MockConnection implements ConnectionApi {
  private frameListeners = new Set<(frame: Frame) => void>()
  private statusListeners = new Set<(status: ConnectionStatus) => void>()
  private status: ConnectionStatus = 'connecting'
  private seq = 0
  private userSeq = 0
  private timers = new Set<ReturnType<typeof setTimeout>>()

  start(): void {
    this.at(50, () => {
      this.setStatus('open')
      this.emit('session/snapshot', {
        protocolVersion: 1,
        capabilities: ['mock'],
        messages: [{ id: 'm0', role: 'assistant', text: '这是 mock 连接，不连后端。' }],
        status: 'idle',
        tools: ['bash', 'read_file', 'write_file'],
        sandbox: { backend: 'restricted', warning: 'network NOT isolated' },
      })
    })
  }

  stop(): void {
    for (const timer of this.timers) clearTimeout(timer)
    this.timers.clear()
    this.setStatus('closed')
  }

  send(command: ClientCommand): void {
    if (command.type === 'session/prompt') {
      this.emit('session/message', {
        id: `u-${(this.userSeq += 1)}`,
        role: 'user',
        text: command.text,
      })
      this.run(command.text)
    }
    if (command.type === 'session/cancel') {
      this.emit('agent/event', { type: 'agent/error', kind: 'aborted', message: '已取消' })
    }
    if (command.type === 'interaction/answer') {
      this.emit('session/status', { status: 'idle' })
    }
  }

  onFrame(callback: (frame: Frame) => void): () => void {
    this.frameListeners.add(callback)
    return () => this.frameListeners.delete(callback)
  }

  onStatus(callback: (status: ConnectionStatus) => void): () => void {
    this.statusListeners.add(callback)
    callback(this.status)
    return () => this.statusListeners.delete(callback)
  }

  getStatus(): ConnectionStatus {
    return this.status
  }

  private run(text: string): void {
    this.at(0, () => this.emit('agent/event', { type: 'agent/start' }))
    this.at(80, () => this.emit('agent/event', { type: 'turn/start', turn: 0 }))
    this.at(200, () =>
      this.emit('agent/event', { type: 'assistant/delta', text: `收到：${text}\n我先看一下目录。` }),
    )
    this.at(500, () =>
      this.emit('agent/event', {
        type: 'tool/start',
        call: { id: 'call-1', name: 'bash', args: { command: 'ls' } },
      }),
    )
    this.at(800, () =>
      this.emit('agent/event', { type: 'tool/update', id: 'call-1', partial: 'README.md\n' }),
    )
    this.at(1100, () =>
      this.emit('agent/event', {
        type: 'tool/end',
        id: 'call-1',
        result: { content: 'README.md\nsrc\npackage.json', isError: false },
        durationMs: 600,
      }),
    )
    this.at(1300, () =>
      this.emit('agent/event', { type: 'assistant/delta', text: '\n仓库里是标准结构。' }),
    )
    this.at(1500, () =>
      this.emit('agent/event', {
        type: 'turn/end',
        usage: { inputTokens: 120, outputTokens: 48 },
      }),
    )
    this.at(1600, () =>
      this.emit('agent/event', {
        type: 'agent/done',
        usage: { inputTokens: 120, outputTokens: 48 },
        turns: 1,
      }),
    )
  }

  private emit(type: string, payload: unknown): void {
    const frame: Frame = { seq: (this.seq += 1), type, payload }
    for (const listener of [...this.frameListeners]) listener(frame)
  }

  private at(delay: number, action: () => void): void {
    const timer = setTimeout(() => {
      this.timers.delete(timer)
      action()
    }, delay)
    this.timers.add(timer)
  }

  private setStatus(status: ConnectionStatus): void {
    if (this.status === status) return
    this.status = status
    for (const listener of [...this.statusListeners]) listener(status)
  }
}

export const mockConnectionPlugin: Plugin = {
  name: 'connection-mock',
  apply(ctx) {
    const connection = new MockConnection()
    ctx.provide('connection', connection)
    ctx.effect(() => {
      connection.start()
      return () => connection.stop()
    })
  },
}
