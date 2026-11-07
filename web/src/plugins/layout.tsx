import { useEffect, useRef, useState, type PointerEvent as ReactPointerEvent } from 'react'
import type { Plugin } from '@deepseek-ai/cordis'
import type { SessionInfo } from '../protocol/frames'
import type { RootProps, SessionActions } from '../render/types'
import { ChevronIcon, FolderIcon, PencilIcon, PlusIcon, SparkIcon, TrashIcon } from '../icons'

/** 与 dsh 的 columns.ts 对齐：264–420，默认 280，图标条 56。 */
const MIN_WIDTH = 264
const MAX_WIDTH = 420
const DEFAULT_WIDTH = 280
const COLLAPSED_WIDTH = 56
/** 低于这个宽度自动收成图标条；再低于 DRAWER 就整列隐藏。 */
const AUTO_COLLAPSE = 1024
const DRAWER = 768
const WIDTH_KEY = 'gradus.sidebar.width'
const RECENT_KEY = 'gradus.workspaces'

type Layout = 'expanded' | 'rail' | 'hidden'

const CONNECTION_TEXT: Record<string, string> = {
  open: '已连接',
  connecting: '连接中',
  closed: '未连接',
}

const STATUS_TEXT: Record<string, string> = {
  idle: '就绪',
  running: '生成中',
  error: '出错',
}

function clampWidth(px: number): number {
  return Math.min(MAX_WIDTH, Math.max(MIN_WIDTH, Math.round(px)))
}

function storedWidth(): number {
  try {
    const raw = Number(localStorage.getItem(WIDTH_KEY))
    return Number.isFinite(raw) && raw > 0 ? clampWidth(raw) : DEFAULT_WIDTH
  } catch {
    return DEFAULT_WIDTH
  }
}

function recentWorkspaces(): string[] {
  try {
    const parsed = JSON.parse(localStorage.getItem(RECENT_KEY) ?? '[]')
    return Array.isArray(parsed) ? parsed.filter((item): item is string => typeof item === 'string') : []
  } catch {
    return []
  }
}

function rememberWorkspace(path: string): void {
  try {
    const next = [path, ...recentWorkspaces().filter(item => item !== path)].slice(0, 5)
    localStorage.setItem(RECENT_KEY, JSON.stringify(next))
  } catch {
    // 隐私模式下写不了，忽略即可。
  }
}

function basename(path: string): string {
  const parts = path.split(/[\\/]/).filter(Boolean)
  return parts[parts.length - 1] ?? path
}

function relativeTime(ms: number): string {
  const diff = Date.now() - ms
  if (diff < 60_000) return '刚刚'
  if (diff < 3_600_000) return `${Math.floor(diff / 60_000)} 分钟前`
  if (diff < 86_400_000) return `${Math.floor(diff / 3_600_000)} 小时前`
  return new Date(ms).toLocaleDateString()
}

function autoLayout(viewport: number): Layout {
  if (viewport < DRAWER) return 'hidden'
  if (viewport < AUTO_COLLAPSE) return 'rail'
  return 'expanded'
}

function useViewport(): number {
  const [width, setWidth] = useState(() => window.innerWidth)

  useEffect(() => {
    const onResize = () => setWidth(window.innerWidth)
    window.addEventListener('resize', onResize)
    return () => window.removeEventListener('resize', onResize)
  }, [])

  return width
}

function SessionRow({
  session,
  active,
  actions,
}: {
  session: SessionInfo
  active: boolean
  actions: SessionActions
}) {
  const [renaming, setRenaming] = useState(false)
  const [draft, setDraft] = useState(session.title)
  const [confirming, setConfirming] = useState(false)
  // Esc 取消时输入框会失焦，别让 onBlur 又把改动提交上去。
  const escaped = useRef(false)

  if (renaming) {
    const submit = () => {
      setRenaming(false)
      if (draft.trim() !== session.title) actions.renameSession(session.id, draft)
    }
    return (
      <form
        className="session-rename"
        onSubmit={event => {
          event.preventDefault()
          submit()
        }}
      >
        <input
          autoFocus
          value={draft}
          placeholder="会话标题"
          onChange={event => setDraft(event.target.value)}
          onBlur={() => {
            if (escaped.current) {
              escaped.current = false
              return
            }
            submit()
          }}
          onKeyDown={event => {
            if (event.key === 'Escape') {
              escaped.current = true
              setRenaming(false)
            }
          }}
        />
      </form>
    )
  }

  if (confirming) {
    return (
      <div className="session-confirm">
        <span>删除这条会话？</span>
        <button
          type="button"
          className="danger"
          onClick={() => {
            setConfirming(false)
            actions.deleteSession(session.id)
          }}
        >
          删除
        </button>
        <button type="button" onClick={() => setConfirming(false)}>
          取消
        </button>
      </div>
    )
  }

  return (
    <div className="session-row">
      <button
        type="button"
        className={`session-item${active ? ' session-active' : ''}`}
        title={session.title}
        onClick={() => {
          if (!active) actions.openSession(session.id)
        }}
      >
        <span className="session-title">{session.title}</span>
        <span className="session-meta">{relativeTime(session.updated)}</span>
      </button>
      <div className="session-actions">
        <button
          type="button"
          title="重命名"
          onClick={() => {
            setDraft(session.title)
            setRenaming(true)
          }}
        >
          <PencilIcon />
        </button>
        <button type="button" title="删除" onClick={() => setConfirming(true)}>
          <TrashIcon />
        </button>
      </div>
    </div>
  )
}

