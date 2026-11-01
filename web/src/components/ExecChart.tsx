/** 看板的图：Apache ECharts（Canvas 渲染、按需引入）在浏览器里画，一张图一个指标，指标多时用下拉换；明细在下面的表格里 */
import { Flex, Select, Statistic, Typography } from 'antd'
import { BarChart, LineChart, PieChart } from 'echarts/charts'
import { GridComponent, LegendComponent, TooltipComponent } from 'echarts/components'
import * as echarts from 'echarts/core'
import { CanvasRenderer } from 'echarts/renderers'
import { useEffect, useMemo, useRef, useState } from 'react'

import type { ExecSummary } from '../api/types'
import { MAX_BARS, chartPoints, type ChartKind, type ChartPlan, type ChartPoint } from '../utils/chartPlan'
import { formatCell } from '../utils/format'

echarts.use([BarChart, LineChart, PieChart, GridComponent, LegendComponent, TooltipComponent, CanvasRenderer])

const SERIES_COLOR = '#2a78d6'
const CATEGORY_COLORS = ['#2a78d6', '#eb6834', '#1baf7a', '#eda100', '#e87ba4', '#008300', '#6250d6', '#e34948']
const TEXT_COLOR = '#52514e'
const GRID_COLOR = '#ecebe7'
const BAR_ROW_PX = 28
const AXIS_HEIGHT_PX = 300
const PIE_HEIGHT_PX = 320
const MIN_HEIGHT_PX = 160
const MAX_SYMBOLS = 40          // 点多时不画圆点，只留线
const MAX_VALUE_LABELS = 12     // 超过这么多根时不在柱子上写数，看提示框

const numberText = (value: unknown) => (typeof value === 'number' ? formatCell(value) : '空')
const valueLabel = ({ value }: { value: unknown }) => numberText(value)

function axisTooltip() {
  return { trigger: 'axis' as const, axisPointer: { type: 'line' as const, lineStyle: { color: '#b5b3ab' } }, valueFormatter: numberText }
}

const valueAxis = { type: 'value' as const, splitLine: { lineStyle: { color: GRID_COLOR } }, axisLabel: { color: TEXT_COLOR } }

function categoryAxis(points: ChartPoint[]) {
  return {
    type: 'category' as const,
    data: points.map((point) => point.label),
    axisLine: { lineStyle: { color: GRID_COLOR } },
    axisTick: { show: false },
  }
}

function lineOption(points: ChartPoint[], name: string) {
  return {
    animation: false,
    grid: { left: 8, right: 24, top: 16, bottom: 8, containLabel: true },
    tooltip: axisTooltip(),
    xAxis: { ...categoryAxis(points), axisLabel: { color: TEXT_COLOR, hideOverlap: true } },
    yAxis: valueAxis,
    series: [{
      name,
      type: 'line' as const,
      data: points.map((point) => point.value),
      showSymbol: points.length <= MAX_SYMBOLS,
      symbolSize: 8,
      lineStyle: { width: 2, color: SERIES_COLOR },
      itemStyle: { color: SERIES_COLOR, borderColor: '#fff', borderWidth: 2 },
    }],
  }
}

function barSeries(points: ChartPoint[], name: string, position: 'right' | 'top', borderRadius: number[], barMaxWidth: number) {
  return {
    name,
    type: 'bar' as const,
    data: points.map((point) => point.value),
    barMaxWidth,
    itemStyle: { color: SERIES_COLOR, borderRadius },
    label: { show: points.length <= MAX_VALUE_LABELS, position, color: TEXT_COLOR, formatter: valueLabel },
  }
}

function barOption(points: ChartPoint[], name: string) {
  return {
    animation: false,
    grid: { left: 8, right: 64, top: 8, bottom: 8, containLabel: true },
    tooltip: axisTooltip(),
    xAxis: valueAxis,
    yAxis: { ...categoryAxis(points), inverse: true, axisLabel: { color: TEXT_COLOR, width: 200, overflow: 'truncate' as const } },
    series: [barSeries(points, name, 'right', [0, 4, 4, 0], 14)],
  }
}

