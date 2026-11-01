/**
 * 按结果的字段选图：只看结果本身，不调模型，数据不出浏览器
 * 只有一行是指标卡；时间配数字是折线（也能换成条形）；分类配数字是条形；其余只看表格
 */
import type { CellValue, ExecSummary } from '../api/types'
import { isNumeric } from './format'

export type ChartKind = 'metric' | 'line' | 'bar'

export interface ChartPlan {
  kinds: ChartKind[]         // 能画的图，第一个是默认
  labelIndexes: number[]     // 当分类或横轴的列
  valueIndexes: number[]     // 数值列，一张图只画其中一个，不用双轴
}

export interface ChartPoint {
  label: string
  value: number | null
}

export const MAX_BARS = 30
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
  if (values.length === 0) return null
  if (exec.rows.length === 1) return { kinds: ['metric'], labelIndexes: [], valueIndexes: values }
  if (times.length === 1 && labels.length === 0) return { kinds: ['line', 'bar'], labelIndexes: times, valueIndexes: values }
  const named = labels.filter((index) => !KEY_NAME.test(exec.columns[index]))
  const chosen = [...times, ...(named.length ? named : labels)]
  return chosen.length ? { kinds: ['bar'], labelIndexes: chosen, valueIndexes: values } : null
}

/** 一个指标的点，分类几列时拼成一个标签；折线按时间排好，条形保持 SQL 的先后 */
export function chartPoints(exec: ExecSummary, plan: ChartPlan, valueIndex: number, kind: ChartKind): ChartPoint[] {
  const points = exec.rows.map((row) => ({
    label: plan.labelIndexes.map((index) => String(row[index] ?? '空')).join(' / '),
    value: row[valueIndex] === null ? null : Number(row[valueIndex]),
  }))
  return kind === 'line' ? points.sort((a, b) => a.label.localeCompare(b.label, 'zh', { numeric: true })) : points
}
