import type { ReactNode } from 'react'
import type { SessionState } from '../model/state'

/** 用户动作。组件拿到的是回调，不是服务对象。 */
export interface SessionActions {
  prompt(text: string): void
  cancel(): void
  answer(id: string, ok: boolean): void
}

/** 渲染子槽位：`entryKey` 用于 keyed 位。 */
export type RenderSlot = (name: string, props: unknown, entryKey?: string) => ReactNode

/** 每个 `app.root` / `shell.blocks` 单元格收到的统一输入。 */
export interface RootProps {
  state: SessionState
  actions: SessionActions
  renderSlot: RenderSlot
}