function columnOption(points: ChartPoint[], name: string) {
  return {
    animation: false,
    grid: { left: 8, right: 16, top: 24, bottom: 8, containLabel: true },
    tooltip: axisTooltip(),
    xAxis: { ...categoryAxis(points), axisLabel: { color: TEXT_COLOR, hideOverlap: true, width: 96, overflow: 'truncate' as const } },
    yAxis: valueAxis,
    series: [barSeries(points, name, 'top', [4, 4, 0, 0], 32)],
  }
}

function pieOption(points: ChartPoint[], name: string) {
  return {
    animation: false,
    color: CATEGORY_COLORS,
    tooltip: { trigger: 'item' as const, valueFormatter: numberText },
    legend: { bottom: 0, type: 'scroll' as const, textStyle: { color: TEXT_COLOR } },
    series: [{
      name,
      type: 'pie' as const,
      radius: ['42%', '68%'],
      center: ['50%', '45%'],
      data: points.map((point) => ({ name: point.label, value: point.value })),
      itemStyle: { borderColor: '#fff', borderWidth: 2 },
      label: { color: TEXT_COLOR, formatter: '{b} {d}%' },
    }],
  }
}

function optionOf(kind: ChartKind, points: ChartPoint[], name: string) {
  if (kind === 'line') return lineOption(points, name)
  if (kind === 'column') return columnOption(points, name)
  return kind === 'pie' ? pieOption(points, name) : barOption(points, name)
}

function Canvas({ option, height }: { option: echarts.EChartsCoreOption; height: number }) {
  const box = useRef<HTMLDivElement>(null)
  useEffect(() => {
    if (!box.current) return undefined
    const chart = echarts.init(box.current)
    chart.setOption(option)
    const observer = new ResizeObserver(() => chart.resize())     // 标签页切回来时才有尺寸
    observer.observe(box.current)
    return () => {
      observer.disconnect()
      chart.dispose()
    }
  }, [option])
  return <div ref={box} className="exec-chart-canvas" style={{ height }} />
}

export default function ExecChart({ exec, plan, kind }: { exec: ExecSummary; plan: ChartPlan; kind: ChartKind }) {
  const [valueIndex, setValueIndex] = useState(plan.valueIndexes[0])
  const name = exec.columns[valueIndex]
  const all = useMemo(() => chartPoints(exec, plan, valueIndex, kind), [exec, plan, valueIndex, kind])
  const points = useMemo(() => (kind === 'bar' || kind === 'column' ? all.slice(0, MAX_BARS) : all), [all, kind])
  const option = useMemo(() => optionOf(kind, points, name), [kind, points, name])

  if (kind === 'metric') {
    return (
      <Flex wrap gap={32} className="exec-metrics">
        {plan.valueIndexes.map((index) => (
          <Statistic key={index} title={exec.columns[index]} value={formatCell(exec.rows[0][index])} />
        ))}
      </Flex>
    )
  }
  const labelName = plan.labelIndexes.map((index) => exec.columns[index]).join(' / ')
  const height = kind === 'bar' ? Math.max(MIN_HEIGHT_PX, points.length * BAR_ROW_PX + 40) : kind === 'pie' ? PIE_HEIGHT_PX : AXIS_HEIGHT_PX
  return (
    <div className="exec-chart">
      <Flex justify="space-between" align="center" wrap gap={8} className="exec-chart-head">
        <Typography.Text strong>{name}<Typography.Text type="secondary">（按 {labelName}）</Typography.Text></Typography.Text>
        {plan.valueIndexes.length > 1 && (
          <Select size="small" value={valueIndex} onChange={setValueIndex} popupMatchSelectWidth={false} aria-label="换指标"
                  options={plan.valueIndexes.map((index) => ({ value: index, label: exec.columns[index] }))} />
        )}
      </Flex>
      <Canvas option={option} height={height} />
      {all.length > points.length && (
        <Typography.Text type="secondary">共 {all.length} 行，图上只画前 {points.length} 行，其余看表格</Typography.Text>
      )}
    </div>
  )
}
