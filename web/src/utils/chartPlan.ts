/**
 * 这份结果能用哪些形式展示：只看结果本身的字段，不调模型，数据不出浏览器
 * 一行是指标卡；时间配数字有折线图、柱状图、条形图；分类配数字有条形图、柱状图，不超过 8 类再加饼图；表格总能看
 */
import type { CellValue, ExecSummary } from '../api/types'
import { isNumeric } from './format'

export type ChartKind = 'metric' | 'line' | 'bar' | 'column' | 'pie' | 'table'

/** 支持的展示形式：反问时列出来供选择，先后就是推荐的先后 */
export const CHART_FORMS: Record<ChartKind, { label: string; hint: string }> = {
  metric: { label: '指标卡', hint: '大号数字，看一个或几个汇总数' },
  line: { label: '折线图', hint: '看随时间的变化' },
  bar: { label: '条形图', hint: '横向比大小，名称长时好读' },
  column: { label: '柱状图', hint: '纵向比大小，类别少时好读' },
  pie: { label: '饼图', hint: '看各部分的占比，最多 8 类' },
  table: { label: '表格', hint: '只看明细，不画图' },
}

export interface ChartPlan {
  kinds: ChartKind[]         // 这份结果能用的形式，至少有表格
  labelIndexes: number[]     // 当分类或横轴的列
  valueIndexes: number[]     // 数值列，一张图只画其中一个，不用双轴
}

export interface ChartPoint {
  label: string
  value: number | null
}

export const MAX_BARS = 30
const MAX_PIE_SLICES = 8
const TIME_NAME = /(年|月|季|日期|时间|year|month|quarter|date|time|period)/i
const MONTH_NAME = /(月|季|month|quarter)/i
const KEY_NAME = /(编号|代码|编码|序号|ID|_id|_no|_code|_cd)$/i
const YEAR_FIRST = /^\d{4}(\D.*)?$/

function isTimeColumn(name: string, values: CellValue[]): boolean {
  const texts = values.map(String)
  if (texts.every((text) => /^\d{4}[-/年]/.test(text))) return true
  if (!TIME_NAME.test(name)) return false
  return texts.every((text) => YEAR_FIRST.test(text)) || (MONTH_NAME.test(name) && values.every((value) => Number(value) >= 1 && Number(value) <= 12))
}

/** 编号、代码是分类不是数量，SUM 和图上的长短都没有意义 */
function isValueColumn(name: string, values: CellValue[]): boolean {
  return values.every(isNumeric) && !KEY_NAME.test(name)
}

function suitsPie(exec: ExecSummary, values: number[]): boolean {
  return exec.rows.length <= MAX_PIE_SLICES && exec.rows.every((row) => values.every((index) => Number(row[index] ?? 0) >= 0))
}

/** 结果的展示方案；没有执行、一行也没有时为 null */
export function planChart(exec: ExecSummary): ChartPlan | null {
  if (!exec.executed || exec.rows.length === 0) return null
  const times: number[] = []
  const values: number[] = []
  const labels: number[] = []
  exec.columns.forEach((name, index) => {
    const filled = exec.rows.map((row) => row[index]).filter((value) => value !== null && value !== '')
    if (filled.length === 0) return
    if (isTimeColumn(name, filled)) times.push(index)
    else if (isValueColumn(name, filled)) values.push(index)
    else labels.push(index)
  })
  const tableOnly: ChartPlan = { kinds: ['table'], labelIndexes: [], valueIndexes: [] }
  if (values.length === 0) return tableOnly
  if (exec.rows.length === 1) return { kinds: ['metric', 'table'], labelIndexes: [], valueIndexes: values }
  if (times.length === 1 && labels.length === 0) {
    return { kinds: ['line', 'column', 'bar', 'table'], labelIndexes: times, valueIndexes: values }
  }
  const named = labels.filter((index) => !KEY_NAME.test(exec.columns[index]))
  const chosen = [...times, ...(named.length ? named : labels)]
  if (chosen.length === 0) return tableOnly
  const kinds: ChartKind[] = suitsPie(exec, values) ? ['bar', 'column', 'pie', 'table'] : ['bar', 'column', 'table']
  return { kinds, labelIndexes: chosen, valueIndexes: values }
}

/** 一个指标的点，分类几列时拼成一个标签；折线按时间排好，其余保持 SQL 的先后 */
export function chartPoints(exec: ExecSummary, plan: ChartPlan, valueIndex: number, kind: ChartKind): ChartPoint[] {
  const points = exec.rows.map((row) => ({
    label: plan.labelIndexes.map((index) => String(row[index] ?? '空')).join(' / '),
    value: row[valueIndex] === null ? null : Number(row[valueIndex]),
  }))
  return kind === 'line' ? points.sort((a, b) => a.label.localeCompare(b.label, 'zh', { numeric: true })) : points
}
