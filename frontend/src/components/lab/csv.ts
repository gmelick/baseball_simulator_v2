/**
 * csv.ts — SIM-439
 * Client-side CSV export for a result set (kept out of the ResultGrid component
 * file so react-refresh's only-export-components rule stays happy).
 */
export function downloadCsv(columns: string[], rows: unknown[][], filename = 'query_result.csv'): void {
  const esc = (v: unknown): string => {
    if (v == null) return ''
    const s = String(v)
    return /[",\n]/.test(s) ? `"${s.replace(/"/g, '""')}"` : s
  }
  const lines = [columns.map(esc).join(','), ...rows.map((r) => r.map(esc).join(','))]
  const blob = new Blob([lines.join('\n')], { type: 'text/csv;charset=utf-8;' })
  const url = URL.createObjectURL(blob)
  const a = document.createElement('a')
  a.href = url
  a.download = filename
  a.click()
  URL.revokeObjectURL(url)
}
