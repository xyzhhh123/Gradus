/**
 * Web 前后端之间唯一的契约。
 *
 * 修改这里的任何字段都要同步改 `gradus/host/wire.py`。
 * `capabilities` 用来让前端在旧后端上降级，而不是靠版本号硬拒。
 */

export const PROTOCOL_VERSION = 1

export interface Frame<T = unknown> {
  /** 单调递增，从 1 开始；快照固定为 0。 */
  seq: number
  type: string
  payload: T
}

export type ConnectionStatus = 'connecting' | 'open' | 'closed'

export type SessionStatus = 'idle' | 'running' | 'error'

export interface ToolCallWire {
  id: string
  name: string
  args: unknown
}

export interface ToolResultWire {
  content: string
  isError: boolean
}

export interface UsageWire {
  inputTokens: number
  outputTokens: number
}

export interface MessageWire {
  id: string
  role: 'user' | 'assistant'
  text: string
}

/** `agent/event` 的 payload，与 `gradus/agent/types.py` 的 AgentEvent 一一对应。 */
export type WireEvent =
  | { type: 'agent/start' }
  | { type: 'turn/start'; turn: number }
  | { type: 'assistant/delta'; text: string }
  | { type: 'tool/start'; call: ToolCallWire }
  | { type: 'tool/update'; id: string; partial: string }
  | { type: 'tool/end'; id: string; result: ToolResultWire; durationMs?: number }
  | { type: 'turn/end'; usage: UsageWire }
  | { type: 'agent/done'; usage: UsageWire; turns: number }
  | {
      type: 'agent/error'
      kind: 'provider' | 'aborted' | 'max_turns' | 'config'
      message: string
    }

export interface SandboxInfo {
  backend: string
  warning: string | null
}

export interface Snapshot {
  protocolVersion: number
  capabilities: string[]
  messages: MessageWire[]
  status: SessionStatus
  tools: string[]
  sandbox: SandboxInfo
}

export interface ConfirmRequest {
  id: string
  question: string
}

export interface StatusPayload {
  status: SessionStatus
  error?: { kind: string; message: string }
}

export type ClientCommand =
  | { type: 'session/prompt'; text: string }
  | { type: 'session/cancel' }
  | { type: 'interaction/answer'; id: string; ok: boolean }
