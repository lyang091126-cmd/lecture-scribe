<script setup>
import { onBeforeUnmount, onMounted, ref, watch } from 'vue'
import * as echarts from 'echarts/core'
import { LineChart } from 'echarts/charts'
import { GridComponent, MarkLineComponent, TooltipComponent } from 'echarts/components'
import { CanvasRenderer } from 'echarts/renderers'

echarts.use([LineChart, GridComponent, TooltipComponent, MarkLineComponent, CanvasRenderer])

const props = defineProps({
  curve: { type: Array, default: () => [] },
  initialCapital: { type: Number, default: 0 },
})

const el = ref(null)
let chart = null
let observer = null

// 权益曲线波动通常只有百分之几，交给 ECharts 自动缩放会把曲线压成一条直线，
// 这里按数据范围（含初始资金基准线）做带留白的自适应
function yBounds(points, initialCapital) {
  const values = points.map((p) => p[1])
  if (initialCapital) values.push(initialCapital)
  if (!values.length) return { min: undefined, max: undefined }
  const min = Math.min(...values)
  const max = Math.max(...values)
  const pad = Math.max((max - min) * 0.15, Math.abs(max) * 0.0015, 1)
  return { min: Math.floor(min - pad), max: Math.ceil(max + pad) }
}

function render() {
  if (!chart) return
  // 多标的并行回测时，各自的模拟时钟会交叉写入曲线，这里按时间排序并去重，
  // 否则 time 轴会把折线来回拉成锯齿
  const merged = new Map()
  for (const point of props.curve) merged.set(Math.round(point.ts * 1000), point.equity)
  const points = [...merged.entries()].sort((a, b) => a[0] - b[0])
  const last = points.length ? points[points.length - 1][1] : props.initialCapital
  const up = last >= props.initialCapital
  const bounds = yBounds(points, props.initialCapital)
  const color = up ? '#ef4d56' : '#22c55e'
  chart.setOption({
    animation: false,
    grid: { top: 16, right: 16, bottom: 28, left: 64 },
    tooltip: {
      trigger: 'axis',
      backgroundColor: 'rgba(13,18,32,0.95)',
      borderColor: '#2a3450',
      textStyle: { color: '#e2e8f0', fontSize: 12 },
      valueFormatter: (v) => Number(v).toLocaleString('zh-CN', { maximumFractionDigits: 2 }),
    },
    xAxis: {
      type: 'time',
      axisLine: { lineStyle: { color: '#2a3450' } },
      axisLabel: { color: '#64748b', fontSize: 11, hideOverlap: true },
      splitLine: { show: false },
    },
    yAxis: {
      type: 'value',
      min: bounds.min,
      max: bounds.max,
      axisLabel: {
        color: '#64748b',
        fontSize: 11,
        formatter: (v) => Number(v).toLocaleString('zh-CN', { maximumFractionDigits: 0 }),
      },
      splitLine: { lineStyle: { color: 'rgba(42,52,80,0.55)' } },
    },
    series: [
      {
        name: '账户权益',
        type: 'line',
        showSymbol: false,
        smooth: 0.2,
        lineStyle: { width: 2, color },
        areaStyle: {
          color: new echarts.graphic.LinearGradient(0, 0, 0, 1, [
            { offset: 0, color: up ? 'rgba(239,77,86,0.35)' : 'rgba(34,197,94,0.35)' },
            { offset: 1, color: 'rgba(8,11,20,0.02)' },
          ]),
        },
        data: points,
        markLine: props.initialCapital
          ? {
              silent: true,
              symbol: 'none',
              lineStyle: { color: '#64748b', type: 'dashed', width: 1 },
              label: { formatter: '初始资金', position: 'insideStartTop', color: '#64748b', fontSize: 10 },
              data: [{ yAxis: props.initialCapital }],
            }
          : undefined,
      },
    ],
  })
}

onMounted(() => {
  chart = echarts.init(el.value, null, { renderer: 'canvas' })
  render()
  observer = new ResizeObserver(() => chart && chart.resize())
  observer.observe(el.value)
})

onBeforeUnmount(() => {
  if (observer) observer.disconnect()
  if (chart) chart.dispose()
  chart = null
})

watch(() => props.curve, render, { deep: false })
watch(() => props.initialCapital, render)
</script>

<template>
  <div class="relative h-[260px] w-full">
    <div ref="el" class="h-full w-full"></div>
    <div v-if="!curve.length" class="absolute inset-0 grid place-items-center text-sm text-slate-500">
      暂无资金曲线数据，点击「启动」开始回测或模拟盘
    </div>
  </div>
</template>
