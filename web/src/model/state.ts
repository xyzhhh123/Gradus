import type {
  ConnectionStatus,
  MessageWire,
  SandboxInfo,
  SessionStatus,
  UsageWire,
} from '../protocol/frames'

export interface ToolCard {
  id: string
  name: string
  args: unknown
  status: 'running' | 'done' | 'error'
  partial: string
  content?: string
  durationMs?: number
}

export interface TurnState {
  index: number
  assistant: string
  tools: ToolCard[]
  usage?: UsageWire
}

export interface PendingConfirm {
  id: string
  question: string
}

export interface SessionState {
  connection: ConnectionStatus
  status: SessionStatus
  /** 历史消息（来自快照）。实时内容在 `turns` 里。 */
  messages: MessageWire[]
  turns: TurnState[]
  tools: string[]
  sandbox: SandboxInfo
  pendingConfirm: PendingConfirm | null
  error: { kind: string; message: string } | null
}

export function initialState(): SessionState {
  return {
    connection: 'connecting',
    status: 'idle',
    messages: [],
    turns: [],
    tools: [],
    sandbox: { backend: 'unknown', warning: null },
    pendingConfirm: null,
    error: null,
  }
}
