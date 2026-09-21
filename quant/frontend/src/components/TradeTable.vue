<script setup>
import { computed, ref } from 'vue'
import { ACTION_LABELS, fmtDateTime, fmtNum, pnlClass } from '../format'

const props = defineProps({ trades: { type: Array, default: () => [] } })
const keyword = ref('')

const rows = computed(() => {
  const kw = keyword.value.trim().toLowerCase()
  const list = kw
    ? props.trades.filter(
        (t) => t.symbol.toLowerCase().includes(kw) || (t.trigger || '').toLowerCase().includes(kw),
      )
    : props.trades
  return list.slice(0, 200)
})

const reasonBadge = {
  jev_signal: ['bg-accent/15 text-accent', 'Jev 信号'],
  take_profit: ['bg-up/15 text-up', '止盈'],
  stop_loss: ['bg-down/15 text-down', '止损'],
  flatten: ['bg-slate-500/15 text-slate-300', '强平'],
  risk_halt: ['bg-amber-500/15 text-amber-400', '风控熔断'],
}
</script>

<template>
  <div class="card flex h-full flex-col">
    <div class="card-title">
      <span>成交明细 <span class="text-xs font-normal text-slate-500">({{ trades.length }})</span></span>
      <input v-model="keyword" class="input !w-44 !py-1 !text-xs" placeholder="搜索标的 / 触发条件" />
    </div>
    <div class="min-h-0 flex-1 overflow-auto">
      <table class="w-full border-collapse text-xs">
        <thead class="sticky top-0 z-10 bg-ink-700/95 text-[11px] uppercase text-slate-400">
          <tr>
            <th class="px-3 py-2 text-left font-medium">时间</th>
            <th class="px-3 py-2 text-left font-medium">标的</th>
            <th class="px-3 py-2 text-left font-medium">动作</th>
            <th class="px-3 py-2 text-right font-medium">价格</th>
            <th class="px-3 py-2 text-right font-medium">数量</th>
            <th class="px-3 py-2 text-right font-medium">盈亏</th>
            <th class="px-3 py-2 text-left font-medium">触发条件</th>
          </tr>
        </thead>
        <tbody>
          <tr v-if="!rows.length">
            <td colspan="7" class="px-3 py-10 text-center text-slate-500">暂无成交记录</td>
          </tr>
          <tr v-for="t in rows" :key="t.id" class="border-t border-ink-700/70 hover:bg-ink-700/40">
            <td class="num whitespace-nowrap px-3 py-1.5 text-slate-400">{{ fmtDateTime(t.ts) }}</td>
            <td class="num whitespace-nowrap px-3 py-1.5 font-medium text-slate-200">{{ t.symbol }}</td>
            <td class="whitespace-nowrap px-3 py-1.5">
              <span
                class="badge"
                :class="t.side === 'buy' ? 'bg-up/15 text-up' : 'bg-down/15 text-down'"
              >
                {{ ACTION_LABELS[t.action] || t.action }} · {{ t.side === 'buy' ? '买' : '卖' }}
              </span>
            </td>
            <td class="num px-3 py-1.5 text-right text-slate-200">{{ fmtNum(t.price, 2) }}</td>
            <td class="num px-3 py-1.5 text-right text-slate-300">{{ fmtNum(t.qty, 0) }}</td>
            <td class="num px-3 py-1.5 text-right" :class="pnlClass(t.realized_pnl)">
              {{ t.action.startsWith('open') ? '--' : fmtNum(t.realized_pnl, 2) }}
            </td>
            <td class="px-3 py-1.5">
              <div class="flex items-start gap-2">
                <span class="badge shrink-0" :class="(reasonBadge[t.reason_code] || ['bg-ink-600 text-slate-300', t.reason_code])[0]">
                  {{ (reasonBadge[t.reason_code] || ['', t.reason_code])[1] }}
                </span>
                <span class="num text-[11px] leading-4 text-slate-400">{{ t.trigger }}</span>
              </div>
            </td>
          </tr>
        </tbody>
      </table>
    </div>
  </div>
</template>
