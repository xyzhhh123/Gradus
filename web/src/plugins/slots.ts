import type { Plugin } from '@deepseek-ai/cordis'
import { Slots } from '../slots/registry'

export const slotsPlugin: Plugin = {
  name: 'slots',
  apply(ctx) {
    ctx.provide('slots', new Slots())
  },
}
