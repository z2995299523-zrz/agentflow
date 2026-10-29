/** 结果的说明：口径说明、执行结果、用表与字段、运行过程（含每次模型调用的 token），分标签页放 */
import { Descriptions, Empty, Table, Tabs, Tag, Timeline, Tooltip, Typography } from 'antd'
import type { ReactNode } from 'react'
import { Link } from 'react-router'

import {
  STEP_LABELS,
  type CallRecord,
  type PlanField,
  type PlanTable,
  type QueryPlan,
  type QueryResult,
  type StepKey,
} from '../api/types'
import { formatMs } from '../utils/format'
import ExecTable from './ExecTable'

const USAGE_LABELS: Record<PlanField['usage'], string> = {
  metric: '指标', dimension: '维度', filter: '过滤', join: '关联', order: '排序', other: '其它',
}

/** 元数据页的地址：打开一张表，给了字段就直接看它的血缘 */
export function metadataLink(table: string, column?: string): string {
  const params = new URLSearchParams({ table })
  if (column) {
    params.set('column', column)
    params.set('tab', 'lineage')
  }
  return `/metadata?${params.toString()}`
}

function textBlock(text: string): ReactNode {
  return <Typography.Paragraph className="pre-wrap" style={{ marginBottom: 0 }}>{text}</Typography.Paragraph>
}

function caliberItems(result: QueryResult) {
  const plan = result.plan
  const items: { key: string; label: string; children: ReactNode }[] = []
  const add = (key: string, label: string, children: ReactNode, when: boolean) => {
    if (when) items.push({ key, label, children })
  }
  add('caliber', '口径说明', textBlock(plan?.caliber ?? ''), !!plan?.caliber.trim())
  add('table_basis', '用表依据', textBlock(plan?.table_basis ?? ''), !!plan?.table_basis.trim())
  add('join_basis', '关联说明', textBlock(plan?.join_basis ?? ''), !!plan?.join_basis.trim())
  add('lineage_note', '溯源说明', textBlock(plan?.lineage_note ?? ''), !!plan?.lineage_note.trim())
  add('filters', '过滤条件', (
    <div className="code-list">{plan?.filters.map((item) => <code key={item}>{item}</code>)}</div>
  ), !!plan?.filters.length)
  add('assumptions', '假设', (
    <ul className="plain-list">{result.assumptions.map((item) => <li key={item}>{item}</li>)}</ul>
  ), result.assumptions.length > 0)
  add('examples', '参考样例', (
    <div>{plan?.examples_used.map((item) => <Tag key={item}>{item}</Tag>)}</div>
  ), !!plan?.examples_used.length)
  return items
}

function PlanTables({ plan, result }: { plan: QueryPlan; result: QueryResult }) {
  return (
    <div className="plan-tables">
      <Typography.Title level={5}>用到的表</Typography.Title>
      <Table<PlanTable>
        size="small"
        rowKey="table"
        pagination={false}
        dataSource={plan.tables}
        columns={[
          { key: 'table', title: '表', dataIndex: 'table', width: 280,
            render: (table: string) => <Link to={metadataLink(table)}><code>{table}</code></Link> },
          { key: 'reason', title: '为什么用它', dataIndex: 'reason' },
        ]}
      />
      {plan.fields.length > 0 && (
        <>
          <Typography.Title level={5}>用到的字段</Typography.Title>
          <Table<PlanField>
            size="small"
            rowKey={(field) => `${field.table}.${field.column}.${field.usage}`}
            pagination={false}
            dataSource={plan.fields}
            columns={[
              { key: 'field', title: '字段（点开看血缘）', width: 320,
                render: (_, field) => (
                  <Link to={metadataLink(field.table, field.column)}><code>{field.table}.{field.column}</code></Link>
                ) },
              { key: 'usage', title: '用途', dataIndex: 'usage', width: 80,
                render: (usage: PlanField['usage']) => <Tag>{USAGE_LABELS[usage] ?? usage}</Tag> },
              { key: 'reason', title: '说明', dataIndex: 'reason' },
            ]}
          />
        </>
      )}
      {plan.joins.length > 0 && (
        <>
          <Typography.Title level={5}>关联</Typography.Title>
          <ul className="plain-list">
            {plan.joins.map((join) => (
              <li key={`${join.left_table}-${join.right_table}-${join.left_columns.join()}`}>
                <code>{join.left_table}({join.left_columns.join(', ')})</code> {join.join_type} JOIN{' '}
                <code>{join.right_table}({join.right_columns.join(', ')})</code>
                {join.reason && <Typography.Text type="secondary">：{join.reason}</Typography.Text>}
              </li>
            ))}
          </ul>
        </>
      )}
      {plan.dropped_tables.length > 0 && (
        <p><Typography.Text type="secondary">精简方案时去掉的表：</Typography.Text>
          {plan.dropped_tables.map((table) => <Tag key={table}>{table}</Tag>)}</p>
      )}
      {result.candidate_tables.length > 0 && (
        <p><Typography.Text type="secondary">证据里的候选表（{result.candidate_tables.length} 张）：</Typography.Text>
          {result.candidate_tables.map((table) => (
            <Link key={table} to={metadataLink(table)}><Tag className="link-tag">{table}</Tag></Link>
          ))}</p>
      )}
    </div>
  )
}

