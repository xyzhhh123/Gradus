import { describe, expect, it } from 'vitest'
import type { Frame } from '../protocol/frames'
import { applyEvent, reduce } from './reduce'
import { initialState } from './state'

const frame = (type: string, payload: unknown): Frame => ({ seq: 1, type, payload })

describe('reduce', () => {
  it('快照替换整份状态', () => {
    const state = reduce(
      initialState(),
      frame('session/snapshot', {
        protocolVersion: 1,
        capabilities: [],
        messages: [{ id: 'm1', role: 'user', text: 'hi' }],
        status: 'idle',
        tools: ['bash'],
        sandbox: { backend: 'restricted', warning: 'network NOT isolated' },
      }),
    )
    expect(state.connection).toBe('open')
    expect(state.messages).toHaveLength(1)
    expect(state.sandbox.backend).toBe('restricted')
  })

  it('并行批次的工具更新按 id 归属，不落到最后一张卡片', () => {
    let state = applyEvent(initialState(), { type: 'agent/start' })
    state = applyEvent(state, { type: 'turn/start', turn: 0 })
    state = applyEvent(state, {
      type: 'tool/start',
      call: { id: 'a', name: 'bash', args: {} },
    })
    state = applyEvent(state, {
      type: 'tool/start',
      call: { id: 'b', name: 'bash', args: {} },
    })
    state = applyEvent(state, { type: 'tool/update', id: 'a', partial: 'x' })
    state = applyEvent(state, { type: 'tool/update', id: 'b', partial: 'y' })

    const [a, b] = state.turns[0].tools
    expect(a.partial).toBe('x')
    expect(b.partial).toBe('y')
  })

  it('同一输入重复应用得到相同结果', () => {
    const events = [
      frame('agent/event', { type: 'agent/start' }),
      frame('agent/event', { type: 'turn/start', turn: 0 }),
      frame('agent/event', { type: 'assistant/delta', text: 'hello' }),
      frame('agent/event', { type: 'agent/done', usage: { inputTokens: 1, outputTokens: 2 }, turns: 1 }),
    ]
    const once = events.reduce(reduce, initialState())
    const twice = events.reduce(reduce, initialState())
    expect(once).toEqual(twice)
  })

  it('session/message 按 id 去重', () => {
    const message = { id: 'u1', role: 'user' as const, text: 'hi' }
    let state = reduce(initialState(), frame('session/message', message))
    state = reduce(state, frame('session/message', message))
    expect(state.messages).toHaveLength(1)
  })

  it('无关帧保持同一个引用', () => {
    const state = initialState()
    expect(reduce(state, frame('unknown/type', {}))).toBe(state)
  })
})
