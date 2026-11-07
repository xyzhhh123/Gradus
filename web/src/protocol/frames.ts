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

/** 当前模型；`name` 是配置里给候选起的显示名。 */
export interface ModelSummary {
  name: string
  provider: string
  model: string
}

export interface ModelInfo extends ModelSummary {
  id: string
}

export interface ModelStatePayload {
  activeId: string | null
  models: ModelInfo[]
  active: ModelSummary | null
}

/** 内置服务商快捷名，来自 `gradus/ai/services.py`。 */
export interface ServiceInfo {
  name: string
  provider: string
  baseUrl: string
}

/** 设置页里的一行候选；`hasKey` 只表示有没有独立 key，key 本身不下发。 */
export interface ProviderEntry {
  id: string
  name: string
  provider: string
  model: string
  baseUrl: string
  hasKey: boolean
}

export interface SettingsStatePayload {
  /** 候选表所在的配置文件绝对路径。 */
  path: string
  services: ServiceInfo[]
  models: ProviderEntry[]
  current: ModelSummary
}

/** 保存候选表时提交的一行；`service` 为空时用 `provider` + `base_url`。 */
export interface ModelDraft {
  name: string
  service?: string
  provider?: string
  base_url?: string
  model: string
  /** `null` / 省略表示沿用原文件里的 key。 */
  api_key?: string | null
}

export interface Snapshot {
  protocolVersion: number
  capabilities: string[]
  messages: MessageWire[]
  status: SessionStatus
  tools: string[]
  sandbox: SandboxInfo
  /** 会话工作区绝对路径；旧后端没有这个字段。 */
  workspace?: string
  /** 当前模型；旧后端没有这个字段。 */
  model?: ModelSummary
}

export interface ConfirmRequest {
  id: string
  question: string
}

export interface SessionInfo {
  id: string
  title: string
  /** 文件最后写入时间，毫秒时间戳。 */
  updated: number
  workspace: string
}

export interface SessionListPayload {
  sessions: SessionInfo[]
  activeId: string | null
}

export interface FsEntry {
  name: string
  /** 相对工作区的 POSIX 路径。 */
  path: string
  dir: boolean
}

export interface FsFilePayload {
  path: string
  content: string
  binary: boolean
  truncated: boolean
}

/** 工作区选择对话框：`path` 为空时 `dirs` 是可选起点（盘符 / 主目录）。 */
export interface BrowsePayload {
  path: string
  parent: string | null
  dirs: FsEntry[]
}

export interface StatusPayload {
  status: SessionStatus
  error?: { kind: string; message: string }
}

export type ClientCommand =
  | { type: 'session/prompt'; text: string }
  | { type: 'session/cancel' }
  | { type: 'session/create'; workspace?: string }
  | { type: 'session/open'; id: string }
  | { type: 'session/rename'; id: string; title: string }
  | { type: 'session/delete'; id: string }
  | { type: 'model/select'; id: string }
  | { type: 'settings/get' }
  | { type: 'settings/save'; models: ModelDraft[] }
  | { type: 'interaction/answer'; id: string; ok: boolean }
  | { type: 'fs/list'; path: string }
  | { type: 'fs/read'; path: string }
  | { type: 'workspace/browse'; path: string }
