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
  setPreset(preset: string): void
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
    setPreset(preset) {
      set({ pending: true, error: null })
      connection.send({ type: 'settings/preset', preset })
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
  /** 预算口径；空串表示「不写进配置」，按模型能力或默认值走。 */
  contextWindow: string
  maxOutputTokens: string
  /** 只读：模型能力上限与推导后的实际口径（未知时为 null）。 */
  windowLimit: number | null
  outputLimit: number | null
  effectiveWindow: number | null
  effectiveOutput: number | null
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
    contextWindow: entry.contextWindow ? String(entry.contextWindow) : '',
    maxOutputTokens: entry.maxOutputTokens ? String(entry.maxOutputTokens) : '',
    windowLimit: entry.windowLimit ?? null,
    outputLimit: entry.outputLimit ?? null,
    effectiveWindow: entry.effectiveWindow ?? null,
    effectiveOutput: entry.effectiveOutput ?? null,
  }
}

function blankDraft(): Draft {
  return {
    name: '',
    service: '',
    provider: '',
    baseUrl: '',
    model: '',
    apiKey: '',
    hasKey: false,
    contextWindow: '',
    maxOutputTokens: '',
    windowLimit: null,
    outputLimit: null,
    effectiveWindow: null,
    effectiveOutput: null,
  }
}

/** 空串或非法值时**不发**这个字段：发 null 会被后端「必须正整数」的校验拒掉。 */
function positive(value: string): number | undefined {
  const parsed = Number(value.trim())
  return Number.isFinite(parsed) && parsed > 0 ? Math.trunc(parsed) : undefined
}

function toPayload(draft: Draft): ModelDraft {
  const base: ModelDraft = {
    name: draft.name.trim() || draft.model.trim(),
    model: draft.model.trim(),
    api_key: draft.apiKey.trim() || null,
  }
  const window = positive(draft.contextWindow)
  const output = positive(draft.maxOutputTokens)
  if (window !== undefined) base.context_window = window
  if (output !== undefined) base.max_output_tokens = output
  if (draft.service) return { ...base, service: draft.service }
  return { ...base, provider: draft.provider.trim(), base_url: draft.baseUrl.trim() }
}

/** 大数用小写单位显示：1000000 → 1M，8192 → 8.2k。 */
function formatTokens(value: number | null | undefined): string {
  if (value == null || value <= 0) return '?'
  if (value >= 1_000_000) {
    const millions = value / 1_000_000
    return `${Number.isInteger(millions) ? millions : millions.toFixed(1)}M`
  }
  if (value >= 1_000) {
    const thousands = value / 1_000
    return `${Number.isInteger(thousands) ? thousands : thousands.toFixed(1)}k`
  }
  return String(value)
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
            <h3>上下文预算</h3>
            <p className="settings-hint">
              窗口是「按多大做上下文管理」，不是模型能力：留空按模型能力走，填了只往下压。
              触发线 = 窗口 − 预留，越过它才压缩。
            </p>
            <div className="settings-row">
              <span className="settings-label">保留量</span>
              <select
                className="settings-input settings-preset"
                value={data?.keepPreset ?? ''}
                disabled={state.pending}
                onChange={event => store.setPreset(event.target.value)}
              >
                {(data?.presets ?? []).map(preset => (
                  <option key={preset.name} value={preset.name}>
                    {preset.label}
                  </option>
                ))}
              </select>
              <span className="settings-hint settings-preset-hint">
                {(data?.presets ?? []).find(item => item.name === data?.keepPreset)?.detail ?? ''}
              </span>
            </div>
            <div className="settings-row">
              <span className="settings-label">当前</span>
              <span className="settings-budget">
                {data?.current.compactionThreshold
                  ? `窗口 ${formatTokens(data.current.windowLimit ?? data.current.contextWindow)} · ` +
                    `触发线 ${formatTokens(data.current.compactionThreshold)} · ` +
                    `保留 ${formatTokens(data.current.keepRecent)}`
                  : '未知（模型能力与窗口都没定）'}
              </span>
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
                      {draft.hasKey && <span className="settings-badge">已存 key</span>}
                    </div>
                    <div className="settings-model-row">
                      <label className="settings-field" title="按多大做上下文管理；留空按模型能力或默认值">
                        <span>窗口</span>
                        <input
                          className="settings-input settings-limit"
                          inputMode="numeric"
                          value={draft.contextWindow}
                          placeholder={'默认 ' + formatTokens(draft.windowLimit ?? draft.effectiveWindow)}
                          onChange={event => patch(index, { contextWindow: event.target.value })}
                        />
                      </label>
                      <label className="settings-field" title="每次请求的 max_output_tokens；留空用默认值">
                        <span>输出</span>
                        <input
                          className="settings-input settings-limit"
                          inputMode="numeric"
                          value={draft.maxOutputTokens}
                          placeholder={'默认 ' + formatTokens(draft.effectiveOutput)}
                          onChange={event => patch(index, { maxOutputTokens: event.target.value })}
                        />
                      </label>
                      <span className="settings-model-meta">
                        {draft.windowLimit
                          ? `上限 ${formatTokens(draft.windowLimit)} / ${formatTokens(draft.outputLimit)}`
                          : '上限未知'}
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

            {/* 配置文件与当前模型压成一行页脚：信息量小，不值得再占一节高度 */}
            <p className="settings-hint settings-footer">
              配置：<code>{data?.path ?? '读取中…'}</code>
              <span className="settings-footer-sep">·</span>
              当前 {data?.current.name ?? '-'}（{data?.current.model ?? '-'}）
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
