// 后端 API 封装。开发模式下走 Vite 代理（/api -> 127.0.0.1:8000），
// 生产模式下前端由 FastAPI 同源托管，路径保持一致。
const BASE = import.meta.env.VITE_API_BASE || ''

async function request(path, options = {}) {
  const resp = await fetch(`${BASE}${path}`, {
    headers: { 'Content-Type': 'application/json' },
    ...options,
  })
  const text = await resp.text()
  const data = text ? JSON.parse(text) : null
  if (!resp.ok) {
    const detail = data && data.detail ? data.detail : `HTTP ${resp.status}`
    throw new Error(typeof detail === 'string' ? detail : JSON.stringify(detail))
  }
  return data
}

export const api = {
  snapshot: () => request('/api/snapshot'),
  tradeLogs: (limit = 200, symbol = null) =>
    request(`/api/trade_logs?limit=${limit}${symbol ? `&symbol=${encodeURIComponent(symbol)}` : ''}`),
  decisions: (limit = 60) => request(`/api/decisions?limit=${limit}`),
  config: () => request('/api/config'),
  updateConfig: (patch) =>
    request('/api/control/config', { method: 'POST', body: JSON.stringify(patch) }),
  action: (action) =>
    request('/api/control/action', { method: 'POST', body: JSON.stringify({ action }) }),
  eventsUrl: () => `${BASE}/api/events?replay=30`,
}