function WorkspaceDialog({
  state,
  actions,
  draft,
  onDraft,
  onClose,
}: {
  state: RootProps['state']
  actions: SessionActions
  draft: string
  onDraft(value: string): void
  onClose(): void
}) {
  const [recent] = useState(recentWorkspaces)
  const browse = state.browse

  const go = (path: string) => {
    onDraft(path)
    actions.browseWorkspace(path)
  }

  return (
    <div className="dialog-backdrop" onClick={onClose}>
      <div className="dialog" onClick={event => event.stopPropagation()}>
        <div className="dialog-head">
          <span>选择工作区</span>
          <span className="sidebar-spacer" />
          <button type="button" className="sidebar-toggle" title="关闭" onClick={onClose}>
            ×
          </button>
        </div>

        <form
          className="dialog-form"
          onSubmit={event => {
            event.preventDefault()
            go(draft)
          }}
        >
          <input
            autoFocus
            value={draft}
            placeholder="输入绝对路径后回车"
            onChange={event => onDraft(event.target.value)}
          />
          <button type="submit">前往</button>
        </form>

        {recent.length > 0 && (
          <div className="workspace-recent">
            {recent.map(path => (
              <button key={path} type="button" onClick={() => go(path)}>
                {path}
              </button>
            ))}
          </div>
        )}

        {state.browseError && <div className="explorer-error">{state.browseError}</div>}

        <div className="dialog-body">
          {browse?.parent && (
            <button type="button" className="tree-row" onClick={() => go(browse.parent as string)}>
              <span className="tree-indent" />
              <span className="tree-name">..</span>
            </button>
          )}
          {browse?.dirs.map(dir => (
            <button
              key={dir.path}
              type="button"
              className="tree-row"
              title={dir.path}
              onClick={() => go(dir.path)}
            >
              <FolderIcon />
              <span className="tree-name">{dir.name}</span>
            </button>
          ))}
          {browse && browse.dirs.length === 0 && (
            <div className="explorer-empty">没有子目录</div>
          )}
          {!browse && !state.browseError && <div className="explorer-empty">读取中…</div>}
        </div>

        <div className="dialog-foot">
          <code className="dialog-path">{browse?.path || '选择起点'}</code>
          <button
            type="button"
            className="primary"
            disabled={!browse?.path}
            onClick={() => {
              if (!browse?.path) return
              rememberWorkspace(browse.path)
              onClose()
              actions.createSession(browse.path)
            }}
          >
            切换到此处
          </button>
        </div>
      </div>
    </div>
  )
}

interface SidebarProps extends RootProps {
  mode: Layout
  width: number
  overlay: boolean
  onToggle(): void
  onClose(): void
  onResizeStart(event: ReactPointerEvent<HTMLDivElement>): void
}

