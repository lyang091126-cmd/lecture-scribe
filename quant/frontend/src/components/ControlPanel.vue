<script setup>
import { computed, reactive, ref, watch } from 'vue'

const props = defineProps({
  open: { type: Boolean, default: false },
  config: { type: Object, default: null },
  presets: { type: Array, default: () => [] },
  busy: { type: Boolean, default: false },
})
const emit = defineEmits(['close', 'apply'])

const draft = reactive({})
const customSymbol = ref('')
const error = ref('')

watch(
  () => props.config,
  (cfg) => {
    if (cfg) Object.assign(draft, JSON.parse(JSON.stringify(cfg)))
  },
  { immediate: true, deep: true },
)

const allSymbols = computed(() => {
  const preset = props.presets.map((p) => p.symbol)
  const extra = (draft.symbols || []).filter((s) => !preset.includes(s))
  return [...preset, ...extra]
})

function symbolMeta(symbol) {
  return props.presets.find((p) => p.symbol === symbol) || { name: symbol, market: symbol.includes('.') ? 'cn_future' : 'us_equity' }
}

function toggleSymbol(symbol) {
  const list = draft.symbols || []
  draft.symbols = list.includes(symbol) ? list.filter((s) => s !== symbol) : [...list, symbol]
}

function addCustom() {
  const value = customSymbol.value.trim()
  if (!value) return
  if (!(draft.symbols || []).includes(value)) draft.symbols = [...(draft.symbols || []), value]
  customSymbol.value = ''
}

const changed = computed(() => {
  if (!props.config) return {}
  const patch = {}
  for (const [key, value] of Object.entries(draft)) {
    if (JSON.stringify(value) !== JSON.stringify(props.config[key])) patch[key] = value
  }
  return patch
})

const restartFields = ['mode', 'symbols', 'initial_capital', 'backtest_ticks', 'replay_speed', 'commission_rate']
const willRestart = computed(() => Object.keys(changed.value).some((k) => restartFields.includes(k)))

async function apply() {
  error.value = ''
  if (!draft.symbols?.length) {
    error.value = '至少选择一个标的'
    return
  }
  try {
    await emit('apply', JSON.parse(JSON.stringify(changed.value)))
  } catch (err) {
    error.value = err.message
  }
}

function resetDraft() {
  if (props.config) Object.assign(draft, JSON.parse(JSON.stringify(props.config)))
  error.value = ''
}
</script>

