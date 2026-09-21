// 全局引擎状态：SSE 实时事件 + 快照轮询兜底。
import { computed, reactive } from 'vue'
import { api } from '../api'

const MAX_DECISIONS = 80
const MAX_TRADES = 300
const MAX_LOGS = 60

export const state = reactive({
  connected: false,
  loading: true,
  error: null,
  snapshot: null,
  config: null,
  presets: [],
  decisions: [],
  trades: [],
  logs: [],
  lastEventAt: 0,
})

let source = null
let pollTimer = null

function pushLog(level, message) {
  if (!message) return
  state.logs.unshift({ id: `${Date.now()}-${Math.random()}`, ts: Date.now(), level, message })
  if (state.logs.length > MAX_LOGS) state.logs.length = MAX_LOGS
}

function onDecision(event) {
  // SSE 重放 + 首屏拉取可能带来重复事件
  if (event.id && state.decisions.some((d) => d.id === event.id)) return
  state.decisions.unshift(event)
  if (state.decisions.length > MAX_DECISIONS) state.decisions.length = MAX_DECISIONS
}

function onTrade(event) {
  if (!event.trade) return
  const exists = state.trades.some((t) => t.id === event.trade.id && t.ts === event.trade.ts)
  if (exists) return
  state.trades.unshift(event.trade)
  if (state.trades.length > MAX_TRADES) state.trades.length = MAX_TRADES
  if (state.snapshot && event.stats) state.snapshot.stats = event.stats
  const pnl = event.trade.realized_pnl
  pushLog(
    pnl > 0 ? 'win' : pnl < 0 ? 'loss' : 'info',
    `${event.trade.symbol} ${event.trade.action} @${event.trade.price} × ${event.trade.qty}` +
      (event.trade.action.startsWith('close') ? `，盈亏 ${pnl.toFixed(2)}` : ''),
  )
}

function handle(type, payload) {
  state.lastEventAt = Date.now()
  if (type === 'decision') onDecision(payload)
  else if (type === 'trade') onTrade(payload)
  else if (type === 'status') {
    if (state.snapshot) state.snapshot.status = payload.status
    pushLog('info', payload.message)
    refresh()
  } else if (type === 'feed') pushLog('info', payload.message)
  else if (type === 'order') pushLog('info', payload.message || `${payload.symbol} 挂单 ${payload.side} @${payload.limit_price}`)
  else if (type === 'error') pushLog('loss', payload.message)
}

export function connect() {
  if (source) source.close()
  source = new EventSource(api.eventsUrl())
  source.onopen = () => {
    state.connected = true
    state.error = null
  }
  source.onerror = () => {
    state.connected = false // EventSource 会自动重连
  }
  for (const type of ['decision', 'trade', 'status', 'feed', 'order', 'error']) {
    source.addEventListener(type, (evt) => {
      try {
        handle(type, JSON.parse(evt.data))
      } catch (err) {
        console.warn('无法解析 SSE 事件', type, err)
      }
    })
  }
}

export function disconnect() {
  if (source) source.close()
  source = null
  state.connected = false
  if (pollTimer) clearInterval(pollTimer)
  pollTimer = null
}

export async function refresh() {
  try {
    state.snapshot = await api.snapshot()
    state.error = null
  } catch (err) {
    state.error = err.message
  } finally {
    state.loading = false
  }
}

export async function bootstrap() {
  try {
    const cfg = await api.config()
    state.config = cfg.config
    state.presets = cfg.presets
    const [logs, decisions] = await Promise.all([api.tradeLogs(200), api.decisions(60)])
    state.trades = logs.trades
    state.decisions = decisions.decisions
  } catch (err) {
    state.error = err.message
  }
  await refresh()
  connect()
  if (pollTimer) clearInterval(pollTimer)
  pollTimer = setInterval(refresh, 2500) // 资金曲线/持仓兜底刷新
}

export async function sendAction(action) {
  try {
    const res = await api.action(action)
    pushLog('info', `控制命令 ${action} 已执行（状态：${res.status}）`)
    if (action === 'reset') {
      state.trades = []
      state.decisions = []
    }
    await refresh()
    return res
  } catch (err) {
    state.error = err.message
    pushLog('loss', `命令 ${action} 失败：${err.message}`)
    throw err
  }
}

export async function applyConfig(patch) {
  const res = await api.updateConfig(patch)
  state.config = res.config
  pushLog('info', `参数已更新：${(res.changed || []).join(', ') || '无变化'}`)
  if (res.restarted) {
    state.trades = []
    state.decisions = []
  }
  await refresh()
  return res
}

export const stats = computed(() => state.snapshot?.stats ?? {})
export const engineStatus = computed(() => state.snapshot?.status ?? 'idle')
export const jevInfo = computed(() => state.snapshot?.jev ?? { mode: 'mock', calls: 0 })
export const positions = computed(() => state.snapshot?.positions ?? [])
export const feeds = computed(() => state.snapshot?.feeds ?? [])
export const equityCurve = computed(() => state.snapshot?.equity_curve ?? [])
