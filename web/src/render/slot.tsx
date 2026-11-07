import type { ComponentType, ReactNode } from 'react'
import type { SlotCell } from '../slots/registry'

/** 把槽位单元格渲染成元素。只有 renderer 与渲染位组件使用它。 */
export function CellView({ cell, props }: { cell: SlotCell; props: unknown }): ReactNode {
  const Component = cell.component as ComponentType<Record<string, unknown>>
  return <Component {...(props as Record<string, unknown>)} />
}
