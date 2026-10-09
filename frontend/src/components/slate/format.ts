/**
 * format.ts — the Daily Diamond text helpers (SIM-519).
 *
 * Small pure functions the slate card and its sections share: ordinals,
 * short names, American odds, run lines, local start times, and the
 * YYYY-MM-DD calendar arithmetic of the date navigator (local dates, no UTC
 * drift).
 */

/** 1 → "1st", 2 → "2nd", 11 → "11th". */
export function ordinal(n: number): string {
  const s = ['th', 'st', 'nd', 'rd']
  const v = n % 100
  return `${n}${s[(v - 20) % 10] ?? s[v] ?? s[0]}`
}

/** "Francisco Lindor" → "F. Lindor". A one-word name stays as it is. */
export function shortName(name: string | null | undefined): string {
  if (!name) return ''
  const parts = name.trim().split(/\s+/)
  return parts.length < 2 ? name : `${parts[0][0]}. ${parts.slice(1).join(' ')}`
}

/** "Francisco Lindor" → "Lindor". */
export function lastName(name: string | null | undefined): string {
  if (!name) return ''
  const parts = name.trim().split(/\s+/)
  return parts.length < 2 ? name : parts.slice(1).join(' ')
}

/** An American price: 130 → "+130", -145 → "-145". */
export function american(price: number): string {
  const p = Math.round(price)
  return p > 0 ? `+${p}` : String(p)
}

/** A run line or spread: 1.5 → "+1.5", -1.5 → "-1.5". */
export function signedLine(line: number): string {
  return line > 0 ? `+${line}` : String(line)
}

/** The local first-pitch time: "7:05 PM". */
export function localStartTime(startUtc: string | null | undefined): string | null {
  if (!startUtc) return null
  const d = new Date(startUtc)
  if (Number.isNaN(d.getTime())) return null
  return d.toLocaleTimeString([], { hour: 'numeric', minute: '2-digit' })
}

/** "2h ago", "5m ago", "3d ago". */
export function ageLabel(iso: string | null | undefined, now: number = Date.now()): string | null {
  if (!iso) return null
  const t = new Date(iso).getTime()
  if (Number.isNaN(t)) return null
  const mins = Math.max(0, Math.round((now - t) / 60000))
  if (mins < 60) return `${mins}m ago`
  const hours = Math.round(mins / 60)
  if (hours < 48) return `${hours}h ago`
  return `${Math.round(hours / 24)}d ago`
}

// ---------------------------------------------------------------------------
// Calendar dates (YYYY-MM-DD, local)
// ---------------------------------------------------------------------------

export const ISO_DATE_RE = /^\d{4}-\d{2}-\d{2}$/

const pad2 = (n: number): string => String(n).padStart(2, '0')

export function toIso(d: Date): string {
  return `${d.getFullYear()}-${pad2(d.getMonth() + 1)}-${pad2(d.getDate())}`
}

export function parseIso(iso: string): Date {
  const [y, m, d] = iso.split('-').map(Number)
  return new Date(y, m - 1, d)
}

export function todayIso(): string {
  return toIso(new Date())
}

export function shiftIso(iso: string, delta: number): string {
  const d = parseIso(iso)
  d.setDate(d.getDate() + delta)
  return toIso(d)
}

/** "Fri, Oct 9" (or with the weekday and year on a wide screen). */
export function shortDateLabel(iso: string, wide: boolean): string {
  return parseIso(iso).toLocaleDateString(
    [],
    wide ? { weekday: 'short', month: 'short', day: 'numeric', year: 'numeric' } : { month: 'short', day: 'numeric' },
  )
}

/** "Friday, October 9". */
export function longDateLabel(iso: string): string {
  return parseIso(iso).toLocaleDateString([], { weekday: 'long', month: 'long', day: 'numeric' })
}

/** The kicker over the date: Today, Yesterday, Tomorrow, else the year. */
export function relativeDayLabel(iso: string, today: string = todayIso()): string {
  if (iso === today) return 'Today'
  if (iso === shiftIso(today, -1)) return 'Yesterday'
  if (iso === shiftIso(today, 1)) return 'Tomorrow'
  return String(parseIso(iso).getFullYear())
}
