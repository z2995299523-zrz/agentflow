/** 执行结果的图：按字段自动选图（见 utils/chartPlan），一张图一个指标，指标多时用下拉换；明细在下面的表格里 */
import { Flex, Segmented, Select, Statistic, Typography } from 'antd'
import { BarChart, LineChart } from 'echarts/charts'
import { GridComponent, TooltipComponent } from 'echarts/components'
import * as echarts from 'echarts/core'
import { CanvasRenderer } from 'echarts/renderers'
import { useEffect, useMemo, useRef, useState } from 'react'

import type { ExecSummary } from '../api/types'
import { MAX_BARS, chartPoints, type ChartKind, type ChartPlan, type ChartPoint } from '../utils/chartPlan'
import { formatCell } from '../utils/format'

echarts.use([BarChart, LineChart, GridComponent, TooltipComponent, CanvasRenderer])

const SERIES_COLOR = '#2a78d6'
const TEXT_COLOR = '#52514e'
const GRID_COLOR = '#ecebe7'
const KIND_LABELS: Record<ChartKind, string> = { metric: '指标卡', line: '折线图', bar: '条形图' }
const BAR_ROW_PX = 28
const LINE_HEIGHT_PX = 300
const MIN_HEIGHT_PX = 160
const MAX_SYMBOLS = 40          // 点多时不画圆点，只留线

const numberText = (value: unknown) => (typeof value === 'number' ? formatCell(value) : '空')

function tooltip(trigger: 'axis') {
  return {
    trigger,
    axisPointer: { type: 'line' as const, lineStyle: { color: '#b5b3ab' } },
    valueFormatter: numberText,
  }
}

function lineOption(points: ChartPoint[], name: string) {
  return {
    animation: false,
    grid: { left: 8, right: 24, top: 16, bottom: 8, containLabel: true },
    tooltip: tooltip('axis'),
    xAxis: {
      type: 'category' as const,
      data: points.map((point) => point.label),
      axisLine: { lineStyle: { color: GRID_COLOR } },
      axisTick: { show: false },
      axisLabel: { color: TEXT_COLOR, hideOverlap: true },
    },
    yAxis: {
      type: 'value' as const,
      splitLine: { lineStyle: { color: GRID_COLOR } },
      axisLabel: { color: TEXT_COLOR },
    },
    series: [{
      name,
      type: 'line' as const,
      data: points.map((point) => point.value),
      connectNulls: false,
      showSymbol: points.length <= MAX_SYMBOLS,
      symbolSize: 8,
      lineStyle: { width: 2, color: SERIES_COLOR },
      itemStyle: { color: SERIES_COLOR, borderColor: '#fff', borderWidth: 2 },
    }],
  }
}

function barOption(points: ChartPoint[], name: string) {
  return {
    animation: false,
    grid: { left: 8, right: 56, top: 8, bottom: 8, containLabel: true },
    tooltip: tooltip('axis'),
    xAxis: { type: 'value' as const, splitLine: { lineStyle: { color: GRID_COLOR } }, axisLabel: { color: TEXT_COLOR } },
    yAxis: {
      type: 'category' as const,
      inverse: true,
      data: points.map((point) => point.label),
      axisLine: { lineStyle: { color: GRID_COLOR } },
      axisTick: { show: false },
      axisLabel: { color: TEXT_COLOR, width: 200, overflow: 'truncate' as const },
    },
    series: [{
      name,
      type: 'bar' as const,
      data: points.map((point) => point.value),
      barMaxWidth: 14,
      itemStyle: { color: SERIES_COLOR, borderRadius: [0, 4, 4, 0] },
      label: { show: points.length <= 12, position: 'right' as const, color: TEXT_COLOR, formatter: ({ value }: { value: unknown }) => numberText(value) },
    }],
  }
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

export default function ExecChart({ exec, plan }: { exec: ExecSummary; plan: ChartPlan }) {
  const [kind, setKind] = useState<ChartKind>(plan.kinds[0])
  const [valueIndex, setValueIndex] = useState(plan.valueIndexes[0])
  const name = exec.columns[valueIndex]
  const all = useMemo(() => chartPoints(exec, plan, valueIndex, kind), [exec, plan, valueIndex, kind])
  const points = kind === 'bar' ? all.slice(0, MAX_BARS) : all
  const option = useMemo(() => (kind === 'line' ? lineOption(points, name) : barOption(points, name)),
    [kind, name, points])  // eslint-disable-line react-hooks/exhaustive-deps

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
  return (
    <div className="exec-chart">
      <Flex justify="space-between" align="center" wrap gap={8} className="exec-chart-head">
        <Typography.Text strong>{name}<Typography.Text type="secondary">（按 {labelName}）</Typography.Text></Typography.Text>
        <Flex gap={8} align="center">
          {plan.valueIndexes.length > 1 && (
            <Select size="small" value={valueIndex} onChange={setValueIndex} popupMatchSelectWidth={false} aria-label="换指标"
                    options={plan.valueIndexes.map((index) => ({ value: index, label: exec.columns[index] }))} />
          )}
          {plan.kinds.length > 1 && (
            <Segmented size="small" value={kind} onChange={setKind}
                       options={plan.kinds.map((item) => ({ value: item, label: KIND_LABELS[item] }))} />
          )}
        </Flex>
      </Flex>
      <Canvas option={option} height={kind === 'line' ? LINE_HEIGHT_PX : Math.max(MIN_HEIGHT_PX, points.length * BAR_ROW_PX + 40)} />
      {all.length > points.length && (
        <Typography.Text type="secondary">共 {all.length} 行，图上只画前 {points.length} 行，其余看表格</Typography.Text>
      )}
    </div>
  )
}