function SidebarContent({
  state,
  actions,
  mode,
  width,
  overlay,
  onToggle,
  onClose,
  onResizeStart,
}: SidebarProps) {
  const [browsing, setBrowsing] = useState(false)
  const [draft, setDraft] = useState('')

  const workspace = state.workspace ? basename(state.workspace) : ''

  if (mode === 'rail') {
    return (
      <aside className="sidebar sidebar-collapsed" style={{ width: COLLAPSED_WIDTH }}>
        <span className="logo" title="Gradus">
          <SparkIcon />
        </span>
        <button type="button" className="sidebar-toggle" title="展开侧栏" onClick={onToggle}>
          <ChevronIcon />
        </button>
        <button
          type="button"
          className="new-chat"
          title="新对话"
          onClick={() => actions.createSession()}
        >
          <PlusIcon />
        </button>
        <span
          className={`dot dot-${state.connection}`}
          title={CONNECTION_TEXT[state.connection]}
        />
      </aside>
    )
  }

  return (
    <aside className={`sidebar${overlay ? ' sidebar-overlay' : ''}`} style={{ width }}>
      <div className="sidebar-head">
        <span className="logo">
          <SparkIcon />
        </span>
        <span className="brand">Gradus</span>
        <span className="sidebar-spacer" />
        <button
          type="button"
          className="sidebar-toggle"
          title={overlay ? '关闭侧栏' : '收起侧栏'}
          onClick={onClose}
        >
          <ChevronIcon className="flip" />
        </button>
      </div>

      <button type="button" className="new-chat" onClick={() => actions.createSession()}>
        <PlusIcon />
        新对话
      </button>

      <div className="workspace-row">
        <button
          type="button"
          className="workspace"
          title={state.workspace || '选择工作区'}
          onClick={() => {
            setDraft(state.workspace)
            setBrowsing(true)
            actions.browseWorkspace(state.workspace)
          }}
        >
          <FolderIcon />
          <span>{workspace || '选择工作区'}</span>
        </button>
      </div>

      <div className="sidebar-label">会话</div>
      <div className="session-list">
        {state.sessions.length === 0 && <div className="session-empty">还没有会话</div>}
        {state.sessions.map(session => (
          <SessionRow
            key={session.id}
            session={session}
            active={session.id === state.activeId}
            actions={actions}
          />
        ))}
      </div>

      <div className="sidebar-foot">
        <div className="foot-row">
          <span>沙箱</span>
          <span className="foot-value">{state.sandbox.backend}</span>
        </div>
        <div className="foot-row">
          <span>工具</span>
          <span className="foot-value">{state.tools.length}</span>
        </div>
        {state.sandbox.warning && <div className="foot-warn">{state.sandbox.warning}</div>}
        {state.connection !== 'open' && <div className="foot-warn">后端未连接，输入已禁用</div>}
      </div>

      {!overlay && (
        <div className="sidebar-resizer" onPointerDown={onResizeStart} title="拖动调整宽度" />
      )}

      {browsing && (
        <WorkspaceDialog
          state={state}
          actions={actions}
          draft={draft}
          onDraft={setDraft}
          onClose={() => setBrowsing(false)}
        />
      )}
    </aside>
  )
}

function Shell(props: RootProps) {
  const { state, renderSlot } = props
  const viewport = useViewport()
  const [width, setWidth] = useState(storedWidth)
  const [manual, setManual] = useState<Layout | null>(null)

  const auto = autoLayout(viewport)
  const mode = manual ?? auto
  /** 窄屏手动展开时盖在内容上，而不是挤压对话列。 */
  const overlay = mode === 'expanded' && viewport < AUTO_COLLAPSE

  // 跨断点时丢掉手动覆盖，回到该宽度下的默认布局。
  useEffect(() => {
    setManual(null)
  }, [auto])

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
      setWidth(clampWidth(startWidth + moveEvent.clientX - startX))
    }
    const stop = () => {
      window.removeEventListener('pointermove', move)
      window.removeEventListener('pointerup', stop)
    }
    window.addEventListener('pointermove', move)
    window.addEventListener('pointerup', stop)
  }

  const sidebar = {
    state,
    actions: props.actions,
    mode,
    width,
    overlay,
    onToggle: () => setManual('expanded'),
    onClose: () => setManual(viewport >= AUTO_COLLAPSE ? 'rail' : auto),
    onResizeStart: startResize,
  }

  return (
    <div className="frame">
      {mode !== 'hidden' && <SidebarContent {...props} {...sidebar} />}
      {overlay && <div className="sidebar-backdrop" onClick={sidebar.onClose} />}

      <main className="center">
        <header className="chat-head">
          <div className="chat-head-left">
            {mode === 'hidden' && (
              <button
                type="button"
                className="sidebar-toggle"
                title="打开侧栏"
                onClick={() => setManual('expanded')}
              >
                <ChevronIcon />
              </button>
            )}
            <div className="chat-heading">
              <div className="chat-title">会话</div>
              <div className="chat-sub">
                {state.sandbox.backend} · {state.tools.length} tools
              </div>
            </div>
          </div>

          <div className="chat-head-right">
            {renderSlot('shell.head-actions', props)}
            <span className={`pill pill-${state.status}`}>
              {STATUS_TEXT[state.status] ?? state.status}
            </span>
          </div>
        </header>

        <div className="chat-body">{renderSlot('shell.blocks', props)}</div>
      </main>

      {renderSlot('shell.aside', props)}
      {renderSlot('app.overlay', props)}
    </div>
  )
}

export const layoutPlugin: Plugin = {
  name: 'layout',
  inject: ['slots'],
  apply(ctx) {
    ctx.effect(() => ctx.slots.declare('app.root', 'single'))
    ctx.effect(() => ctx.slots.declare('shell.blocks', 'list'))
    ctx.effect(() => ctx.slots.declare('shell.head-actions', 'list'))
    ctx.effect(() => ctx.slots.declare('shell.aside', 'single'))
    ctx.effect(() => ctx.slots.declare('app.overlay', 'list'))
    ctx.effect(() => ctx.slots.register('app.root', { id: 'shell', component: Shell }))
  },
}
