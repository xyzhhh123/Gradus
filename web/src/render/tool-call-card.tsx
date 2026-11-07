import { useState, type ReactNode } from 'react'
import type { ToolCard } from '../model/state'
import { ChevronIcon, toolIcon } from '../icons'

function summary(card: ToolCard): string {
  if (!card.args || typeof card.args !== 'object') return ''
  const record = card.args as Record<string, unknown>
  for (const key of ['command', 'path', 'file_path', 'pattern', 'query']) {
    const value = record[key]
    if (typeof value === 'string' && value) return value
  }
  return ''
}

interface Props {
  card: ToolCard
  /** 默认输出；插件可以传更贴合工具的内容。 */
  children?: ReactNode
}

/** 工具调用卡片：标题行 + 可折叠输出。运行中默认展开，完成后默认收起。 */
export function ToolCallCard({ card, children }: Props) {
  const [open, setOpen] = useState(card.status !== 'done')
  const Icon = toolIcon(card.name)
  const detail = summary(card)

  return (
    <div className={`tool-card tool-status-${card.status}`}>
      <button
        type="button"
        className="tool-head"
        aria-expanded={open}
        onClick={() => setOpen(value => !value)}
      >
        <span className="tool-icon">
          <Icon />
        </span>
        <span className="tool-name">{card.name}</span>
        {detail && <span className="tool-summary">{detail}</span>}
        <span className="tool-spacer" />
        {card.status === 'running' && <span className="tool-badge">运行中</span>}
        {card.status === 'cancelled' && (
          <span className="tool-badge tool-badge-muted">已取消</span>
        )}
        {card.durationMs !== undefined && <span className="tool-time">{card.durationMs}ms</span>}
        <ChevronIcon className={`tool-chevron${open ? ' tool-chevron-open' : ''}`} />
      </button>

      {open && (
        <div className="tool-body">
          {children ?? <pre>{card.content ?? card.partial ?? '…'}</pre>}
        </div>
      )}
    </div>
  )
}
