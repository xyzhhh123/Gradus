import type { Plugin } from '@deepseek-ai/cordis'
import type { RootProps } from '../render/types'

function ApprovalBlock({ state, actions }: RootProps) {
  const pending = state.pendingConfirm
  if (!pending) return null

  return (
    <div className="approval">
      <p className="approval-question">{pending.question}</p>
      <div className="approval-actions">
        <button type="button" onClick={() => actions.answer(pending.id, true)}>
          允许
        </button>
        <button type="button" className="danger" onClick={() => actions.answer(pending.id, false)}>
          拒绝
        </button>
      </div>
    </div>
  )
}

export const approvalPlugin: Plugin = {
  name: 'approval',
  inject: ['slots'],
  apply(ctx) {
    ctx.effect(() =>
      ctx.slots.inject('shell.blocks', () =>
        ctx.slots.register('shell.blocks', {
          id: 'approval',
          order: 40,
          component: ApprovalBlock,
        }),
      ),
    )
  },
}
