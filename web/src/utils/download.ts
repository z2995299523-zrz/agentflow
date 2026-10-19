/** 复制到剪贴板、导出 CSV */
import type { CellValue } from '../api/types'

/** 复制文本：优先用剪贴板接口；不是 https 或 localhost（局域网 IP 访问）时剪贴板接口不可用，退回到选中文本复制 */
export async function copyText(text: string): Promise<boolean> {
  if (window.isSecureContext && navigator.clipboard) {
    try {
      await navigator.clipboard.writeText(text)
      return true
    } catch {
      // 权限被拒绝：走下面的办法
    }
  }
  const area = document.createElement('textarea')
  area.value = text
  area.setAttribute('readonly', '')
  area.style.position = 'fixed'
  area.style.opacity = '0'
  document.body.appendChild(area)
  area.select()
  try {
    return document.execCommand('copy')
  } finally {
    document.body.removeChild(area)
  }
}

function csvCell(value: CellValue): string {
  if (value === null || value === undefined) return ''
  const text = String(value)
  return /[",\n\r]/.test(text) ? `"${text.replace(/"/g, '""')}"` : text
}

/** 把结果表格导出成 CSV 文件；带 BOM，Excel 打开中文不乱码 */
export function downloadCsv(fileName: string, columns: string[], rows: CellValue[][]): void {
  const lines = [columns.map(csvCell).join(','), ...rows.map((row) => row.map(csvCell).join(','))]
  const blob = new Blob(['﻿' + lines.join('\r\n')], { type: 'text/csv;charset=utf-8' })
  const url = URL.createObjectURL(blob)
  const link = document.createElement('a')
  link.href = url
  link.download = fileName
  link.click()
  URL.revokeObjectURL(url)
}
