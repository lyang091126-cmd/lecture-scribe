<script setup>
import { computed } from 'vue'
import { fmtNum, fmtPct, pnlClass } from '../format'

const props = defineProps({ stats: { type: Object, default: () => ({}) }, jev: { type: Object, default: () => ({}) } })

const cards = computed(() => {
  const s = props.stats
  return [
    { label: '账户权益', value: fmtNum(s.equity), sub: `初始 ${fmtNum(s.initial_capital, 0)}`, cls: pnlClass(s.total_return_pct) },
    { label: '累计收益率', value: fmtPct(s.total_return_pct), sub: `已实现 ${fmtNum(s.realized_pnl)}`, cls: pnlClass(s.total_return_pct) },
    { label: '胜率', value: fmtPct(s.win_rate, 1), sub: `${s.win_trades ?? 0} 胜 / ${s.loss_trades ?? 0} 负`, cls: 'text-slate-100' },
    { label: '成交笔数', value: fmtNum(s.total_fills, 0), sub: `平仓 ${s.closed_trades ?? 0} 笔`, cls: 'text-slate-100' },
    { label: '盈亏比', value: fmtNum(s.payoff_ratio, 2), sub: `盈利因子 ${fmtNum(s.profit_factor, 2)}`, cls: 'text-slate-100' },
    { label: '夏普比率', value: fmtNum(s.sharpe, 2), sub: `最大回撤 ${fmtPct(s.max_drawdown_pct, 2)}`, cls: pnlClass(s.sharpe) },
    { label: '浮动盈亏', value: fmtNum(s.unrealized_pnl), sub: `占用保证金 ${fmtNum(s.margin_used, 0)}`, cls: pnlClass(s.unrealized_pnl) },
    {
      label: 'Jev 决策',
      value: fmtNum(props.jev.decisions ?? 0, 0),
      sub:
        props.jev.mode === 'live'
          ? `在线 ${props.jev.model} · ${props.jev.calls ?? 0} 次调用`
          : '本地 Mock 动量模型',
      cls: props.jev.mode === 'live' ? 'text-accent' : 'text-amber-400',
    },
  ]
})
</script>

<template>
  <div class="grid grid-cols-2 gap-3 md:grid-cols-4 xl:grid-cols-8">
    <div v-for="card in cards" :key="card.label" class="card px-3 py-2.5">
      <div class="text-[11px] text-slate-400">{{ card.label }}</div>
      <div class="num mt-1 text-lg font-semibold" :class="card.cls">{{ card.value }}</div>
      <div class="truncate text-[11px] text-slate-500">{{ card.sub }}</div>
    </div>
  </div>
</template>
