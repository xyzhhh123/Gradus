import type { Frame, MessageWire, Snapshot, StatusPayload, WireEvent } from '../protocol/frames'
import type { SessionState, TurnState } from './state'

/** 事件流 → UI 状态。纯函数：同一输入必须得到同一输出。 */
export function reduce(state: SessionState, frame: Frame): SessionState {
  switch (frame.type) {
    case 'session/snapshot':
      return fromSnapshot(state, frame.payload as Snapshot)
    case 'session/message': {
      // 服务端回显用户输入；重启或重连后可能重复到达，按 id 去重。
      const message = frame.payload as MessageWire
      if (state.messages.some(existing => existing.id === message.id)) return state
      return { ...state, messages: [...state.messages, message] }
    }
    case 'agent/event':
      return applyEvent(state, frame.payload as WireEvent)
    case 'interaction/confirm': {
      const payload = frame.payload as { id: string; question: string }
      return { ...state, pendingConfirm: { id: payload.id, question: payload.question } }
    }
    case 'session/status': {
      const payload = frame.payload as StatusPayload
      return {
        ...state,
        status: payload.status,
        error: payload.error ?? null,
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

    case 'agent/error':
      return {
        ...state,
        status: 'error',
        error: { kind: event.kind, message: event.message },
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
