import { useEffect, useSyncExternalStore, useState } from 'react'
import type { Plugin } from '@deepseek-ai/cordis'
import type { ConnectionApi } from './connection'
import type { ModelDraft, ProviderEntry, SettingsStatePayload } from '../protocol/frames'
import { MonitorIcon, MoonIcon, SlidersIcon, SunIcon, TrashIcon } from '../icons'
import { setPreference, themeStore, type ThemePreference } from '../model/theme'

/**
 * 设置页：配置模型服务商候选表。
 *
 * 只做「读配置 / 写配置」，不碰会话；保存后由 host 重读配置再广播
 * `settings/state` 与 `model/state`，这里不自己推断结果。
 */

interface SettingsStore {
  open: boolean
  data: SettingsStatePayload | null
  error: string | null
  pending: boolean
}

interface SettingsApi {
  subscribe(listener: () => void): () => void
  getState(): SettingsStore
  open(): void
  close(): void
  reload(): void
  save(models: ModelDraft[]): void
}

const EMPTY: SettingsStore = { open: false, data: null, error: null, pending: false }

function createSettings(connection: ConnectionApi): SettingsApi {
  let state = EMPTY
  const listeners = new Set<() => void>()

  const set = (patch: Partial<SettingsStore>) => {
    state = { ...state, ...patch }
    for (const listener of [...listeners]) listener()
  }

  connection.onFrame(frame => {
    if (frame.type === 'settings/state') {
      set({ data: frame.payload as SettingsStatePayload, error: null, pending: false })
    } else if (frame.type === 'settings/error') {
      set({ error: (frame.payload as { message: string }).message, pending: false })
    }
  })

  return {
    subscribe(listener) {
      listeners.add(listener)
      return () => listeners.delete(listener)
    },
    getState: () => state,
    open() {
      set({ open: true })
      connection.send({ type: 'settings/get' })
    },
    close() {
      set({ open: false })
    },
    reload() {
      set({ pending: true, error: null })
      connection.send({ type: 'settings/get' })
    },
    save(models) {
      set({ pending: true, error: null })
      connection.send({ type: 'settings/save', models })
    },
  }
}

interface Draft {
  name: string
  service: string
  provider: string
  baseUrl: string
  model: string
  apiKey: string
  hasKey: boolean
}

function toDraft(entry: ProviderEntry, services: SettingsStatePayload['services']): Draft {
  // 地址与内置服务商一致时回填成下拉值，用户改配置时不用重新填地址。
  const service = services.find(
    item => item.provider === entry.provider && item.baseUrl === entry.baseUrl,
  )
  return {
    name: entry.name,
    service: service?.name ?? '',
    provider: entry.provider,
    baseUrl: entry.baseUrl,
    model: entry.model,
    apiKey: '',
    hasKey: entry.hasKey,
  }
}

function blankDraft(): Draft {
  return { name: '', service: '', provider: '', baseUrl: '', model: '', apiKey: '', hasKey: false }
}

function toPayload(draft: Draft): ModelDraft {
  const base: ModelDraft = {
    name: draft.name.trim() || draft.model.trim(),
    model: draft.model.trim(),
    api_key: draft.apiKey.trim() || null,
  }
  if (draft.service) return { ...base, service: draft.service }
  return { ...base, provider: draft.provider.trim(), base_url: draft.baseUrl.trim() }
}

/** 三个主题选项：图标 + 名称，选中态跟随「已保存的偏好」而非解析后的颜色。 */
const THEMES: readonly { id: ThemePreference; label: string; Icon: typeof SunIcon }[] = [
  { id: 'light', label: '浅色', Icon: SunIcon },
  { id: 'dark', label: '深色', Icon: MoonIcon },
  { id: 'system', label: '跟随系统', Icon: MonitorIcon },
]

