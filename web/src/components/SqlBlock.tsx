/** SQL 代码块：语法高亮、行号、复制按钮；标出静态校验是否通过 */
import { CheckCircleFilled, CopyOutlined, WarningFilled } from '@ant-design/icons'
import { App, Button, Tooltip } from 'antd'
import hljs from 'highlight.js/lib/core'
import sql from 'highlight.js/lib/languages/sql'
import { useMemo } from 'react'

import { copyText } from '../utils/download'

hljs.registerLanguage('sql', sql)

const DIALECT_LABELS: Record<string, string> = { postgres: 'GaussDB / PostgreSQL' }

interface Props {
  sql: string
  dialect?: string
  validated?: boolean | null           // true 校验通过，false 有错误，null 没校验
}

export default function SqlBlock({ sql: text, dialect = 'postgres', validated = null }: Props) {
  const { message } = App.useApp()
  const html = useMemo(() => hljs.highlight(text, { language: 'sql' }).value, [text])     // 输出已经转义过
  const lineCount = text.split('\n').length

  const copy = async () => {
    if (await copyText(text)) message.success('SQL 已复制')
    else message.error('复制失败，请手动选中复制')
  }

  return (
    <div className="sql-block">
      <div className="sql-toolbar">
        <span className="sql-dialect">SQL · {DIALECT_LABELS[dialect] ?? dialect}</span>
        {validated === true && <span className="sql-check ok"><CheckCircleFilled /> 静态校验通过</span>}
        {validated === false && <span className="sql-check bad"><WarningFilled /> 静态校验有错误</span>}
        <Tooltip title="复制 SQL">
          <Button size="small" type="text" icon={<CopyOutlined />} onClick={copy} className="sql-copy">复制</Button>
        </Tooltip>
      </div>
      <div className="sql-body">
        <pre className="sql-gutter" aria-hidden>{Array.from({ length: lineCount }, (_, index) => index + 1).join('\n')}</pre>
        <pre className="sql-code hljs"><code dangerouslySetInnerHTML={{ __html: html }} /></pre>
      </div>
    </div>
  )
}
