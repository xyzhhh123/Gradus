import type { ConnectionApi } from './plugins/connection'
import type { SessionsApi } from './plugins/sessions'
import type { Slots } from './slots/registry'

/**
 * 把服务挂到 `ctx` 的类型上。
 * 运行时由各插件在 `apply` 里 `ctx.provide(name, value)` 注册。
 */
declare module '@deepseek-ai/cordis' {
  interface Context {
    connection: ConnectionApi
    sessions: SessionsApi
    slots: Slots
  }
}
