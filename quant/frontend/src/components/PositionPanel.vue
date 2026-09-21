<script setup>
import { fmtNum, fmtPct, pnlClass } from '../format'

defineProps({
  positions: { type: Array, default: () => [] },
  feeds: { type: Array, default: () => [] },
  logs: { type: Array, default: () => [] },
})
</script>

<template>
  <div class="card flex h-full flex-col">
    <div class="card-title"><span>持仓 / 行情源 / 运行日志</span></div>

    <div class="space-y-3 overflow-y-auto p-3">
      <section>
        <h3 class="mb-1.5 text-[11px] uppercase tracking-wide text-slate-500">当前持仓</h3>
        <div v-if="!positions.length" class="rounded-lg border border-dashed border-ink-600 px-3 py-3 text-xs text-slate-500">
          当前空仓
        </div>
        <div
          v-for="p in positions"
          :key="p.symbol"
          class="mb-1.5 flex items-center justify-between rounded-lg border border-ink-600 bg-ink-900/60 px-3 py-2 text-xs"
        >
          <div>
            <div class="num font-medium text-slate-100">{{ p.symbol }}</div>
            <div class="num text-[11px] text-slate-500">
              均价 {{ fmtNum(p.avg_price) }} → 现价 {{ fmtNum(p.mark_price) }} · {{ fmtNum(p.qty, 0) }} 手/股
            </div>
          </div>
          <div class="text-right">
            <span class="badge" :class="p.side === 'long' ? 'bg-up/15 text-up' : 'bg-down/15 text-down'">
              {{ p.side === 'long' ? '多头' : '空头' }}
            </span>
            <div class="num mt-0.5 text-[11px]" :class="pnlClass(p.unrealized_pnl)">
              {{ fmtNum(p.unrealized_pnl) }} ({{ fmtPct(p.unrealized_pnl_pct) }})
            </div>
          </div>
        </div>
      </section>

      <section>
        <h3 class="mb-1.5 text-[11px] uppercase tracking-wide text-slate-500">行情源</h3>
        <div
          v-for="f in feeds"
          :key="f.symbol"
          class="mb-1.5 rounded-lg border border-ink-600 bg-ink-900/60 px-3 py-2 text-[11px]"
        >
          <div class="flex items-center justify-between">
            <span class="num font-medium text-slate-200">{{ f.symbol }}</span>
            <span class="badge bg-ink-600 text-slate-300">
              {{ f.market === 'cn_future' ? '商品期货' : '美股' }} · {{ f.source }}
            </span>
          </div>
          <div class="num mt-1 text-slate-500">
            现价 {{ fmtNum(f.last_price) }} · 价差 {{ f.spread ?? '--' }} · Tick {{ f.ticks }} ·
            Jev 调用 {{ f.jev_calls }}
            <span v-if="f.pending_order" class="text-amber-400">
              · 挂单 {{ f.pending_order.side }} @{{ f.pending_order.limit_price }}
            </span>
          </div>
        </div>
        <div v-if="!feeds.length" class="text-xs text-slate-500">引擎未运行</div>
      </section>

      <section>
        <h3 class="mb-1.5 text-[11px] uppercase tracking-wide text-slate-500">运行日志</h3>
        <ul class="space-y-1">
          <li
            v-for="log in logs.slice(0, 20)"
            :key="log.id"
            class="num text-[11px] leading-4"
            :class="log.level === 'win' ? 'text-up' : log.level === 'loss' ? 'text-down' : 'text-slate-400'"
          >
            <span class="text-slate-600">{{ new Date(log.ts).toLocaleTimeString('zh-CN', { hour12: false }) }}</span>
            {{ log.message }}
          </li>
          <li v-if="!logs.length" class="text-xs text-slate-500">暂无日志</li>
        </ul>
      </section>
    </div>
  </div>
</template>
