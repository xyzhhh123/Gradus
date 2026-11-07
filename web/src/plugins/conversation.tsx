import type { Plugin } from '@deepseek-ai/cordis'
import type { TurnState } from '../model/state'
import type { RenderSlot, RootProps } from '../render/types'

interface TurnProps {
  turn: TurnState
  renderSlot: RenderSlot
}

function ConversationBlock({ state, renderSlot }: RootProps) {
  return (
    <section className="conversation">
      {state.messages.map(message => (
        <div key={message.id} className={`message message-${message.role}`}>
          {message.text}
        </div>
      ))}

      {state.turns.map(turn => (
        <div key={turn.index} className="turn">
          {renderSlot('conversation.turn', { turn, renderSlot })}
        </div>
      ))}

      {state.error && (
        <div className="error">
          {state.error.kind}: {state.error.message}
        </div>
      )}
    </section>
  )
}

function TurnView({ turn, renderSlot }: TurnProps) {
  return (
    <>
      {turn.assistant && <div className="assistant">{turn.assistant}</div>}

      {turn.tools.map(card => (
        <div key={card.id} className="tool">
          {renderSlot('tool.call.view', { card }, card.name) ?? (
            <pre className="card card-default">{JSON.stringify(card.args, null, 2)}</pre>
          )}
        </div>
      ))}

      {turn.usage && (
        <div className="usage">
          in {turn.usage.inputTokens} / out {turn.usage.outputTokens}
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
