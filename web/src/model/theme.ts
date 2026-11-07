/**
 * 主题偏好：浅色 / 深色 / 跟随系统。
 *
 * 纯前端展示状态，只读写 `<html data-theme>` 与 localStorage，不经 host：
 * 换个主题和会话、配置无关，没必要往返一次。`:root` 是浅色，
 * `:root[data-theme='dark']` 只覆盖颜色。
 *
 * 首帧的主题由 index.html 里的内联脚本先定好（避免闪一下），
 * 这里负责之后的切换与持久化，两边共用同一个存储键。
 */

export type ThemePreference = 'light' | 'dark' | 'system'

/** 与 index.html 内联脚本共用的存储键。 */
export const THEME_STORAGE_KEY = 'gradus.theme'

const DARK_QUERY = '(prefers-color-scheme: dark)'

function isPreference(value: unknown): value is ThemePreference {
  return value === 'light' || value === 'dark' || value === 'system'
}

/** 读回上次的选择；坏值或读不到时按「跟随系统」。 */
export function readPreference(): ThemePreference {
  try {
    const stored = localStorage.getItem(THEME_STORAGE_KEY)
    if (isPreference(stored)) return stored
  } catch {
    /* 隐私模式下 localStorage 可能不可用，退回默认值即可 */
  }
  return 'system'
}

function prefersDark(): boolean {
  return window.matchMedia(DARK_QUERY).matches
}

/** 把偏好落到 DOM：`system` 看系统，其余直接用自身。 */
export function applyPreference(preference: ThemePreference): void {
  const dark = preference === 'dark' || (preference === 'system' && prefersDark())
  document.documentElement.dataset.theme = dark ? 'dark' : 'light'
}

let preference = readPreference()
const listeners = new Set<() => void>()

function notify(): void {
  for (const listener of [...listeners]) listener()
}

/** 切换偏好并立即生效；失败只影响这次是否记得住。 */
export function setPreference(next: ThemePreference): void {
  if (next === preference) return
  preference = next
  try {
    localStorage.setItem(THEME_STORAGE_KEY, next)
  } catch {
    /* 存不下就只影响下次启动 */
  }
  applyPreference(next)
  notify()
}

// 「跟随系统」时，系统切换要跟着变；已明确选了浅/深则不理会。
window.matchMedia(DARK_QUERY).addEventListener('change', () => {
  if (preference === 'system') applyPreference('system')
})

/** 供 useSyncExternalStore 订阅：只暴露偏好本身，不暴露解析后的颜色。 */
export const themeStore = {
  subscribe(listener: () => void): () => void {
    listeners.add(listener)
    return () => listeners.delete(listener)
  },
  getPreference: (): ThemePreference => preference,
}
