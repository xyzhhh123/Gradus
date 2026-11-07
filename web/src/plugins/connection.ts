import type { Plugin } from '@deepseek-ai/cordis'
import type { ClientCommand, ConnectionStatus, Frame } from '../protocol/frames'

export interface ConnectionApi {
  send(command: ClientCommand): void
  onFrame(callback: (frame: Frame) => void): () => void
  onStatus(callback: (status: ConnectionStatus) => void): () => void
  getStatus(): ConnectionStatus
}

function resolveUrl(): string {
  const configured = import.meta.env.VITE_GRADUS_WS as string | undefined
  const scheme = location.protocol === 'https:' ? 'wss' : 'ws'
  const url = new URL(configured ?? `${scheme}://${location.host}/ws`, location.href)

  // gradus-serve 启动时打印的地址带 token；没有它 `/ws` 会直接 4401。
  const token = new URLSearchParams(location.search).get('token')
  if (token && !url.searchParams.has('token')) url.searchParams.set('token', token)

  return url.toString()
}

const MAX_BACKOFF = 5000

class SocketConnection implements ConnectionApi {
  private socket: WebSocket | undefined
  private frameListeners = new Set<(frame: Frame) => void>()
  private statusListeners = new Set<(status: ConnectionStatus) => void>()
  private status: ConnectionStatus = 'connecting'
  private retry: ReturnType<typeof setTimeout> | undefined
  private backoff = 500
  private stopped = false

  constructor(private readonly url: string) {}

  start(): void {
    this.stopped = false
    this.open()
  }

  stop(): void {
    this.stopped = true
    if (this.retry !== undefined) clearTimeout(this.retry)
    this.retry = undefined
    this.socket?.close()
    this.socket = undefined
    this.setStatus('closed')
  }

  send(command: ClientCommand): void {
    if (this.socket?.readyState !== WebSocket.OPEN) {
      console.warn('[gradus] connection is not open, dropped command', command)
      return
    }
    this.socket.send(JSON.stringify(command))
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

  private open(): void {
    if (this.stopped) return
    this.setStatus('connecting')

    const socket = new WebSocket(this.url)
    this.socket = socket

    socket.onopen = () => {
      this.backoff = 500
      this.setStatus('open')
    }

    socket.onmessage = message => {
      if (typeof message.data !== 'string') return
      let frame: Frame
      try {
        frame = JSON.parse(message.data) as Frame
      } catch {
        console.warn('[gradus] malformed frame', message.data)
        return
      }
      if (typeof frame.seq !== 'number' || typeof frame.type !== 'string') return
      for (const listener of [...this.frameListeners]) listener(frame)
    }

    socket.onclose = () => {
      this.socket = undefined
      this.setStatus('closed')
      this.scheduleReconnect()
    }

    socket.onerror = () => {
      socket.close()
    }
  }

  private scheduleReconnect(): void {
    if (this.stopped || this.retry !== undefined) return
    this.retry = setTimeout(() => {
      this.retry = undefined
      this.open()
    }, this.backoff)
    this.backoff = Math.min(this.backoff * 2, MAX_BACKOFF)
  }

  private setStatus(status: ConnectionStatus): void {
    if (this.status === status) return
    this.status = status
    for (const listener of [...this.statusListeners]) listener(status)
  }
}

export const connectionPlugin: Plugin = {
  name: 'connection',
  apply(ctx) {
    const connection = new SocketConnection(resolveUrl())
    ctx.provide('connection', connection)
    ctx.effect(() => {
      connection.start()
      return () => connection.stop()
    })
  },
}
