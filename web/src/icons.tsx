import type { ReactNode, SVGProps } from 'react'

type Props = SVGProps<SVGSVGElement>

function Line({ children, ...props }: Props & { children: ReactNode }) {
  return (
    <svg
      width="16"
      height="16"
      viewBox="0 0 16 16"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.5"
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
      {...props}
    >
      {children}
    </svg>
  )
}

export function ChevronIcon(props: Props) {
  return (
    <Line {...props}>
      <path d="M6 3.5 10.5 8 6 12.5" />
    </Line>
  )
}

export function CopyIcon(props: Props) {
  return (
    <Line {...props}>
      <rect x="5.5" y="5.5" width="8" height="8" rx="1.5" />
      <path d="M10.5 3.5H4a1 1 0 0 0-1 1V11" />
    </Line>
  )
}

export function CheckIcon(props: Props) {
  return (
    <Line {...props}>
      <path d="M3.5 8.5 6.5 11.5 12.5 5" />
    </Line>
  )
}

export function TerminalIcon(props: Props) {
  return (
    <Line {...props}>
      <path d="M3 5.5 6 8l-3 2.5" />
      <path d="M8.5 11h4.5" />
    </Line>
  )
}

export function FileIcon(props: Props) {
  return (
    <Line {...props}>
      <path d="M4 2.5h5l3 3v8H4z" />
      <path d="M9 2.5v3h3" />
    </Line>
  )
}

export function FolderIcon(props: Props) {
  return (
    <Line {...props}>
      <path d="M2.5 4.5h4l1.5 2h5.5v6.5h-11z" />
    </Line>
  )
}

export function SearchIcon(props: Props) {
  return (
    <Line {...props}>
      <circle cx="7" cy="7" r="3.5" />
      <path d="m9.8 9.8 2.7 2.7" />
    </Line>
  )
}

export function PencilIcon(props: Props) {
  return (
    <Line {...props}>
      <path d="M11 2.8 13.2 5 6 12.2l-3 .8.8-3z" />
    </Line>
  )
}

export function TrashIcon(props: Props) {
  return (
    <Line {...props}>
      <path d="M3 4.5h10M6 4.5V3h4v1.5M4.5 4.5l.6 8.2h5.8l.6-8.2" />
    </Line>
  )
}

export function PlusIcon(props: Props) {
  return (
    <Line {...props}>
      <path d="M8 3.5v9M3.5 8h9" />
    </Line>
  )
}

export function SendIcon(props: Props) {
  return (
    <Line {...props}>
      <path d="M8 13V3.5M4 7.2 8 3.2l4 4" />
    </Line>
  )
}

export function StopIcon(props: Props) {
  return (
    <svg width="16" height="16" viewBox="0 0 16 16" aria-hidden="true" {...props}>
      <rect x="4.5" y="4.5" width="7" height="7" rx="1.4" fill="currentColor" />
    </svg>
  )
}

export function SparkIcon(props: Props) {
  return (
    <Line {...props}>
      <path d="M8 2.5 9.3 6.2 13 7.5l-3.7 1.3L8 12.5 6.7 8.8 3 7.5l3.7-1.3z" />
    </Line>
  )
}

export function GearIcon(props: Props) {
  return (
    <Line {...props}>
      <circle cx="8" cy="8" r="2.2" />
      <path d="M8 2.2v1.4M8 12.4v1.4M2.2 8h1.4M12.4 8h1.4M3.9 3.9l1 1M11.1 11.1l1 1M12.1 3.9l-1 1M4.9 11.1l-1 1" />
    </Line>
  )
}

/** 浅色主题。 */
export function SunIcon(props: Props) {
  return (
    <Line {...props}>
      <circle cx="8" cy="8" r="2.8" />
      <path d="M8 1.8v1.6M8 12.6v1.6M1.8 8h1.6M12.6 8h1.6M3.6 3.6l1.1 1.1M11.3 11.3l1.1 1.1M12.4 3.6l-1.1 1.1M4.7 11.3l-1.1 1.1" />
    </Line>
  )
}

/** 深色主题。 */
export function MoonIcon(props: Props) {
  return (
    <Line {...props}>
      <path d="M13 9.6A5.4 5.4 0 0 1 6.4 3a5.4 5.4 0 1 0 6.6 6.6z" />
    </Line>
  )
}

/** 跟随系统主题。 */
export function MonitorIcon(props: Props) {
  return (
    <Line {...props}>
      <rect x="2.2" y="3.2" width="11.6" height="7.6" rx="1.2" />
      <path d="M6 13.2h4M8 10.8v2.4" />
    </Line>
  )
}

export function toolIcon(name: string) {
  const lower = name.toLowerCase()
  if (lower.includes('bash') || lower.includes('shell') || lower.includes('powershell')) {
    return TerminalIcon
  }
  if (lower.includes('grep') || lower.includes('search')) return SearchIcon
  if (lower.includes('glob') || lower.includes('list') || lower.includes('dir')) return FolderIcon
  if (lower.includes('delete') || lower.includes('remove')) return TrashIcon
  if (lower.includes('write') || lower.includes('edit') || lower.includes('patch')) return PencilIcon
  return FileIcon
}
