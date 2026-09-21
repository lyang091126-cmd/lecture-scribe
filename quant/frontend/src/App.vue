<script setup>
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'
import ControlPanel from './components/ControlPanel.vue'
import DecisionFeed from './components/DecisionFeed.vue'
import EquityChart from './components/EquityChart.vue'
import PositionPanel from './components/PositionPanel.vue'
import StatCards from './components/StatCards.vue'
import TradeTable from './components/TradeTable.vue'
import {
  applyConfig,
  bootstrap,
  disconnect,
  engineStatus,
  equityCurve,
  feeds,
  jevInfo,
  positions,
  sendAction,
  state,
  stats,
} from './composables/useEngine'

const panelOpen = ref(false)
const busy = ref(false)

const statusMeta = computed(() => {
  const map = {
    idle: ['bg-slate-500/15 text-slate-300', '空闲'],
    running: ['bg-emerald-500/15 text-emerald-400', '运行中'],
    paused: ['bg-amber-500/15 text-amber-400', '已暂停'],
    finished: ['bg-accent/15 text-accent', '已完成'],
    halted: ['bg-red-500/15 text-red-400', '风控熔断'],
    error: ['bg-red-500/15 text-red-400', '异常'],
  }
  return map[engineStatus.value] || map.idle
})

const modeLabel = computed(() => (state.snapshot?.mode === 'paper' ? '实时模拟盘' : '历史回测'))

async function run(action) {
  busy.value = true
  try {
    await sendAction(action)
  } finally {
    busy.value = false
  }
}

async function onApply(patch) {
  busy.value = true
  try {
    await applyConfig(patch)
    panelOpen.value = false
  } finally {
    busy.value = false
  }
}

onMounted(bootstrap)
onBeforeUnmount(disconnect)
</script>

<template>
  <div class="flex h-full flex-col gap-3 p-3 lg:p-4">
    <!-- 顶部状态栏 -->
    <header class="card flex flex-wrap items-center gap-3 px-4 py-2.5">
      <h1 class="text-base font-semibold text-slate-100">
        JEV 量化交易大屏
        <span class="ml-1 text-xs font-normal text-slate-500">Jev 决策大模型 · 美股 / 国内商品期货</span>
      </h1>

      <div class="flex flex-wrap items-center gap-2 text-xs">
        <span class="badge" :class="statusMeta[0]">● {{ statusMeta[1] }}</span>
        <span class="badge bg-ink-600 text-slate-300">{{ modeLabel }}</span>
        <span class="badge" :class="jevInfo.mode === 'live' ? 'bg-accent/15 text-accent' : 'bg-amber-500/15 text-amber-400'">
          {{ jevInfo.mode === 'live' ? `JEV 在线 · ${jevInfo.model}` : 'JEV 降级 · 本地 Mock' }}
        </span>
        <span class="badge" :class="state.connected ? 'bg-emerald-500/15 text-emerald-400' : 'bg-down/15 text-down'">
          SSE {{ state.connected ? '已连接' : '重连中' }}
        </span>
        <span class="num badge bg-ink-600 text-slate-400">
          决策 {{ state.snapshot?.engine?.total_decisions ?? 0 }} · 拦截 {{ state.snapshot?.engine?.rejected_signals ?? 0 }}
        </span>
      </div>

      <div class="ml-auto flex items-center gap-2">
        <button class="btn-primary" :disabled="busy || engineStatus === 'running'" @click="run('start')">▶ 启动</button>
        <button
          class="btn-ghost"
          :disabled="busy || !['running', 'paused'].includes(engineStatus)"
          @click="run(engineStatus === 'paused' ? 'resume' : 'pause')"
        >
          {{ engineStatus === 'paused' ? '⏵ 继续' : '⏸ 暂停' }}
        </button>
        <button class="btn-ghost" :disabled="busy" @click="run('flatten')">一键平仓</button>
        <button class="btn-ghost" :disabled="busy" @click="run('reset')">⟲ 重置</button>
        <button class="btn-ghost" @click="panelOpen = true">⚙ 控制面板</button>
      </div>
    </header>

    <p v-if="state.error" class="rounded-lg border border-red-500/40 bg-red-500/10 px-3 py-2 text-xs text-red-400">
      后端连接异常：{{ state.error }}
    </p>

    <StatCards :stats="stats" :jev="jevInfo" />

    <!-- 主体 -->
    <div class="grid min-h-0 flex-1 grid-cols-1 gap-3 xl:grid-cols-3">
      <div class="flex min-h-0 flex-col gap-3 xl:col-span-2">
        <section class="card">
          <div class="card-title">
            <span>资金曲线</span>
            <span class="num text-xs font-normal text-slate-500">
              {{ (state.snapshot?.symbols || []).join(' · ') }}
            </span>
          </div>
          <EquityChart :curve="equityCurve" :initial-capital="stats.initial_capital || 0" />
        </section>

        <section class="min-h-[320px] flex-1">
          <TradeTable :trades="state.trades" />
        </section>
      </div>

      <div class="grid min-h-0 grid-rows-2 gap-3">
        <DecisionFeed :decisions="state.decisions" />
        <PositionPanel :positions="positions" :feeds="feeds" :logs="state.logs" />
      </div>
    </div>

    <ControlPanel
      :open="panelOpen"
      :config="state.config"
      :presets="state.presets"
      :busy="busy"
      @close="panelOpen = false"
      @apply="onApply"
    />
  </div>
</template>
