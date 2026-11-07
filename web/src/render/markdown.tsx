import { Children, isValidElement, useState, type ReactNode } from 'react'
import ReactMarkdown from 'react-markdown'
import rehypeHighlight from 'rehype-highlight'
import remarkGfm from 'remark-gfm'
import { CheckIcon, CopyIcon } from '../icons'

function textOf(node: ReactNode): string {
  if (typeof node === 'string') return node
  if (typeof node === 'number') return String(node)
  if (Array.isArray(node)) return node.map(textOf).join('')
  if (isValidElement<{ children?: ReactNode }>(node)) return textOf(node.props.children)
  return ''
}

function CopyButton({ value }: { value: string }) {
  const [done, setDone] = useState(false)

  return (
    <button
      type="button"
      className="copy"
      onClick={() => {
        void navigator.clipboard.writeText(value).then(() => {
          setDone(true)
          setTimeout(() => setDone(false), 1200)
        })
      }}
    >
      {done ? <CheckIcon /> : <CopyIcon />}
      <span>{done ? '已复制' : '复制'}</span>
    </button>
  )
}

function CodeBlock({ children }: { children?: ReactNode }) {
  const first = Children.toArray(children)[0]
  const className = isValidElement<{ className?: string }>(first) ? first.props.className ?? '' : ''
  const language = /language-([\w-]+)/.exec(className)?.[1] ?? 'text'

  return (
    <div className="code-block">
      <div className="code-banner">
        <span>{language}</span>
        <CopyButton value={textOf(children).replace(/\n$/, '')} />
      </div>
      <pre>{children}</pre>
    </div>
  )
}

export function Markdown({ text }: { text: string }) {
  return (
    <div className="markdown">
      <ReactMarkdown
        remarkPlugins={[remarkGfm]}
        rehypePlugins={[rehypeHighlight]}
        components={{ pre: CodeBlock }}
      >
        {text}
      </ReactMarkdown>
    </div>
  )
}
