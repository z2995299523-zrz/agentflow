/** 按上一轮记录解释用表，逐张列出诊断，并允许在当前需求上改用指定表 */
import { Button, Table, Tag, Typography } from 'antd'
import { Link } from 'react-router'

import type { Explanation, FieldMatch, PlanTable } from '../api/types'
import { layerStyle } from '../utils/format'
import { metadataLink } from './ResultTabs'

interface Props {
  explanation: Explanation
  active: boolean                     // 这一轮之后还没有新的需求时，改用按钮才能点
  disabled: boolean                   // 有一轮正在跑
  onPick: (text: string) => void
}

function matchColor(match: FieldMatch): string {
  if (!match.column) return 'orange'
  return match.weak ? 'gold' : 'green'
}

function matchText(match: FieldMatch): string {
  if (!match.column) return `${match.term}：没有名字相近的字段`
  return `${match.term} → ${match.weak ? '可能是 ' : ''}${match.column_cn || match.column}`
}

/** 解释只展示记录和元数据，改用按钮提交一条保留原需求的追问 */
export default function ExplainPanel({ explanation, active, disabled, onPick }: Props) {
  const locked = !active || disabled
  return (
    <div className="explain-panel">
      {explanation.notes.map((note, index) => (
        <Typography.Text key={index} type="secondary">{note}</Typography.Text>
      ))}
      {explanation.requirement && (
        <div className="requirement"><span className="requirement-label">上一轮的需求</span>{explanation.requirement}</div>
      )}
      {explanation.tables.length > 0 && (
        <div className="explain-basis">
          <Table<PlanTable>
            size="small"
            tableLayout="fixed"
            rowKey="table"
            pagination={false}
            dataSource={explanation.tables}
            columns={[
              { key: 'table', title: '表', dataIndex: 'table',
                render: (table: string) => <Link to={metadataLink(table)}><code>{table}</code></Link> },
              { key: 'reason', title: '为什么用它', dataIndex: 'reason' },
            ]}
          />
          {explanation.table_basis && <p><strong>用表依据：</strong>{explanation.table_basis}</p>}
          {explanation.caliber && <p><strong>口径说明：</strong>{explanation.caliber}</p>}
          {explanation.dropped_tables.length > 0 && (
            <p><strong>精简方案时去掉的表：</strong>{explanation.dropped_tables.map((table) => <Tag key={table}>{table}</Tag>)}</p>
          )}
        </div>
      )}
      {explanation.candidate_tables.length > 0 && (
        <p className="explain-candidates"><strong>④ 看到的候选表（{explanation.candidate_tables.length} 张）：</strong>
          {explanation.candidate_tables.map((table) => (
            <Link key={table} to={metadataLink(table)}><Tag className="link-tag">{table}</Tag></Link>
          ))}</p>
      )}
      {explanation.named.map((table, index) => (
        <section key={`${table.name}-${index}`} className="explain-table">
          <div className="explain-table-head">
            {table.table_id ? (
              <Link to={metadataLink(table.table_id)}><code>{table.table_id}</code></Link>
            ) : <code>{table.name}</code>}
            {table.table_cn && <span>{table.table_cn}</span>}
            {table.layer && <Tag color={layerStyle(table.layer).color}>{table.layer}</Tag>}
            {table.table_type && <Tag>{table.table_type}</Tag>}
            {!table.standard && <Tag color="orange">非标准表</Tag>}
          </div>
          <Typography.Text strong>{table.conclusion}</Typography.Text>
          {table.reasons.length > 0 && (
            <ul className="plain-list">{table.reasons.map((reason, i) => <li key={i}>{reason}</li>)}</ul>
          )}
          {table.matches.length > 0 && (
            <div className="explain-matches">
              <span>需求对照：</span>
              {table.matches.map((match) => (
                <Tag key={`${match.role}-${match.term}`} color={matchColor(match)}>{matchText(match)}</Tag>
              ))}
              <Typography.Text type="secondary" className="explain-match-note">只按字段名比较，不代表口径一致</Typography.Text>
            </div>
          )}
          {table.switchable && (
            <Button disabled={locked} onClick={() => onPick(`改用 ${table.table_id} 这张表`)}>改用这张表重新生成</Button>
          )}
          {table.similar.length > 0 && (
            <div className="explain-similar">
              <Typography.Text type="secondary">相近的表</Typography.Text>
              {table.similar.map((similar) => (
                <div key={similar} className="explain-similar-row">
                  <Button type="link" size="small" disabled={locked} onClick={() => onPick(`改用 ${similar} 这张表`)}>{similar}</Button>
                  <Link to={metadataLink(similar)}>查看元数据</Link>
                </div>
              ))}
            </div>
          )}
        </section>
      ))}
    </div>
  )
}
