import type {
  BrowsePayload,
  Frame,
  MessageWire,
  ModelStatePayload,
  SessionListPayload,
  Snapshot,
  StatusPayload,
  WireEvent,
} from '../protocol/frames'
import type { SessionState, TurnState } from './state'

/** 事件流 → UI 状态。纯函数：同一输入必须得到同一输出。 */
export function reduce(state: SessionState, frame: Frame): SessionState {
  switch (frame.type) {
    case 'session/snapshot':
      return fromSnapshot(state, frame.payload as Snapshot)
    case 'session/message': {
      // 用户消息既可能是本轮回显，也可能是重连后的补发；按 id 去重。
      // 先把上一轮的助手文本落入历史，再追加这一条，顺序才不会颠倒。
      const message = frame.payload as MessageWire
      const committed = commitTurns(state)
      if (committed.messages.some(existing => existing.id === message.id)) return committed
      return { ...committed, messages: [...committed.messages, message] }
    }
    case 'agent/event':
      return applyEvent(state, frame.payload as WireEvent)
    case 'interaction/confirm': {
      const payload = frame.payload as { id: string; question: string }
      return { ...state, pendingConfirm: { id: payload.id, question: payload.question } }
    }
    case 'session/list': {
      const payload = frame.payload as SessionListPayload
      return {
        ...state,
        sessions: payload.sessions ?? [],
        activeId: payload.activeId ?? null,
      }
    }
    case 'session/status': {
      const payload = frame.payload as StatusPayload
      return {
        ...state,
        status: payload.status,
        error: payload.status === 'error' ? payload.error ?? null : null,
        // 一轮结束后不再需要审批；残留面板会挡在输入框上方。
        pendingConfirm: payload.status === 'running' ? state.pendingConfirm : null,
      }
    }
    case 'workspace/dirs': {
      const payload = frame.payload as BrowsePayload
      return { ...state, browse: payload, browseError: null }
    }
    case 'workspace/error': {
      const payload = frame.payload as { message: string }
      return { ...state, browseError: payload.message }
    }
    case 'model/state': {
      const payload = frame.payload as ModelStatePayload
      return {
        ...state,
        models: payload.models ?? [],
        activeModelId: payload.activeId ?? null,
        model: payload.active ?? state.model,
      }
    }
    default:
      return state
  }
}

function fromSnapshot(state: SessionState, snapshot: Snapshot): SessionState {
  return {
    ...state,
    connection: 'open',
    status: snapshot.status,
    messages: snapshot.messages ?? [],
    tools: snapshot.tools ?? [],
    sandbox: snapshot.sandbox ?? state.sandbox,
    workspace: snapshot.workspace ?? state.workspace,
    model: snapshot.model ?? state.model,
    capabilities: snapshot.capabilities ?? [],
    turns: [],
    pendingConfirm: null,
    error: null,
  }
}

export function applyEvent(state: SessionState, event: WireEvent): SessionState {
  switch (event.type) {
    case 'agent/start':
      return { ...state, status: 'running', turns: [], error: null }

    case 'turn/start':
      return { ...state, turns: [...state.turns, { index: event.turn, assistant: '', tools: [] }] }

    case 'assistant/delta':
      return updateLastTurn(state, turn => ({
        ...turn,
        assistant: turn.assistant + event.text,
      }))

    case 'tool/start':
      return updateLastTurn(state, turn => ({
        ...turn,
        tools: [
          ...turn.tools,
          {
            id: event.call.id,
            name: event.call.name,
            args: event.call.args,
            status: 'running',
            partial: '',
          },
        ],
      }))

    case 'tool/update':
      return updateTool(state, event.id, card => ({
        ...card,
        partial: card.partial + event.partial,
      }))

    case 'tool/end':
      return updateTool(state, event.id, card => ({
        ...card,
        status: event.result.isError ? 'error' : 'done',
        content: event.result.content,
        durationMs: event.durationMs,
      }))

    case 'turn/end':
      return updateLastTurn(state, turn => ({ ...turn, usage: event.usage }))

    case 'agent/done':
      return { ...state, status: 'idle' }

    case 'agent/error': {
      // 被中止不是失败：回到就绪，只把还在跑的工具标成已取消。
      const next = cancelRunningTools(state)
      if (event.kind === 'aborted') {
        return { ...next, status: 'idle', error: null }
      }
      return {
        ...next,
        status: 'error',
        error: { kind: event.kind, message: event.message },
      }
    }

    default:
      return state
  }
}

function updateLastTurn(
  state: SessionState,
  update: (turn: TurnState) => TurnState,
): SessionState {
  const index = state.turns.length - 1
  if (index < 0) return state
  const turns = state.turns.slice()
  turns[index] = update(turns[index])
  return { ...state, turns }
}

/** 一轮结束后把助手文本并入历史：下一次提问时，旧内容不会消失或错位。 */
function commitTurns(state: SessionState): SessionState {
  const texts = state.turns.map(turn => turn.assistant.trim()).filter(text => text !== '')
  if (texts.length === 0) return state

  const base = state.messages.length
  const added: MessageWire[] = texts.map((text, offset) => ({
    id: `a-${base}-${offset}`,
    role: 'assistant',
    text,
  }))
  return { ...state, messages: [...state.messages, ...added], turns: [] }
}

/** 中止时还没收到 `tool/end` 的卡片永远等不到结果，直接标成已取消。 */
function cancelRunningTools(state: SessionState): SessionState {
  const running = state.turns.some(turn => turn.tools.some(tool => tool.status === 'running'))
  if (!running) return state

  return {
    ...state,
    turns: state.turns.map(turn => ({
      ...turn,
      tools: turn.tools.map(tool =>
        tool.status === 'running' ? { ...tool, status: 'cancelled' } : tool,
      ),
    })),
  }
}

/** 按 `call.id` 归属，不能用「最后一张卡片」代替：并行批次的更新会交错。 */
function updateTool(
  state: SessionState,
  id: string,
  update: (card: TurnState['tools'][number]) => TurnState['tools'][number],
): SessionState {
  let found = false
  const turns = state.turns.map(turn => {
    if (!turn.tools.some(card => card.id === id)) return turn
    found = true
    return { ...turn, tools: turn.tools.map(card => (card.id === id ? update(card) : card)) }
  })
  return found ? { ...state, turns } : state
}
