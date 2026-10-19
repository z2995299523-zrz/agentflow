/** 字段血缘：选字段、选深度，血缘图加右侧详情，下面是每一跳的明细表 */
import { AimOutlined, CloseOutlined } from '@ant-design/icons'
import { Alert, Button, Descriptions, Empty, Segmented, Select, Space, Spin, Table, Tabs, Tag, Typography } from 'antd'
import { useEffect, useMemo, useState, type ReactNode } from 'react'

import { api, errorText } from '../api/client'
import type { ColumnInfo, FieldBrief, Hop, LineageInfo } from '../api/types'
import { layerStyle, splitField } from '../utils/format'
import { hopExpression, type LayoutEdge } from '../utils/lineageLayout'
import LineageGraph from './LineageGraph'

const DEPTHS = [1, 2, 3, 5]
const ORIGIN_LABELS: Record<string, string> = { list: '血缘清单', script: '加工脚本解析' }

type Selection = { kind: 'node'; id: string } | { kind: 'edge'; edge: LayoutEdge } | null

interface Props {
  table: string
  columns: ColumnInfo[]
  column: string | null
  onColumn: (column: string) => void
  onOpenField: (table: string, column: string) => void
}

function briefOf(info: LineageInfo, id: string): FieldBrief {
  const { table, column } = splitField(id)
  return info.fields[id] ?? { table, column, table_cn: '', layer: '', column_cn: '', known: false }
}

function FieldName({ info, id }: { info: LineageInfo; id: string }) {
  const brief = briefOf(info, id)
  return (
    <span className="field-name">
      <Tag color={layerStyle(brief.layer).color} variant="filled">{layerStyle(brief.layer).short}</Tag>
      <code>{id}</code>{brief.column_cn && <span className="field-cn">{brief.column_cn}</span>}
    </span>
  )
}

function HopDetail({ info, hop }: { info: LineageInfo; hop: Hop }) {
  const expression = hopExpression(hop)
  return (
    <Descriptions
      size="small"
      column={1}
      bordered
      className="hop-detail"
      items={[
        { key: 'target', label: '加工出', children: <FieldName info={info} id={hop.target} /> },
        { key: 'source', label: '来源', children: (
          <>
            <FieldName info={info} id={hop.source} />
            {hop.source_external && <Tag color="orange">外部表</Tag>}
            {!hop.source_known && !hop.source_external && <Tag color="orange">不在字段清单里</Tag>}
          </>
        ) },
        { key: 'expression', label: '加工表达式',
          children: expression ? <pre className="expr">{expression}</pre> : <Typography.Text type="secondary">没有记录</Typography.Text> },
        ...(hop.filters.length ? [{ key: 'filters', label: '过滤条件',
          children: <div className="code-list">{hop.filters.map((item) => <code key={item}>{item}</code>)}</div> }] : []),
        ...(hop.joins.length ? [{ key: 'joins', label: '关联条件',
          children: <div className="code-list">{hop.joins.map((item) => <code key={item}>{item}</code>)}</div> }] : []),
        { key: 'script', label: '加工脚本', children: hop.script_name || '—' },
        { key: 'origin', label: '血缘来自', children: ORIGIN_LABELS[hop.origin] ?? hop.origin },
      ]}
    />
  )
}

/** 浮在血缘图右侧的详情：点了连线看这一跳的加工，点了字段看字段说明；什么都没选时不显示 */
function SidePanel({ info, selection, onOpenField, onClose }: {
  info: LineageInfo
  selection: NonNullable<Selection>
  onOpenField: (table: string, column: string) => void
  onClose: () => void
}) {
  let title: string
  let body: ReactNode
  if (selection.kind === 'edge') {
    const { edge } = selection
    title = `这一跳的加工${edge.hops.length > 1 ? `（${edge.hops.length} 条血缘）` : ''}`
    body = edge.hops.map((hop, index) => <HopDetail key={`${hop.script_name}-${index}`} info={info} hop={hop} />)
  } else {
    const brief = briefOf(info, selection.id)
    const all = [...info.upstream, ...info.downstream]
    const sources = all.filter((hop) => hop.target === selection.id).length
    const targets = all.filter((hop) => hop.source === selection.id).length
    title = brief.column_cn || brief.column
    body = (
      <>
        <Descriptions size="small" column={1} bordered items={[
          { key: 'field', label: '字段', children: <code>{brief.column}</code> },
          { key: 'table', label: '所在表', children: <span><code>{brief.table}</code> {brief.table_cn}</span> },
          { key: 'layer', label: '层级', children: brief.layer || '外部（不在表清单里）' },
          { key: 'links', label: '图上的连线', children: `来源 ${sources} 条，去向 ${targets} 条` },
        ]} />
        {brief.known && selection.id !== info.field && (
          <Button size="small" type="primary" icon={<AimOutlined />} onClick={() => onOpenField(brief.table, brief.column)}>
            以它为中心展开
          </Button>
        )}
      </>
    )
  }
  return (
    <div className="lineage-side">
      <div className="lineage-side-head">
        <Typography.Title level={5}>{title}</Typography.Title>
        <Button size="small" type="text" icon={<CloseOutlined />} onClick={onClose} aria-label="关闭" />
      </div>
      {body}
    </div>
  )
}

