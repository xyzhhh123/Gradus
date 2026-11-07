import type { ReactNode } from 'react'
import type { SessionState } from '../model/state'

/** 用户动作。组件拿到的是回调，不是服务对象。 */
export interface SessionActions {
  prompt(text: string): void
  cancel(): void
  answer(id: string, ok: boolean): void
  /** 新建会话；不给工作区时沿用当前的。 */
  createSession(workspace?: string): void
  /** 打开一条磁盘上的会话。 */
  openSession(id: string): void
  /** 改会话标题；空串表示恢复成取首条提问。 */
  renameSession(id: string, title: string): void
  /** 删掉一条会话；删的是当前会话时后端会先换到新会话。 */
  deleteSession(id: string): void
  /** 浏览工作区候选目录；`path` 为空取起点（盘符 / 主目录）。 */
  browseWorkspace(path: string): void
  /** 弹系统「选择文件夹」对话框；结果仍以 `workspace/dirs` 回来。 */
  pickWorkspace(path: string): void
  /** 切换到某个候选模型；`id` 来自 `model/state`。 */
  selectModel(id: string): void
}

/** 渲染子槽位：`entryKey` 用于 keyed 位。 */
export type RenderSlot = (name: string, props: unknown, entryKey?: string) => ReactNode

/** 每个 `app.root` / `shell.blocks` 单元格收到的统一输入。 */
export interface RootProps {
  state: SessionState
  actions: SessionActions
  renderSlot: RenderSlot
}