<template>
  <transition name="fade">
    <div v-if="open" class="fixed inset-0 z-40 bg-black/50" @click="emit('close')"></div>
  </transition>
  <transition name="slide">
    <aside
      v-if="open"
      class="fixed right-0 top-0 z-50 flex h-full w-full max-w-md flex-col border-l border-ink-600 bg-ink-800 shadow-2xl"
    >
      <header class="flex items-center justify-between border-b border-ink-600 px-4 py-3">
        <div>
          <h2 class="text-sm font-semibold text-slate-100">后台控制面板</h2>
          <p class="text-[11px] text-slate-500">修改后点击「应用配置」生效，阈值类参数可热更新</p>
        </div>
        <button class="btn-ghost !px-2 !py-1" @click="emit('close')">✕</button>
      </header>

      <div v-if="draft.symbols" class="flex-1 space-y-5 overflow-y-auto px-4 py-4">
        <!-- 模式 -->
        <section>
          <h3 class="mb-2 text-xs font-semibold uppercase tracking-wide text-slate-400">运行模式</h3>
          <div class="grid grid-cols-2 gap-2">
            <button
              v-for="opt in [
                { value: 'backtest', label: '历史回测', desc: '全速推演历史 Tick' },
                { value: 'paper', label: '实时模拟盘', desc: '订阅实时盘口撮合' },
              ]"
              :key="opt.value"
              class="rounded-lg border px-3 py-2 text-left transition"
              :class="draft.mode === opt.value ? 'border-accent bg-accent/10 text-white' : 'border-ink-500 bg-ink-700 text-slate-300'"
              @click="draft.mode = opt.value"
            >
              <div class="text-sm font-medium">{{ opt.label }}</div>
              <div class="text-[11px] text-slate-500">{{ opt.desc }}</div>
            </button>
          </div>
        </section>

        <!-- 标的池 -->
        <section>
          <h3 class="mb-2 text-xs font-semibold uppercase tracking-wide text-slate-400">目标资产池</h3>
          <div class="flex flex-wrap gap-1.5">
            <button
              v-for="symbol in allSymbols"
              :key="symbol"
              class="badge border transition"
              :class="
                draft.symbols.includes(symbol)
                  ? 'border-accent bg-accent/15 text-accent'
                  : 'border-ink-500 bg-ink-700 text-slate-400'
              "
              :title="symbolMeta(symbol).name"
              @click="toggleSymbol(symbol)"
            >
              {{ symbol }}
              <span class="text-[10px] opacity-60">{{ symbolMeta(symbol).market === 'cn_future' ? '期货' : '美股' }}</span>
            </button>
          </div>
          <div class="mt-2 flex gap-2">
            <input
              v-model="customSymbol"
              class="input"
              placeholder="自定义代码，如 y2701.DCE / MSFT"
              @keyup.enter="addCustom"
            />
            <button class="btn-ghost" @click="addCustom">添加</button>
          </div>
        </section>

        <!-- Jev 阈值 -->
        <section class="space-y-3">
          <h3 class="text-xs font-semibold uppercase tracking-wide text-slate-400">Jev 决策阈值</h3>
          <div v-for="f in [
            { key: 'buy_threshold', label: '开仓概率阈值', min: 0.5, max: 0.95 },
            { key: 'close_threshold', label: '平仓概率阈值', min: 0.3, max: 0.95 },
            { key: 'min_confidence', label: '最低置信度', min: 0.3, max: 0.95 },
          ]" :key="f.key">
            <div class="field-label">
              <span>{{ f.label }}</span>
              <span class="num text-accent">{{ (draft[f.key] * 100).toFixed(0) }}%</span>
            </div>
            <input
              v-model.number="draft[f.key]"
              type="range"
              :min="f.min"
              :max="f.max"
              step="0.01"
              class="w-full accent-[#5b8cff]"
            />
          </div>
          <label class="flex items-center gap-2 text-xs text-slate-300">
            <input v-model="draft.jev_enabled" type="checkbox" class="accent-[#5b8cff]" />
            调用线上 Jev 模型（关闭则强制使用本地 Mock 动量模型）
          </label>
        </section>

        <!-- 风控 -->
        <section class="space-y-3">
          <h3 class="text-xs font-semibold uppercase tracking-wide text-slate-400">风险控制</h3>
          <div class="grid grid-cols-2 gap-3">
            <div>
              <div class="field-label"><span>止盈 (%)</span></div>
              <input v-model.number="draft.take_profit_pct" type="number" step="0.1" min="0" class="input num" />
            </div>
            <div>
              <div class="field-label"><span>止损 (%)</span></div>
              <input v-model.number="draft.stop_loss_pct" type="number" step="0.1" min="0" class="input num" />
            </div>
            <div>
              <div class="field-label"><span>最大价差</span></div>
              <input v-model.number="draft.max_spread" type="number" step="0.5" min="0" class="input num" />
            </div>
            <div>
              <div class="field-label"><span>防刷冷却 (秒)</span></div>
              <input v-model.number="draft.cooldown_sec" type="number" step="0.5" min="0" class="input num" />
            </div>
            <div>
              <div class="field-label"><span>每分钟交易上限</span></div>
              <input v-model.number="draft.max_trades_per_min" type="number" step="1" min="1" class="input num" />
            </div>
            <div>
              <div class="field-label"><span>最大回撤熔断 (%)</span></div>
              <input v-model.number="draft.max_daily_loss_pct" type="number" step="0.5" min="0" class="input num" />
            </div>
          </div>
        </section>

        <!-- 资金与撮合 -->
        <section class="space-y-3">
          <h3 class="text-xs font-semibold uppercase tracking-wide text-slate-400">资金与撮合</h3>
          <div class="grid grid-cols-2 gap-3">
            <div>
              <div class="field-label"><span>初始资金</span></div>
              <input v-model.number="draft.initial_capital" type="number" step="10000" min="1" class="input num" />
            </div>
            <div>
              <div class="field-label"><span>单笔数量</span></div>
              <input v-model.number="draft.order_qty" type="number" step="1" min="1" class="input num" />
            </div>
            <div>
              <div class="field-label"><span>下单方式</span></div>
              <select v-model="draft.order_type" class="input">
                <option value="ioc">IOC 吃单（对手价成交）</option>
                <option value="maker">Maker 挂单（买一/卖一排队）</option>
              </select>
            </div>
            <div>
              <div class="field-label"><span>手续费率</span></div>
              <input v-model.number="draft.commission_rate" type="number" step="0.0001" min="0" class="input num" />
            </div>
            <div>
              <div class="field-label"><span>决策间隔 (ms)</span></div>
              <input v-model.number="draft.decision_interval_ms" type="number" step="100" min="0" class="input num" />
            </div>
            <div>
              <div class="field-label"><span>回测 Tick 数</span></div>
              <input v-model.number="draft.backtest_ticks" type="number" step="500" min="10" class="input num" />
            </div>
          </div>
        </section>
      </div>

      <footer class="space-y-2 border-t border-ink-600 px-4 py-3">
        <p v-if="error" class="text-xs text-red-400">{{ error }}</p>
        <p v-else-if="willRestart" class="text-[11px] text-amber-400">
          ⚠️ 修改了模式 / 标的 / 资金类参数，应用后引擎将按新配置重启并清空账户
        </p>
        <p v-else-if="Object.keys(changed).length" class="text-[11px] text-slate-500">
          将热更新：{{ Object.keys(changed).join(', ') }}
        </p>
        <div class="flex gap-2">
          <button class="btn-primary flex-1 justify-center" :disabled="busy || !Object.keys(changed).length" @click="apply">
            应用配置
          </button>
          <button class="btn-ghost" :disabled="busy" @click="resetDraft">还原</button>
        </div>
      </footer>
    </aside>
  </transition>
</template>

<style scoped>
.fade-enter-active,
.fade-leave-active {
  transition: opacity 0.2s ease;
}
.fade-enter-from,
.fade-leave-to {
  opacity: 0;
}
.slide-enter-active,
.slide-leave-active {
  transition: transform 0.25s ease;
}
.slide-enter-from,
.slide-leave-to {
  transform: translateX(100%);
}
</style>
