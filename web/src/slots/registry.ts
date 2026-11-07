export type SlotKind = 'single' | 'list' | 'keyed'

export interface SlotCell {
  id: string
  /** 仅 keyed 位使用：按业务键（例如工具名）选择要渲染的单元格。 */
  key?: string
  /** 越小越靠前；相同则按注册顺序。 */
  order: number
  /** React 组件。注册表本身不依赖 React，渲染方负责转换。 */
  component: unknown
}

/**
 * 声明 + 授权的最小实现。
 *
 * 规则：
 * 1. 渲染未声明的位置直接抛错；
 * 2. 注册返回撤销函数；
 * 3. 声明晚于注册时，`inject` 会等它出现，声明消失后贡献一并撤销。
 */
export class Slots {
  private declarations = new Map<string, SlotKind>()
  private cells = new Map<string, SlotCell[]>()
  private waiters = new Map<string, Set<() => void>>()
  private waiterCleanups = new Map<() => void, () => void>()
  private listeners = new Set<() => void>()
  private revision = 0

  declare(name: string, kind: SlotKind = 'list'): () => void {
    if (this.declarations.has(name)) {
      throw new Error(`slot "${name}" is already declared`)
    }
    this.declarations.set(name, kind)
    this.touch()

    for (const waiter of [...(this.waiters.get(name) ?? [])]) {
      this.waiterCleanups.get(waiter)?.()
      this.waiterCleanups.delete(waiter)
      waiter()
    }

    return () => {
      if (!this.declarations.has(name)) return
      // 声明塌掉时，等它的贡献也要一起撤掉；重新声明后它们会再跑一次。
      for (const waiter of [...(this.waiters.get(name) ?? [])]) {
        this.waiterCleanups.get(waiter)?.()
        this.waiterCleanups.delete(waiter)
      }
      this.declarations.delete(name)
      this.cells.delete(name)
      this.touch()
    }
  }

  isDeclared(name: string): boolean {
    return this.declarations.has(name)
  }

  register(name: string, cell: Omit<SlotCell, 'order'> & { order?: number }): () => void {
    if (!this.declarations.has(name)) {
      throw new Error(`slot "${name}" is not declared`)
    }
    const normalized: SlotCell = { order: 0, ...cell }
    const list = [...(this.cells.get(name) ?? []), normalized].sort(
      (a, b) => a.order - b.order,
    )
    this.cells.set(name, list)
    this.touch()

    return () => {
      const current = this.cells.get(name)
      if (!current) return
      this.cells.set(
        name,
        current.filter(item => item !== normalized),
      )
      this.touch()
    }
  }

  get(name: string): readonly SlotCell[] {
    return this.cells.get(name) ?? []
  }

  /**
   * 等某个位置被声明后再执行。
   * 声明塌掉时撤销贡献，重新声明后再跑一遍。
   */
  inject(name: string, callback: () => (() => void) | void): () => void {
    let active = true

    const run = () => {
      if (!active) return
      this.waiterCleanups.get(run)?.()
      this.waiterCleanups.delete(run)
      const result = callback()
      if (typeof result === 'function') this.waiterCleanups.set(run, result)
    }

    if (this.declarations.has(name)) {
      run()
    } else {
      const set = this.waiters.get(name) ?? new Set<() => void>()
      set.add(run)
      this.waiters.set(name, set)
    }

    return () => {
      active = false
      this.waiters.get(name)?.delete(run)
      this.waiterCleanups.get(run)?.()
      this.waiterCleanups.delete(run)
    }
  }

  subscribe = (listener: () => void): (() => void) => {
    this.listeners.add(listener)
    return () => {
      this.listeners.delete(listener)
    }
  }

  getVersion = (): number => this.revision

  private touch(): void {
    this.revision += 1
    for (const listener of [...this.listeners]) listener()
  }
}
