import { useEffect, useRef, useState } from 'react'
import type { Plugin } from '@deepseek-ai/cordis'
import type { ModelInfo } from '../protocol/frames'
import type { RootProps } from '../render/types'
import { CheckIcon, ChevronIcon, SendIcon, StopIcon } from '../icons'

const MAX_HEIGHT = 200

/** 模型选择器：贴在输入框左下的触发器 + 向上展开的列表。 */
function ModelSelect({
  models,
  activeId,
  label,
  disabled,
  onSelect,
}: {
  models: ModelInfo[]
  activeId: string | null
  label: string
  disabled: boolean
  onSelect(id: string): void
}) {
  const [open, setOpen] = useState(false)
  const root = useRef<HTMLDivElement>(null)

  useEffect(() => {
    if (!open) return
    const close = (event: PointerEvent) => {
      if (!root.current?.contains(event.target as Node)) setOpen(false)
    }
    document.addEventListener('pointerdown', close)
    return () => document.removeEventListener('pointerdown', close)
  }, [open])

  return (
    <div className="model-select" ref={root}>
      <button
        type="button"
        className={`model-trigger${open ? ' model-trigger-open' : ''}`}
        title={disabled ? '生成中不能切换模型' : '切换模型'}
        disabled={disabled}
        aria-haspopup="listbox"
        aria-expanded={open}
        onClick={() => setOpen(value => !value)}
      >
        <span className="model-name">{label}</span>
        <ChevronIcon className="model-chevron" />
      </button>

      {open && (
        <div className="model-menu" role="listbox">
          {models.map(model => {
            const active = model.id === activeId
            return (
              <button
                key={model.id}
                type="button"
                role="option"
                aria-selected={active}
                className={`model-option${active ? ' model-option-active' : ''}`}
                onClick={() => {
                  setOpen(false)
                  if (!active) onSelect(model.id)
                }}
              >
                <span className="model-option-main">
                  <span className="model-option-name">{model.name}</span>
                  <span className="model-option-sub">
                    {model.provider} · {model.model}
                  </span>
                </span>
                {active && <CheckIcon className="model-check" />}
              </button>
            )
          })}
        </div>
      )}
    </div>
  )
}

function ComposerBlock({ state, actions }: RootProps) {
  const [text, setText] = useState('')
  const input = useRef<HTMLTextAreaElement>(null)
  const running = state.status === 'running'
  const offline = state.connection !== 'open'

  const resize = () => {
    const element = input.current
    if (!element) return
    element.style.height = 'auto'
    element.style.height = `${Math.min(element.scrollHeight, MAX_HEIGHT)}px`
  }

  const submit = () => {
    const value = text.trim()
    if (!value || running || offline) return
    actions.prompt(value)
    setText('')
    requestAnimationFrame(() => {
      resize()
      input.current?.focus()
    })
  }

  return (
    <form
      className="composer"
      onSubmit={event => {
        event.preventDefault()
        submit()
      }}
    >
      <textarea
        ref={input}
        value={text}
        rows={1}
        placeholder={offline ? '连接中…' : '给 Gradus 发送消息…'}
        onChange={event => {
          setText(event.target.value)
          resize()
        }}
        onKeyDown={event => {
          if (event.key !== 'Enter' || event.shiftKey || event.nativeEvent.isComposing) return
          event.preventDefault()
          submit()
        }}
      />

      <div className="composer-bar">
        <div className="composer-meta">
          <ModelSelect
            models={state.models}
            activeId={state.activeModelId}
            label={state.model?.name ?? '模型'}
            disabled={running || state.models.length === 0}
            onSelect={actions.selectModel}
          />
          <span className="composer-hint">Enter 发送 · Shift+Enter 换行</span>
        </div>
        {running ? (
          <button type="button" className="round" title="停止" onClick={actions.cancel}>
            <StopIcon />
          </button>
        ) : (
          <button
            type="submit"
            className="round round-primary"
            title="发送"
            disabled={offline || text.trim() === ''}
          >
            <SendIcon />
          </button>
        )}
      </div>
    </form>
  )
}

export const composerPlugin: Plugin = {
  name: 'composer',
  inject: ['slots'],
  apply(ctx) {
    ctx.effect(() =>
      ctx.slots.inject('shell.blocks', () =>
        ctx.slots.register('shell.blocks', {
          id: 'composer',
          order: 30,
          component: ComposerBlock,
        }),
      ),
    )
  },
}
