/** 元数据页左侧：按层级筛表、按关键字搜表和字段（搜索在服务端做，真实库两万多个字段） */
import { SearchOutlined } from '@ant-design/icons'
import { Alert, Empty, Input, Segmented, Spin, Tag, Typography } from 'antd'
import { useEffect, useMemo, useState } from 'react'

import { api, errorText } from '../api/client'
import { LAYERS, type SearchResult, type TableInfo } from '../api/types'
import { layerStyle } from '../utils/format'

const ALL = '全部'
const SEARCH_DELAY_MS = 250

interface Props {
  tables: TableInfo[]
  selected: string | null
  onSelectTable: (table: string) => void
  onSelectColumn: (table: string, column: string) => void
}

function TableItem({ table, active, onClick }: { table: TableInfo; active: boolean; onClick: () => void }) {
  const layer = layerStyle(table.layer)
  return (
    <button type="button" className={`table-item${active ? ' active' : ''}`} onClick={onClick}>
      <span className="table-item-cn">{table.table_cn || table.table_id}</span>
      <span className="table-item-id">{table.table_id}</span>
      <span className="table-item-tags">
        <Tag color={layer.color} variant="filled">{layer.short}</Tag>
        {table.table_type && <Tag variant="filled">{table.table_type}</Tag>}
        <span className="table-item-count">{table.column_count} 个字段</span>
      </span>
    </button>
  )
}

export default function TableBrowser({ tables, selected, onSelectTable, onSelectColumn }: Props) {
  const [layer, setLayer] = useState<string>(ALL)
  const [keyword, setKeyword] = useState('')
  const [result, setResult] = useState<SearchResult | null>(null)
  const [searching, setSearching] = useState(false)
  const [error, setError] = useState('')

  useEffect(() => {
    const word = keyword.trim()
    if (!word) {
      setResult(null)
      setError('')
      return undefined
    }
    let cancelled = false
    const timer = window.setTimeout(() => {
      setSearching(true)
      api.search(word)
        .then((value) => { if (!cancelled) { setResult(value); setError('') } })
        .catch((reason: unknown) => { if (!cancelled) setError(errorText(reason)) })
        .finally(() => { if (!cancelled) setSearching(false) })
    }, SEARCH_DELAY_MS)
    return () => {
      cancelled = true
      window.clearTimeout(timer)
    }
  }, [keyword])

  const counts = useMemo(() => {
    const value: Record<string, number> = { [ALL]: tables.length }
    for (const table of tables) value[table.layer] = (value[table.layer] ?? 0) + 1
    return value
  }, [tables])
  const shown = layer === ALL ? tables : tables.filter((table) => table.layer === layer)
  const inLayer = (item: { layer: string }) => layer === ALL || item.layer === layer

  return (
    <div className="table-browser">
      <Segmented
        block
        size="small"
        value={layer}
        onChange={(value) => setLayer(String(value))}
        options={[ALL, ...LAYERS].map((name) => ({ value: name, label: `${name === ALL ? name : layerStyle(name).short} ${counts[name] ?? 0}` }))}
      />
      <Input
        allowClear
        prefix={<SearchOutlined />}
        suffix={searching ? <Spin size="small" /> : null}
        placeholder="搜表名、字段名或中文名"
        value={keyword}
        onChange={(event) => setKeyword(event.target.value)}
      />
      {error && <Alert type="error" showIcon title={error} />}
      <div className="table-browser-list">
        {result ? (
          <>
            <div className="list-group">表（{result.table_total}）</div>
            {result.tables.filter(inLayer).map((table) => (
              <TableItem key={table.table_id} table={table} active={table.table_id === selected}
                         onClick={() => onSelectTable(table.table_id)} />
            ))}
            <div className="list-group">
              字段（{result.column_total}{result.column_total > result.columns.length ? `，显示前 ${result.columns.length} 个` : ''}）
            </div>
            {result.columns.filter(inLayer).map((hit) => (
              <button type="button" key={`${hit.table_id}.${hit.column_name}`} className="column-hit"
                      onClick={() => onSelectColumn(hit.table_id, hit.column_name)}>
                <span className="column-hit-name"><code>{hit.column_name}</code> {hit.column_cn}</span>
                <span className="column-hit-table">
                  <Tag color={layerStyle(hit.layer).color} variant="filled">{layerStyle(hit.layer).short}</Tag>
                  {hit.table_cn || hit.table_id}
                </span>
              </button>
            ))}
            {!result.tables.length && !result.columns.length && (
              <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="没有找到" />
            )}
          </>
        ) : shown.length ? (
          shown.map((table) => (
            <TableItem key={table.table_id} table={table} active={table.table_id === selected}
                       onClick={() => onSelectTable(table.table_id)} />
          ))
        ) : (
          <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description={<Typography.Text type="secondary">这一层没有表</Typography.Text>} />
        )}
      </div>
    </div>
  )
}
