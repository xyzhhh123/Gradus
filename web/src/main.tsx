import { Context, type Plugin } from '@deepseek-ai/cordis'
import './styles.css'
import { approvalPlugin } from './plugins/approval'
import { composerPlugin } from './plugins/composer'
import { connectionPlugin } from './plugins/connection'
import { conversationPlugin } from './plugins/conversation'
import { explorerPlugin } from './plugins/explorer'
import { layoutPlugin } from './plugins/layout'
import { mockConnectionPlugin } from './plugins/mock-connection'
import { rendererPlugin } from './plugins/renderer'
import { sessionsPlugin } from './plugins/sessions'
import { settingsPlugin } from './plugins/settings'
import { slotsPlugin } from './plugins/slots'
import { toolBashPlugin } from './plugins/tool-bash'
import { toolFsPlugin } from './plugins/tool-fs'

const useMock = new URLSearchParams(location.search).has('mock')

/**
 * 注册顺序在这里没有语义：谁先启动由 inject 决定。
 * 换掉一个插件（例如 mock 连接）不影响其余任何一块。
 */
const plugins: Plugin[] = [
  useMock ? mockConnectionPlugin : connectionPlugin,
  slotsPlugin,
  sessionsPlugin,
  layoutPlugin,
  conversationPlugin,
  composerPlugin,
  approvalPlugin,
  toolBashPlugin,
  toolFsPlugin,
  explorerPlugin,
  settingsPlugin,
  rendererPlugin,
]

const ctx = new Context()
const fibers = plugins.map(plugin => ({ name: plugin.name ?? '(anonymous)', fiber: ctx.plugin(plugin) }))

const STATE_NAMES = ['PENDING', 'LOADING', 'ACTIVE', 'FAILED', 'DISPOSED', 'UNLOADING']

// 启动状态表：某个功能没出现时，先看这里是不是 PENDING、缺哪个服务。
setTimeout(() => {
  console.table(
    fibers.map(({ name, fiber }) => ({
      plugin: name,
      state: STATE_NAMES[fiber.state] ?? fiber.state,
      missing:
        Object.keys(fiber.inject)
          .filter(dependency => ctx.get(dependency, true) === undefined)
          .join(', ') || '-',
    })),
  )
}, 100)
