/** 界面上的格式化：耗时、时间、数字、层级颜色、结果状态 */
import type { CellValue, QueryResult } from '../api/types'

/** 毫秒写成「320 ms」或「6.2 秒」 */
export function formatMs(ms: number): string {
  return ms < 1000 ? `${ms} ms` : `${(ms / 1000).toFixed(1)} 秒`
}

function sameDay(a: Date, b: Date): boolean {
  return a.getFullYear() === b.getFullYear() && a.getMonth() === b.getMonth() && a.getDate() === b.getDate()
}

function pad(value: number): string {
  return String(value).padStart(2, '0')
}

/** 时间写成「14:03」（今天）、「昨天 14:03」、「10-17 14:03」（今年）或「2025-10-17」 */
export function formatTime(iso: string, now: Date = new Date()): string {
  const date = new Date(iso)
  if (Number.isNaN(date.getTime())) return iso
  const time = `${pad(date.getHours())}:${pad(date.getMinutes())}`
  if (sameDay(date, now)) return time
  const yesterday = new Date(now)
  yesterday.setDate(now.getDate() - 1)
  if (sameDay(date, yesterday)) return `昨天 ${time}`
  if (date.getFullYear() === now.getFullYear()) return `${pad(date.getMonth() + 1)}-${pad(date.getDate())} ${time}`
  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}`
}

/** 会话按时间分组：今天、昨天、最近 7 天、更早 */
export function timeGroup(iso: string, now: Date = new Date()): string {
  const date = new Date(iso)
  if (sameDay(date, now)) return '今天'
  const yesterday = new Date(now)
  yesterday.setDate(now.getDate() - 1)
  if (sameDay(date, yesterday)) return '昨天'
  return now.getTime() - date.getTime() < 7 * 24 * 3600 * 1000 ? '最近 7 天' : '更早'
}

/** 今天的日期：YYYY-MM-DD */
export function todayText(now: Date = new Date()): string {
  return `${now.getFullYear()}-${pad(now.getMonth() + 1)}-${pad(now.getDate())}`
}

const numberFormat = new Intl.NumberFormat('zh-CN', { maximumFractionDigits: 10 })
const DECIMAL_TEXT = /^-?\d+(\.\d+)?$/        // 接口把数据库的 DECIMAL 转成字符串传过来，金额不丢精度

/** 是不是数：数字，或者写成字符串的十进制数 */
export function isNumeric(value: CellValue): boolean {
  return typeof value === 'number' || (typeof value === 'string' && DECIMAL_TEXT.test(value))
}

/** 结果表格里的一个值：数加千分位（字符串形式的十进制数按字符串加，不转浮点），空值写 NULL */
export function formatCell(value: CellValue): string {
  if (value === null || value === undefined) return 'NULL'
  if (typeof value === 'number') return numberFormat.format(value)
  if (typeof value === 'boolean') return value ? 'true' : 'false'
  if (DECIMAL_TEXT.test(value)) {
    const [whole, fraction] = value.split('.')
    return whole.replace(/\B(?=(\d{3})+(?!\d))/g, ',') + (fraction !== undefined ? `.${fraction}` : '')
  }
  return value
}

/** 层级对应的颜色（Ant Design 的预设色）和简称，血缘图和标签共用 */
export const LAYER_STYLES: Record<string, { color: string; short: string; key: string }> = {
  贴源层: { color: 'green', short: '贴源', key: 'ods' },
  标准层: { color: 'blue', short: '标准', key: 'dwd' },
  汇总层: { color: 'cyan', short: '汇总', key: 'dws' },
  专题层: { color: 'purple', short: '专题', key: 'ads' },
}

export function layerStyle(layer: string): { color: string; short: string; key: string } {
  return LAYER_STYLES[layer] ?? { color: 'default', short: layer || '外部', key: 'ext' }
}

export interface StatusView {
  color: string                         // Tag 的颜色
  text: string                          // 一句话结论
  tone: 'success' | 'warning' | 'info' | 'error'
}

/** 一轮结果的结论：成功、需人工检查、待确认、不是查数需求、没有生成 SQL */
export function describeStatus(result: QueryResult): StatusView {
  switch (result.status) {
    case 'ok':
      return { color: 'success', text: '已生成 SQL', tone: 'success' }
    case 'unvalidated':
      return { color: 'warning', text: '需人工检查', tone: 'warning' }
    case 'need_clarify':
      return { color: 'processing', text: result.plan ? '待确认口径' : '待确认需求', tone: 'info' }
    default:
      if (result.parsed && !result.parsed.is_query) return { color: 'default', text: '不是查数需求', tone: 'info' }
      return { color: 'error', text: '没有生成 SQL', tone: 'error' }
  }
}

/** 字段标识「模式.表.字段」拆成表和字段 */
export function splitField(field: string): { table: string; column: string } {
  const index = field.lastIndexOf('.')
  return index < 0 ? { table: '', column: field } : { table: field.slice(0, index), column: field.slice(index + 1) }
}