/** 每次调用写成哪一步：同一步又调了一轮（上溯后重新选表、修正 SQL）加「第 N 轮」，输出不合格后的重试加「重试」 */
function callLabels(calls: CallRecord[]): string[] {
  const rounds: Record<string, number> = {}
  return calls.map((call) => {
    const label = STEP_LABELS[call.step as StepKey] ?? call.step
    if (call.attempt > 1) return `${label}（重试）`
    rounds[call.step] = (rounds[call.step] ?? 0) + 1
    return rounds[call.step] > 1 ? `${label}（第 ${rounds[call.step]} 轮）` : label
  })
}

function callResult(call: CallRecord): ReactNode {
  if (call.ok) return <Tag color="success">成功</Tag>
  const text = call.error.startsWith('模型调用失败') ? '调用失败' : '输出不合格'
  return <Tooltip title={call.error}><Tag color="error">{text}</Tag></Tooltip>
}

/** 每次模型调用一行：哪一步、输入和输出的 token、用时、结果；最后一行合计 */
function CallTable({ calls }: { calls: CallRecord[] }) {
  const labels = callLabels(calls)
  const rows = calls.map((call, index) => ({ ...call, index, label: labels[index] }))
  const tokensIn = calls.reduce((sum, call) => sum + call.input_tokens, 0)
  const tokensOut = calls.reduce((sum, call) => sum + call.output_tokens, 0)
  return (
    <div className="call-table">
      <Typography.Title level={5}>模型调用（{calls.length} 次）</Typography.Title>
      <Table
        size="small"
        rowKey="index"
        pagination={false}
        scroll={{ x: 'max-content' }}
        dataSource={rows}
        columns={[
          { key: 'index', title: '#', width: 44, render: (_, row) => row.index + 1 },
          { key: 'step', title: '步骤', dataIndex: 'label' },
          { key: 'in', title: '输入 tokens', align: 'right', render: (_, row) => row.input_tokens.toLocaleString() },
          { key: 'out', title: '输出 tokens', align: 'right', render: (_, row) => row.output_tokens.toLocaleString() },
          { key: 'ms', title: '用时', align: 'right', render: (_, row) => formatMs(row.elapsed_ms) },
          { key: 'ok', title: '结果', render: (_, row) => callResult(row) },
        ]}
        summary={() => (
          <Table.Summary.Row>
            <Table.Summary.Cell index={0} colSpan={2}>合计</Table.Summary.Cell>
            <Table.Summary.Cell index={2} align="right">{tokensIn.toLocaleString()}</Table.Summary.Cell>
            <Table.Summary.Cell index={3} align="right">{tokensOut.toLocaleString()}</Table.Summary.Cell>
            <Table.Summary.Cell index={4} colSpan={2} />
          </Table.Summary.Row>
        )}
      />
    </div>
  )
}

function TraceTimeline({ result }: { result: QueryResult }) {
  const notes = [
    result.first_pass_ok !== null ? `第一次生成的 SQL ${result.first_pass_ok ? '通过' : '没通过'}校验` : '',
    result.recall_notes.length > 0 ? `召回说明：${result.recall_notes.join('；')}` : '',
  ].filter(Boolean)
  return (
    <div className="trace">
      <Timeline
        items={result.trace.map((step, index) => ({
          key: `${step.step}-${index}`,
          color: step.step === result.failed_step ? 'red' : 'blue',
          content: (
            <div className="trace-item">
              <span className="trace-label">{step.label}</span>
              <span className="trace-ms">{formatMs(step.elapsed_ms)}</span>
              {step.summary && <div className="trace-summary">{step.summary}</div>}
            </div>
          ),
        }))}
      />
      {result.calls.length > 0
        ? <CallTable calls={result.calls} />
        : <Typography.Text type="secondary">这一轮没有调用模型</Typography.Text>}
      {notes.length > 0 && <Typography.Text type="secondary" className="trace-notes">{notes.join('；')}</Typography.Text>}
    </div>
  )
}

export default function ResultTabs({ result }: { result: QueryResult }) {
  const plan = result.plan
  const items: { key: string; label: string; children: ReactNode }[] = []
  const caliber = caliberItems(result)
  if (caliber.length) {
    items.push({ key: 'caliber', label: '口径说明',
                 children: <Descriptions column={1} bordered size="small" items={caliber} className="caliber" /> })
  }
  if (result.exec_result) {
    const exec = result.exec_result
    items.push({ key: 'exec', label: exec.executed ? `执行结果（${exec.row_count} 行）` : '执行结果',
                 children: <ExecTable exec={exec} /> })
  }
  if (plan && plan.tables.length > 0) {
    items.push({ key: 'tables', label: `用表与字段（${plan.tables.length} 张表）`,
                 children: <PlanTables plan={plan} result={result} /> })
  }
  items.push({ key: 'trace', label: '运行过程',
               children: result.trace.length ? <TraceTimeline result={result} /> : <Empty /> })
  return <Tabs className="result-tabs" size="small" items={items} defaultActiveKey={items[0].key} />
}
