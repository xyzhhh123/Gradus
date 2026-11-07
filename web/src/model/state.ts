import type {
  BrowsePayload,
  ConnectionStatus,
  MessageWire,
  ModelInfo,
  ModelSummary,
  SandboxInfo,
  SessionInfo,
  SessionStatus,
  UsageWire,
} from '../protocol/frames'

export interface ToolCard {
  id: string
  name: string
  args: unknown
  status: 'running' | 'done' | 'error' | 'cancelled'
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
  /** 会话工作区绝对路径；快照没带时为空串。 */
  workspace: string
  /** 磁盘上的会话列表；当前会话用 `activeId` 标记。 */
  sessions: SessionInfo[]
  activeId: string | null
  /** 工作区选择对话框的当前目录；`null` 表示没在浏览。 */
  browse: BrowsePayload | null
  browseError: string | null
  /** 当前模型；快照没带时为空。 */
  model: ModelSummary | null
  /** 后端报出的可选能力；旧后端或没带快照时为空数组。 */
  capabilities: string[]
  /** 可切换的模型；只有一个时界面不显示选择器。 */
  models: ModelInfo[]
  activeModelId: string | null
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
    workspace: '',
    sessions: [],
    activeId: null,
    browse: null,
    browseError: null,
    model: null,
    capabilities: [],
    models: [],
    activeModelId: null,
    pendingConfirm: null,
    error: null,
  }
}
