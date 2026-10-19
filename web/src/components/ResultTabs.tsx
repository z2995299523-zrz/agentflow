/** 结果的说明：口径说明、执行结果、用表与字段、运行过程，分标签页放 */
import { Descriptions, Empty, Table, Tabs, Tag, Timeline, Typography } from 'antd'
import type { ReactNode } from 'react'
import { Link } from 'react-router'

import type { PlanField, PlanTable, QueryPlan, QueryResult } from '../api/types'
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

function TraceTimeline({ result }: { result: QueryResult }) {
  const tokensIn = result.calls.reduce((sum, call) => sum + call.input_tokens, 0)
  const tokensOut = result.calls.reduce((sum, call) => sum + call.output_tokens, 0)
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
      <Typography.Text type="secondary">
        模型调用 {result.calls.length} 次，输入 {tokensIn.toLocaleString()} tokens、输出 {tokensOut.toLocaleString()} tokens
        {result.first_pass_ok !== null && `；第一次生成的 SQL ${result.first_pass_ok ? '通过' : '没通过'}校验`}
        {result.recall_notes.length > 0 && `；召回说明：${result.recall_notes.join('；')}`}
      </Typography.Text>
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
