/**
 * 执行结果的看板：图表、分页表格、导出 CSV；结果被截断时写明只取回了多少行；测试数据上方写明不是业务数据
 * 问题里没写展示形式时反问：列出这份结果能用的形式，点一下马上显示，不用再查一次，也不调模型
 */
import { DownloadOutlined, QuestionCircleFilled } from '@ant-design/icons'
import { Alert, Button, Flex, Segmented, Spin, Table, Typography } from 'antd'
import type { ColumnsType } from 'antd/es/table'
import { Suspense, lazy, useMemo, useState } from 'react'

import type { CellValue, ExecSummary } from '../api/types'
import { CHART_FORMS, planChart, type ChartKind, type ChartPlan } from '../utils/chartPlan'
import { downloadCsv } from '../utils/download'
import { formatCell, formatMs, isNumeric } from '../utils/format'

const ExecChart = lazy(() => import('./ExecChart'))      // 图表库单独打包，只看 SQL 时不用下载

type Row = Record<string, CellValue> & { key: number }

function DisplayPicker({ plan, wanted, onPick }: { plan: ChartPlan; wanted: string; onPick: (kind: ChartKind) => void }) {
  const missing = CHART_FORMS[wanted as ChartKind]        // 问题里写了，但这份结果画不了
  return (
    <div className="clarify exec-picker">
      <div className="clarify-question">
        <QuestionCircleFilled /> {missing ? `这份结果画不了${missing.label}，换一种形式？` : '用什么形式展示这份结果？'}
      </div>
      <div className="clarify-options">
        {plan.kinds.map((kind) => (
          <button key={kind} type="button" className="clarify-option" onClick={() => onPick(kind)}>
            <span className="clarify-label">{CHART_FORMS[kind].label}</span>
            <span className="clarify-meaning">{CHART_FORMS[kind].hint}</span>
          </button>
        ))}
      </div>
      <Typography.Text type="secondary" className="clarify-tip">
        只列这份结果能用的形式；下次在问题里写明「用柱状图」这类说法就不用选了
      </Typography.Text>
    </div>
  )
}

export default function ExecTable({ exec, display }: { exec: ExecSummary; display: string }) {
  const plan = useMemo(() => planChart(exec), [exec])
  const [kind, setKind] = useState<ChartKind | null>(() => {
    if (plan?.kinds.includes(display as ChartKind)) return display as ChartKind
    return plan?.kinds.length === 1 ? 'table' : null
  })
  const columns = useMemo<ColumnsType<Row>>(() => exec.columns.map((name, index) => {
    const numeric = exec.rows.length > 0 && exec.rows.every((row) => row[index] === null || isNumeric(row[index]))
    return {
      key: `c${index}`,
      dataIndex: `c${index}`,
      title: name,
      align: numeric ? 'right' : 'left',
      render: (value: CellValue) => (
        <span className={value === null ? 'cell-null' : undefined}>{formatCell(value)}</span>
      ),
    }
  }), [exec])
  const data = useMemo<Row[]>(() => exec.rows.map((row, rowIndex) => {
    const record: Row = { key: rowIndex }
    row.forEach((value, index) => { record[`c${index}`] = value })
    return record
  }), [exec])

  if (!exec.executed) {
    return exec.error
      ? <Alert type="error" showIcon title="执行失败" description={exec.error} />
      : <Typography.Text type="secondary">{exec.note || '没有执行'}</Typography.Text>
  }
  return (
    <div className="exec-table">
      {exec.note && <Alert type="warning" showIcon className="exec-note" title={exec.note} />}
      {plan && !kind && <DisplayPicker plan={plan} wanted={display} onPick={setKind} />}
      {plan && kind && plan.kinds.length > 1 && (
        <Segmented className="exec-kinds" size="small" value={kind} onChange={setKind}
                   options={plan.kinds.map((item) => ({ value: item, label: CHART_FORMS[item].label }))} />
      )}
      {plan && kind && kind !== 'table' && (
        <Suspense fallback={<Spin size="small" />}>
          <ExecChart exec={exec} plan={plan} kind={kind} />
        </Suspense>
      )}
      <Flex justify="space-between" align="center" className="exec-meta">
        <Typography.Text type="secondary">
          返回 {exec.row_count} 行，用时 {formatMs(exec.elapsed_ms)}
          {exec.truncated && `；结果超过上限，只取回了前 ${exec.row_count} 行`}
        </Typography.Text>
        <Button size="small" icon={<DownloadOutlined />} onClick={() => downloadCsv('查询结果.csv', exec.columns, exec.rows)}>
          导出 CSV
        </Button>
      </Flex>
      <Table<Row>
        size="small"
        columns={columns}
        dataSource={data}
        scroll={{ x: 'max-content' }}
        pagination={{
          defaultPageSize: 10,
          showSizeChanger: true,
          pageSizeOptions: [10, 20, 50, 100],
          showTotal: (total) => `共 ${total} 行`,
          hideOnSinglePage: false,
        }}
      />
    </div>
  )
}
