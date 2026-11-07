import { useEffect, useRef, useState } from 'react'
import type { Plugin } from '@deepseek-ai/cordis'
import type { TurnState } from '../model/state'
import type { RootProps, RenderSlot } from '../render/types'
import { Markdown } from '../render/markdown'
import { ToolCallCard } from '../render/tool-call-card'
import { CheckIcon, CopyIcon, SparkIcon } from '../icons'

interface TurnProps {
  turn: TurnState
  running: boolean
  renderSlot: RenderSlot
}

const SUGGESTIONS = ['看看这个项目的结构', '跑一遍测试', '解释 gradus/coding/session.py']

function UserRow({ text }: { text: string }) {
  const [copied, setCopied] = useState(false)

  return (
    <div className="user-row">
      <div className="user-bubble">{text}</div>
      <div className="msg-actions">
        <button
          type="button"
          className="icon-btn"
          title="复制"
          onClick={() => {
            void navigator.clipboard.writeText(text).then(() => {
              setCopied(true)
              setTimeout(() => setCopied(false), 1200)
            })
          }}
        >
          {copied ? <CheckIcon /> : <CopyIcon />}
        </button>
      </div>
    </div>
  )
}

function EmptyState({ actions, disabled }: { actions: RootProps['actions']; disabled: boolean }) {
  return (
    <div className="empty">
      <span className="empty-mark">
        <SparkIcon />
      </span>
      <h2>有什么可以帮你的？</h2>
      <p>Gradus 会在工作区副本里执行命令，改动需要你确认。</p>
      <div className="chips">
        {SUGGESTIONS.map(text => (
          <button
            key={text}
            type="button"
            className="chip"
            disabled={disabled}
            onClick={() => actions.prompt(text)}
          >
            {text}
          </button>
        ))}
      </div>
    </div>
  )
}

function ConversationBlock({ state, actions, renderSlot }: RootProps) {
  const viewport = useRef<HTMLElement>(null)
  const stick = useRef(true)
  const empty = state.messages.length === 0 && state.turns.length === 0

  useEffect(() => {
    const element = viewport.current
    if (element && stick.current) element.scrollTop = element.scrollHeight
  })

  return (
    <section
      className="conversation"
      ref={viewport}
      onScroll={() => {
        const element = viewport.current
        if (!element) return
        stick.current =
          element.scrollHeight - element.scrollTop - element.clientHeight < 48
      }}
    >
      <div className="conversation-inner">
        {empty && (
          <EmptyState
            actions={actions}
            disabled={state.connection !== 'open' || state.status === 'running'}
          />
        )}

        {state.messages.map(message =>
          message.role === 'user' ? (
            <UserRow key={message.id} text={message.text} />
          ) : (
            <div key={message.id} className="assistant">
              <Markdown text={message.text} />
            </div>
          ),
        )}

        {state.turns.map(turn => (
          <div key={turn.index} className="turn">
            {renderSlot('conversation.turn', {
              turn,
              renderSlot,
              running: state.status === 'running' && turn.index === state.turns.length - 1,
            })}
          </div>
        ))}

        {state.error && (
          <div className="error">
            <strong>{state.error.kind}</strong>
            <span>{state.error.message}</span>
          </div>
        )}
      </div>
    </section>
  )
}

function TurnView({ turn, running, renderSlot }: TurnProps) {
  return (
    <>
      {turn.assistant && (
        <div className={`assistant${running ? ' streaming' : ''}`}>
          <Markdown text={turn.assistant} />
        </div>
      )}

      {turn.tools.length > 0 && (
        <div className="tools">
          {turn.tools.map(card => (
            <div key={card.id} className="tool">
              {renderSlot('tool.call.view', { card }, card.name) ?? <ToolCallCard card={card} />}
            </div>
          ))}
        </div>
      )}

      {turn.usage && (
        <div className="usage">
          <span>输入 {turn.usage.inputTokens}</span>
          <span>输出 {turn.usage.outputTokens}</span>
        </div>
      )}
    </>
  )
}

export const conversationPlugin: Plugin = {
  name: 'conversation',
  inject: ['slots', 'sessions'],
  apply(ctx) {
    ctx.effect(() => ctx.slots.declare('conversation.turn', 'list'))
    ctx.effect(() => ctx.slots.declare('tool.call.view', 'keyed'))
    ctx.effect(() =>
      ctx.slots.inject('shell.blocks', () =>
        ctx.slots.register('shell.blocks', {
          id: 'conversation',
          order: 10,
          component: ConversationBlock,
        }),
      ),
    )
    ctx.effect(() =>
      ctx.slots.register('conversation.turn', { id: 'default-turn', component: TurnView }),
    )
  },
}