function HopTable({ info, hops, onPick }: { info: LineageInfo; hops: Hop[]; onPick: (hop: Hop) => void }) {
  return (
    <Table<Hop>
      size="small"
      rowKey={(hop) => `${hop.depth}-${hop.source}-${hop.target}-${hop.script_name}`}
      dataSource={hops}
      pagination={{ defaultPageSize: 10, hideOnSinglePage: true, showSizeChanger: false }}
      onRow={(hop) => ({ onClick: () => onPick(hop) })}
      scroll={{ x: 900 }}
      columns={[
        { key: 'depth', title: '跳', dataIndex: 'depth', width: 48 },
        { key: 'target', title: '加工出', width: 260, render: (_, hop) => <FieldName info={info} id={hop.target} /> },
        { key: 'source', title: '来源', width: 260, render: (_, hop) => <FieldName info={info} id={hop.source} /> },
        { key: 'expression', title: '加工', ellipsis: true,
          render: (_, hop) => <code className="expr-inline">{hopExpression(hop) || '—'}</code> },
        { key: 'conditions', title: '条件', width: 90,
          render: (_, hop) => (hop.filters.length + hop.joins.length
            ? <Tag color="orange">{hop.filters.length + hop.joins.length} 条</Tag> : '—') },
      ]}
    />
  )
}

export default function LineagePanel({ table, columns, column, onColumn, onOpenField }: Props) {
  const [depth, setDepth] = useState(3)
  const [info, setInfo] = useState<LineageInfo | null>(null)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState('')
  const [selection, setSelection] = useState<Selection>(null)

  useEffect(() => {
    setSelection(null)
    if (!column) {
      setInfo(null)
      return undefined
    }
    let cancelled = false
    setLoading(true)
    setError('')
    api.lineage(table, column, depth)
      .then((value) => { if (!cancelled) setInfo(value) })
      .catch((reason: unknown) => { if (!cancelled) { setInfo(null); setError(errorText(reason)) } })
      .finally(() => { if (!cancelled) setLoading(false) })
    return () => { cancelled = true }
  }, [table, column, depth])

  const options = useMemo(() => columns.map((item) => ({
    value: item.column_name,
    label: `${item.column_name}  ${item.column_cn}${item.upstream_count || item.downstream_count ? '' : '（无血缘）'}`,
  })), [columns])

  const openField = (fieldId: string) => {
    const { table: target, column: name } = splitField(fieldId)
    if (info?.fields[fieldId]?.known) onOpenField(target, name)
  }

  const pickHop = (hop: Hop) => {
    setSelection({ kind: 'edge', edge: { id: `${hop.source}->${hop.target}`, source: hop.source, target: hop.target,
                                          direction: 'upstream', hops: [hop] } })
  }

  const empty = info && !info.upstream.length && !info.downstream.length
  return (
    <div className="lineage-panel">
      <Space className="lineage-toolbar" wrap>
        <Select
          showSearch={{ optionFilterProp: 'label' }}
          className="lineage-select"
          placeholder="选一个字段"
          value={column ?? undefined}
          options={options}
          onChange={(value: string) => onColumn(value)}
        />
        <span className="toolbar-label">最多展开</span>
        <Segmented value={depth} onChange={(value) => setDepth(Number(value))}
                   options={DEPTHS.map((value) => ({ value, label: `${value} 跳` }))} />
        {info && !empty && (
          <Typography.Text type="secondary" className="toolbar-label">
            上游 {info.upstream.length} 跳、{new Set(info.upstream.map((hop) => hop.source)).size} 个字段 ·
            下游 {info.downstream.length} 跳、{new Set(info.downstream.map((hop) => hop.target)).size} 个字段
          </Typography.Text>
        )}
      </Space>
      {!column && <Empty description="在字段清单里点一个字段，或者在上面选一个字段" />}
      {error && <Alert type="error" showIcon title={error} />}
      {column && loading && !info && <div className="lineage-loading"><Spin /></div>}
      {info && empty && (
        <Alert type="info" showIcon title="这个字段没有登记血缘"
               description="血缘清单和加工脚本里都没有找到它的来源或去向。" />
      )}
      {info && !empty && (
        <>
          <div className="lineage-body">
            <Spin spinning={loading}>
              <LineageGraph
                key={`${info.field}-${depth}`}
                info={info}
                onSelectNode={(id) => setSelection({ kind: 'node', id })}
                onSelectEdge={(edge) => setSelection({ kind: 'edge', edge })}
                onClear={() => setSelection(null)}
                onOpenField={openField}
              />
            </Spin>
            {selection && (
              <SidePanel info={info} selection={selection} onOpenField={onOpenField} onClose={() => setSelection(null)} />
            )}
          </div>
          <Tabs
            size="small"
            className="hop-tabs"
            items={[
              { key: 'up', label: `上游（${info.upstream.length} 跳）`,
                children: <HopTable info={info} hops={info.upstream} onPick={pickHop} /> },
              { key: 'down', label: `下游（${info.downstream.length} 跳）`,
                children: <HopTable info={info} hops={info.downstream} onPick={pickHop} /> },
            ]}
          />
        </>
      )}
    </div>
  )
}
