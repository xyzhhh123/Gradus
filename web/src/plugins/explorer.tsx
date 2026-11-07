import {
  useEffect,
  useState,
  useSyncExternalStore,
  type PointerEvent as ReactPointerEvent,
  type ReactNode,
} from 'react'
import type { Plugin } from '@deepseek-ai/cordis'
import type { ConnectionApi } from './connection'
import type { FsEntry, FsFilePayload } from '../protocol/frames'
import { Markdown } from '../render/markdown'
import { ChevronIcon, FileIcon, FolderIcon } from '../icons'

/** 与 dsh 的 columns.ts 对齐：右栏最小 300，最大占整帧的 70%。 */
const MIN_WIDTH = 300
const DEFAULT_WIDTH = 340
const MAX_RATIO = 0.7
const WIDTH_KEY = 'gradus.explorer.width'

function clampWidth(px: number): number {
  const max = Math.max(MIN_WIDTH, Math.round(window.innerWidth * MAX_RATIO))
  return Math.min(max, Math.max(MIN_WIDTH, Math.round(px)))
}

function storedWidth(): number {
  try {
    const raw = Number(localStorage.getItem(WIDTH_KEY))
    return Number.isFinite(raw) && raw > 0 ? clampWidth(raw) : DEFAULT_WIDTH
  } catch {
    return DEFAULT_WIDTH
  }
}

function basename(path: string): string {
  const parts = path.split('/').filter(Boolean)
  return parts[parts.length - 1] ?? path
}

interface ExplorerState {
  open: boolean
  /** 目录路径 → 子项；`''` 是工作区根。 */
  tree: Record<string, FsEntry[]>
  expanded: string[]
  /** 已打开的文件，按打开顺序；同一路径只留一份。 */
  files: FsFilePayload[]
  /** 当前查看的文件；`null` 表示显示目录树。 */
  activePath: string | null
  error: string | null
}

interface ExplorerApi {
  subscribe(listener: () => void): () => void
  getState(): ExplorerState
  toggle(): void
  expand(path: string): void
  read(path: string): void
  show(path: string | null): void
  close(path: string): void
  closePanel(): void
}

const EMPTY: ExplorerState = {
  open: false,
  tree: {},
  expanded: [''],
  files: [],
  activePath: null,
  error: null,
}

function createExplorer(connection: ConnectionApi): ExplorerApi {
  let state = EMPTY
  const listeners = new Set<() => void>()

  const set = (patch: Partial<ExplorerState>) => {
    state = { ...state, ...patch }
    for (const listener of [...listeners]) listener()
  }

  connection.onFrame(frame => {
    if (frame.type === 'fs/entries') {
      const payload = frame.payload as { path: string; entries: FsEntry[] }
      set({ tree: { ...state.tree, [payload.path]: payload.entries }, error: null })
    } else if (frame.type === 'fs/file') {
      const file = frame.payload as FsFilePayload
      const known = state.files.some(item => item.path === file.path)
      const files = known
        ? state.files.map(item => (item.path === file.path ? file : item))
        : [...state.files, file]
      set({ files, activePath: file.path, error: null })
    } else if (frame.type === 'fs/error') {
      set({ error: (frame.payload as { message: string }).message })
    }
  })

  const ensure = (path: string) => {
    if (!state.tree[path]) connection.send({ type: 'fs/list', path })
  }

  return {
    subscribe(listener) {
      listeners.add(listener)
      return () => listeners.delete(listener)
    },
    getState: () => state,
    toggle() {
      const open = !state.open
      set({ open })
      if (open) ensure('')
    },
    expand(path) {
      const wasExpanded = state.expanded.includes(path)
      const expanded = wasExpanded
        ? state.expanded.filter(item => item !== path)
        : [...state.expanded, path]
      set({ expanded })
      if (!wasExpanded) ensure(path)
    },
    read(path) {
      // 已打开过就直接聚焦，不重复请求；想看最新内容时由后端 push 覆盖。
      if (state.files.some(item => item.path === path)) {
        set({ activePath: path })
        return
      }
      connection.send({ type: 'fs/read', path })
    },
    show(path) {
      set({ activePath: path })
    },
    close(path) {
      const index = state.files.findIndex(item => item.path === path)
      if (index < 0) return
      const files = state.files.filter(item => item.path !== path)
      if (state.activePath !== path) {
        set({ files })
        return
      }
      // 关掉当前文件后退到右邻，没有就左邻，再没有就回目录树。
      const next = files[index] ?? files[index - 1] ?? null
      set({ files, activePath: next?.path ?? null })
    },
    closePanel() {
      set({ open: false })
    },
  }
}