function ThemeRow() {
  const preference = useSyncExternalStore(themeStore.subscribe, themeStore.getPreference)

  return (
    <div className="settings-row">
      <span className="settings-label">主题</span>
      <div className="theme-cubes">
        {THEMES.map(({ id, label, Icon }) => (
          <button
            key={id}
            type="button"
            className={`theme-cube${preference === id ? ' theme-cube-active' : ''}`}
            aria-pressed={preference === id}
            onClick={() => setPreference(id)}
          >
            <Icon />
            <span>{label}</span>
          </button>
        ))}
      </div>
    </div>
  )
}

/** 头部按钮：打开设置。 */
function SettingsToggle({ store }: { store: SettingsApi }) {
  const state = useSyncExternalStore(store.subscribe, store.getState)

  return (
    <button
      type="button"
      className={`sidebar-toggle${state.open ? ' sidebar-toggle-active' : ''}`}
      title="设置"
      onClick={state.open ? store.close : store.open}
    >
      <SlidersIcon />
    </button>
  )
}

function SettingsPanel({ store }: { store: SettingsApi }) {
  const state = useSyncExternalStore(store.subscribe, store.getState)
  const data = state.data
  const [drafts, setDrafts] = useState<Draft[]>([])
  const [seeded, setSeeded] = useState<SettingsStatePayload | null>(null)
  const [savedAt, setSavedAt] = useState<number | null>(null)

  // 每次拿到新的服务端快照就重置草稿（保存成功后也是这条路径）。
  useEffect(() => {
    if (!data || data === seeded) return
    setDrafts(data.models.map(entry => toDraft(entry, data.services)))
    setSeeded(data)
  }, [data, seeded])

  // 保存成功后收起面板；失败时留在原地显示错误。
  useEffect(() => {
    if (!state.open || savedAt === null || !data || state.pending || state.error) return
    const timer = setTimeout(() => store.close(), 600)
    return () => clearTimeout(timer)
  }, [state.open, savedAt, data, state.pending, state.error, store])

  // 收起后清掉「已保存」标记，否则下次打开会立刻被上面的定时器关掉。
  useEffect(() => {
    if (!state.open) setSavedAt(null)
  }, [state.open])

  if (!state.open) return null

  const patch = (index: number, value: Partial<Draft>) => {
    setDrafts(items => items.map((item, at) => (at === index ? { ...item, ...value } : item)))
    setSavedAt(null)
  }

  const add = () => {
    setDrafts(items => [...items, blankDraft()])
    setSavedAt(null)
  }

  const remove = (index: number) => {
    setDrafts(items => items.filter((_, at) => at !== index))
    setSavedAt(null)
  }

  return (
    <div className="dialog-backdrop" onClick={store.close}>
      <div className="settings" onClick={event => event.stopPropagation()}>
        <div className="dialog-head">
          <span>设置</span>
          <span className="sidebar-spacer" />
          <button type="button" className="sidebar-toggle" title="关闭" onClick={store.close}>
            ×
          </button>
        </div>

        <div className="settings-body">
          <section className="settings-section">
            <h3>外观</h3>
            <ThemeRow />
            <div className="settings-row">
              <span className="settings-label">字号</span>
              <span className="settings-hint">随窗口宽度自动缩放（16–20px），无需手动设置</span>
            </div>
          </section>

          <section className="settings-section">
            <h3>模型服务商</h3>
            <p className="settings-hint">
              每行是一个可切换的目标；切换只重建模型连接，同一会话的历史继续使用。
              API Key 留空表示沿用当前 key，且不会回显已保存的值。
            </p>

            <div className="settings-table">
              {drafts.map((draft, index) => {
                const service = data?.services.find(item => item.name === draft.service)
                return (
                  <div className="settings-model" key={index}>
                    <div className="settings-model-row">
                      <input
                        className="settings-input settings-name"
                        value={draft.name}
                        placeholder="显示名（如 DeepSeek Chat）"
                        onChange={event => patch(index, { name: event.target.value })}
                      />
                      <button
                        type="button"
                        className="sidebar-toggle"
                        title="删除这一行"
                        onClick={() => remove(index)}
                      >
                        <TrashIcon />
                      </button>
                    </div>
                    <div className="settings-model-row">
                      <select
                        className="settings-input"
                        value={draft.service}
                        onChange={event => {
                          const name = event.target.value
                          const picked = data?.services.find(item => item.name === name)
                          patch(index, {
                            service: name,
                            provider: picked?.provider ?? draft.provider,
                            baseUrl: picked?.baseUrl ?? draft.baseUrl,
                          })
                        }}
                      >
                        <option value="">自定义（手填 provider 与地址）</option>
                        {data?.services.map(item => (
                          <option key={item.name} value={item.name}>
                            {item.name} · {item.provider}
                          </option>
                        ))}
                      </select>
                      <input
                        className="settings-input"
                        value={draft.model}
                        placeholder="模型名，如 deepseek-chat"
                        onChange={event => patch(index, { model: event.target.value })}
                      />
                    </div>
                    {!draft.service && (
                      <div className="settings-model-row">
                        <input
                          className="settings-input"
                          value={draft.provider}
                          placeholder="provider：openai_compat / anthropic / gemini / openai_responses"
                          onChange={event => patch(index, { provider: event.target.value })}
                        />
                        <input
                          className="settings-input"
                          value={draft.baseUrl}
                          placeholder="base_url：https://..."
                          onChange={event => patch(index, { baseUrl: event.target.value })}
                        />
                      </div>
                    )}
                    <div className="settings-model-row">
                      <input
                        className="settings-input"
                        type="password"
                        value={draft.apiKey}
                        placeholder={draft.hasKey ? '已配置独立 key（留空不改）' : 'API Key（留空沿用默认）'}
                        onChange={event => patch(index, { apiKey: event.target.value })}
                      />
                      <span className="settings-model-meta">
                        {service ? `${service.provider} · ${service.baseUrl}` : draft.provider || '自定义'}
                      </span>
                    </div>
                  </div>
                )
              })}

              {drafts.length === 0 && <div className="explorer-empty">还没有候选模型</div>}
            </div>

            <div className="settings-actions">
              <button type="button" className="settings-add" onClick={add}>
                添加模型
              </button>
              <span className="sidebar-spacer" />
              <button type="button" className="settings-reload" onClick={store.reload}>
                重新读取
              </button>
              <button
                type="button"
                className="primary"
                disabled={state.pending}
                onClick={() => {
                  store.save(drafts.map(toPayload))
                  setSavedAt(Date.now())
                }}
              >
                {state.pending ? '保存中…' : '保存'}
              </button>
            </div>

            {state.error && <div className="explorer-error">{state.error}</div>}
            {savedAt !== null && !state.error && !state.pending && (
              <div className="settings-ok">已保存</div>
            )}
          </section>

          <section className="settings-section">
            <h3>配置文件</h3>
            <code className="dialog-path">{data?.path ?? '读取中…'}</code>
            <p className="settings-hint">
              当前模型：{data?.current.name ?? '-'}（{data?.current.provider} · {data?.current.model}）
            </p>
          </section>
        </div>
      </div>
    </div>
  )
}

export const settingsPlugin: Plugin = {
  name: 'settings',
  inject: ['connection', 'slots'],
  apply(ctx) {
    const store = createSettings(ctx.connection)

    ctx.effect(() =>
      ctx.slots.inject('shell.head-actions', () =>
        ctx.slots.register('shell.head-actions', {
          id: 'settings-toggle',
          component: () => <SettingsToggle store={store} />,
        }),
      ),
    )
    ctx.effect(() =>
      ctx.slots.inject('app.overlay', () =>
        ctx.slots.register('app.overlay', {
          id: 'settings-panel',
          component: () => <SettingsPanel store={store} />,
        }),
      ),
    )
  },
}
