export const fmtNum = (v, digits = 2) =>
  v === null || v === undefined || Number.isNaN(v)
    ? '--'
    : Number(v).toLocaleString('zh-CN', { minimumFractionDigits: digits, maximumFractionDigits: digits })

export const fmtPct = (v, digits = 2) => (v === null || v === undefined ? '--' : `${Number(v).toFixed(digits)}%`)

export const fmtTime = (ts) => {
  if (!ts) return '--'
  const d = new Date(ts * (ts > 1e11 ? 1 : 1000))
  return d.toLocaleTimeString('zh-CN', { hour12: false }) + '.' + String(d.getMilliseconds()).padStart(3, '0')
}

export const fmtDateTime = (ts) => {
  if (!ts) return '--'
  const d = new Date(ts * (ts > 1e11 ? 1 : 1000))
  return `${d.getMonth() + 1}/${d.getDate()} ${d.toLocaleTimeString('zh-CN', { hour12: false })}`
}

// 中国习惯：涨红跌绿
export const pnlClass = (v) => (v > 0 ? 'text-up' : v < 0 ? 'text-down' : 'text-slate-400')

export const ACTION_LABELS = {
  open_long: '开多',
  open_short: '开空',
  close_position: '平仓',
  hold: '观望',
  close_long: '平多',
  close_short: '平空',
}

export const ACTION_COLORS = {
  open_long: '#ef4d56',
  open_short: '#22c55e',
  close_position: '#f59e0b',
  hold: '#64748b',
}