function Tree({ explorer, path, depth }: { explorer: ExplorerApi; path: string; depth: number }) {
  const { tree, expanded, activePath } = explorer.getState()
  const entries = tree[path] ?? []

  return (
    <>
      {entries.map(entry => (
        <div key={entry.path}>
          <button
            type="button"
            className={`tree-row${!entry.dir && entry.path === activePath ? ' tree-row-active' : ''}`}
            style={{ paddingLeft: 8 + depth * 12 }}
            title={entry.path}
            onClick={() => (entry.dir ? explorer.expand(entry.path) : explorer.read(entry.path))}
          >
            {entry.dir ? (
              <ChevronIcon
                className={`tree-chevron${expanded.includes(entry.path) ? ' tree-chevron-open' : ''}`}
              />
            ) : (
              <span className="tree-indent" />
            )}
            {entry.dir ? <FolderIcon /> : <FileIcon />}
            <span className="tree-name">{entry.name}</span>
          </button>
          {entry.dir && expanded.includes(entry.path) && (
            <Tree explorer={explorer} path={entry.path} depth={depth + 1} />
          )}
        </div>
      ))}
    </>
  )
}

function ExplorerToggle({ explorer }: { explorer: ExplorerApi }) {
  const state = useSyncExternalStore(explorer.subscribe, explorer.getState)

  return (
    <button
      type="button"
      className={`sidebar-toggle${state.open ? ' sidebar-toggle-active' : ''}`}
      title="工作区文件"
      onClick={explorer.toggle}
    >
      <FolderIcon />
    </button>
  )
}

function ExplorerPanel({ explorer }: { explorer: ExplorerApi }) {
  const state = useSyncExternalStore(explorer.subscribe, explorer.getState)
  const [width, setWidth] = useState(storedWidth)

  useEffect(() => {
    try {
      localStorage.setItem(WIDTH_KEY, String(width))
    } catch {
      // 隐私模式下写不了，忽略即可。
    }
  }, [width])

  const startResize = (event: ReactPointerEvent<HTMLDivElement>) => {
    event.preventDefault()
    const startX = event.clientX
    const startWidth = width

    const move = (moveEvent: PointerEvent) => {
      // 面板贴右边：往左拖变宽。
      setWidth(clampWidth(startWidth - (moveEvent.clientX - startX)))
    }
    const stop = () => {
      window.removeEventListener('pointermove', move)
      window.removeEventListener('pointerup', stop)
    }
    window.addEventListener('pointermove', move)
    window.addEventListener('pointerup', stop)
  }

  if (!state.open) return null

  const active = state.files.find(item => item.path === state.activePath) ?? null

  let body: ReactNode
  if (!active) {
    body = (
      <div className="explorer-tree">
        <Tree explorer={explorer} path="" depth={0} />
      </div>
    )
  } else if (active.binary) {
    body = <div className="explorer-empty">二进制文件，无法预览</div>
  } else if (active.path.endsWith('.md') || active.path.endsWith('.mdx')) {
    body = (
      <div className="explorer-markdown">
        <Markdown text={active.content} />
      </div>
    )
  } else {
    body = (
      <pre className="explorer-pre">
        {active.content}
        {active.truncated ? '\n…（已截断）' : ''}
      </pre>
    )
  }

  return (
    <aside className="explorer" style={{ width }}>
      <div className="explorer-resizer" onPointerDown={startResize} title="拖动调整宽度" />

      <div className="explorer-head">
        <div className="explorer-tabs" role="tablist">
          <button
            type="button"
            role="tab"
            aria-selected={active === null}
            className={active === null ? 'active' : ''}
            onClick={() => explorer.show(null)}
          >
            <FolderIcon />
            文件
          </button>
          {state.files.map(file => (
            <span
              key={file.path}
              className={`explorer-tab${file.path === state.activePath ? ' active' : ''}`}
            >
              <button
                type="button"
                role="tab"
                aria-selected={file.path === state.activePath}
                title={file.path}
                onClick={() => explorer.show(file.path)}
              >
                {basename(file.path)}
              </button>
              <button
                type="button"
                className="explorer-tab-close"
                title="关闭"
                onClick={() => explorer.close(file.path)}
              >
                ×
              </button>
            </span>
          ))}
        </div>
        <button
          type="button"
          className="sidebar-toggle"
          title="关闭面板"
          onClick={explorer.closePanel}
        >
          <ChevronIcon className="flip" />
        </button>
      </div>

      {state.error && <div className="explorer-error">{state.error}</div>}

      <div className="explorer-body">{body}</div>
    </aside>
  )
}

export const explorerPlugin: Plugin = {
  name: 'explorer',
  inject: ['connection', 'slots'],
  apply(ctx) {
    const explorer = createExplorer(ctx.connection)

    ctx.effect(() =>
      ctx.slots.inject('shell.head-actions', () =>
        ctx.slots.register('shell.head-actions', {
          id: 'explorer-toggle',
          component: () => <ExplorerToggle explorer={explorer} />,
        }),
      ),
    )
    ctx.effect(() =>
      ctx.slots.inject('shell.aside', () =>
        ctx.slots.register('shell.aside', {
          id: 'explorer',
          component: () => <ExplorerPanel explorer={explorer} />,
        }),
      ),
    )
  },
}
