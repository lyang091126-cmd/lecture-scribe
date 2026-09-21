<script setup>
import { computed, ref } from 'vue'
import { ACTION_COLORS, ACTION_LABELS, fmtTime } from '../format'

const props = defineProps({ decisions: { type: Array, default: () => [] } })
const onlyExecuted = ref(false)
const symbolFilter = ref('')

const symbols = computed(() => [...new Set(props.decisions.map((d) => d.symbol))])
const rows = computed(() =>
  props.decisions
    .filter((d) => (onlyExecuted.value ? d.outcome?.executed : true))
    .filter((d) => (symbolFilter.value ? d.symbol === symbolFilter.value : true))
    .slice(0, 40),
)

const order = ['open_long', 'open_short', 'close_position', 'hold']
</script>

<template>
  <div class="card flex h-full flex-col">
    <div class="card-title">
      <span>Jev 实时决策流</span>
      <div class="flex items-center gap-2 text-[11px] font-normal text-slate-400">
        <select v-model="symbolFilter" class="input !w-auto !py-0.5 !text-[11px]">
          <option value="">全部标的</option>
          <option v-for="s in symbols" :key="s" :value="s">{{ s }}</option>
        </select>
        <label class="flex cursor-pointer items-center gap-1">
          <input v-model="onlyExecuted" type="checkbox" class="accent-[#5b8cff]" />
          仅看触发交易
        </label>
      </div>
    </div>

    <div class="min-h-0 flex-1 space-y-2 overflow-y-auto p-3">
      <div v-if="!rows.length" class="py-10 text-center text-sm text-slate-500">
        等待 Jev 决策事件…
      </div>

      <article
        v-for="d in rows"
        :key="`${d.id}-${d.symbol}-${d.tick_ts}`"
        class="rounded-lg border border-ink-600 bg-ink-900/60 p-2.5"
        :class="d.outcome?.executed ? 'border-l-2 border-l-accent' : ''"
      >
        <header class="flex flex-wrap items-center gap-2 text-xs">
          <span class="num font-semibold text-slate-100">{{ d.symbol }}</span>
          <span class="badge" :style="{ background: `${ACTION_COLORS[d.top_action]}22`, color: ACTION_COLORS[d.top_action] }">
            {{ ACTION_LABELS[d.top_action] || d.top_action }} {{ (d.top_prob * 100).toFixed(1) }}%
          </span>
          <span class="badge bg-ink-600 text-slate-300">置信度 {{ Number(d.confidence).toFixed(2) }}</span>
          <span
            class="badge"
            :class="d.source === 'jev' ? 'bg-accent/20 text-accent' : 'bg-amber-500/15 text-amber-400'"
            :title="d.error || ''"
          >
            {{ d.source === 'jev' ? `JEV ${d.model}` : 'Mock 降级' }}
          </span>
          <span class="num ml-auto text-[11px] text-slate-500">{{ fmtTime(d.tick_ts) }} · {{ d.latency_ms }}ms</span>
        </header>

        <div class="mt-2 grid grid-cols-2 gap-x-4 gap-y-1">
          <div v-for="action in order" :key="action" class="flex items-center gap-2">
            <span class="w-9 shrink-0 text-[11px] text-slate-400">{{ ACTION_LABELS[action] }}</span>
            <div class="h-1.5 flex-1 overflow-hidden rounded bg-ink-600">
              <div
                class="h-full rounded transition-all"
                :style="{
                  width: `${((d.probabilities?.[action] || 0) * 100).toFixed(1)}%`,
                  background: ACTION_COLORS[action],
                }"
              ></div>
            </div>
            <span class="num w-11 shrink-0 text-right text-[11px] text-slate-400">
              {{ ((d.probabilities?.[action] || 0) * 100).toFixed(1) }}%
            </span>
          </div>
        </div>

        <footer class="mt-2 space-y-1 text-[11px]">
          <div class="num text-slate-500">
            盘口 {{ d.state?.orderbook?.bid_price_1 }} / {{ d.state?.orderbook?.ask_price_1 }}
            · 价差 {{ d.state?.orderbook?.spread }}
            · RSI {{ d.state?.indicators?.rsi_14?.toFixed?.(1) }}
            · MACD {{ d.state?.indicators?.macd_histogram?.toFixed?.(3) }}
            · 持仓 {{ d.state?.current_position?.side }}
          </div>
          <div :class="d.outcome?.executed ? 'text-accent' : 'text-slate-500'">
            {{ d.outcome?.executed ? '✅ 已触发：' : '⛔ 未触发：' }}{{ d.outcome?.reason }}
            <span class="text-slate-600">
              （开仓阈值 {{ (d.thresholds?.buy_threshold * 100).toFixed(0) }}% / 置信度
              {{ (d.thresholds?.min_confidence * 100).toFixed(0) }}%）
            </span>
          </div>
        </footer>
      </article>
    </div>
  </div>
</template>
