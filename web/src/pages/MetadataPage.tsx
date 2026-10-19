/**
 * 元数据页：左边按层级浏览、搜索表和字段；右边是表的信息、字段清单和字段血缘
 * 选中的表、字段、标签页记在地址里（?table=&column=&tab=），查数页的「用表与字段」可以直接链过来
 */
import { Alert, Descriptions, Empty, Tabs, Tag, Typography } from 'antd'
import { useEffect, useMemo, useState } from 'react'
import { useSearchParams } from 'react-router'

import { api, errorText } from '../api/client'
import type { ColumnInfo, TableInfo } from '../api/types'
import ColumnTable from '../components/ColumnTable'
import LineagePanel from '../components/LineagePanel'
import { ServiceAlert } from '../components/StatusBar'
import TableBrowser from '../components/TableBrowser'
import { layerStyle } from '../utils/format'

type TabKey = 'columns' | 'lineage'

export default function MetadataPage() {
  const [params, setParams] = useSearchParams()
  const tableId = params.get('table')
  const column = params.get('column')
  const tab: TabKey = params.get('tab') === 'lineage' ? 'lineage' : 'columns'

  const [tables, setTables] = useState<TableInfo[]>([])
  const [tablesError, setTablesError] = useState('')
  const [columns, setColumns] = useState<ColumnInfo[]>([])
  const [columnsLoading, setColumnsLoading] = useState(false)
  const [columnsError, setColumnsError] = useState('')

  useEffect(() => {
    api.tables()
      .then((items) => { setTables(items); setTablesError('') })
      .catch((error: unknown) => setTablesError(errorText(error)))
  }, [])

  useEffect(() => {
    if (!tableId) {
      setColumns([])
      return undefined
    }
    let cancelled = false
    setColumnsLoading(true)
    setColumnsError('')
    api.columns(tableId)
      .then((items) => { if (!cancelled) setColumns(items) })
      .catch((error: unknown) => { if (!cancelled) { setColumns([]); setColumnsError(errorText(error)) } })
      .finally(() => { if (!cancelled) setColumnsLoading(false) })
    return () => { cancelled = true }
  }, [tableId])

  const table = useMemo(() => tables.find((item) => item.table_id === tableId) ?? null, [tables, tableId])

  const select = (next: { table?: string | null; column?: string | null; tab?: TabKey }) => {
    const value = new URLSearchParams()
    const nextTable = next.table === undefined ? tableId : next.table
    const nextColumn = next.column === undefined ? column : next.column
    if (nextTable) value.set('table', nextTable)
    if (nextTable && nextColumn) value.set('column', nextColumn)
    const nextTab = next.tab ?? tab
    if (nextTab !== 'columns') value.set('tab', nextTab)
    setParams(value)
  }

  return (
    <div className="meta-page">
      <aside className="meta-side">
        {tablesError && <Alert type="error" showIcon title="表清单读不出来" description={tablesError} />}
        <TableBrowser
          tables={tables}
          selected={tableId}
          onSelectTable={(id) => select({ table: id, column: null, tab: 'columns' })}
          onSelectColumn={(id, name) => select({ table: id, column: name, tab: 'lineage' })}
        />
      </aside>
      <main className="meta-main">
        <ServiceAlert />
        {!tableId && (
          <div className="meta-empty">
            <Empty description="从左边选一张表，或者搜一个字段" />
          </div>
        )}
        {tableId && (
          <>
            <div className="table-head">
              <div className="table-head-title">
                <Typography.Title level={4}>{table?.table_cn || tableId}</Typography.Title>
                {table && <Tag color={layerStyle(table.layer).color}>{table.layer}</Tag>}
                {table?.table_type && <Tag>{table.table_type}</Tag>}
              </div>
              <Typography.Text code copyable className="table-head-id">{tableId}</Typography.Text>
              {table?.description && <Typography.Paragraph type="secondary" className="table-desc">{table.description}</Typography.Paragraph>}
              {table && (
                <Descriptions size="small" column={{ xs: 1, sm: 3 }} items={[
                  { key: 'grain', label: '粒度', children: table.grain_columns.join('、') || '—' },
                  { key: 'date', label: '数据日期字段', children: table.date_column || '—' },
                  { key: 'count', label: '字段数', children: table.column_count },
                ]} />
              )}
            </div>
            {columnsError && <Alert type="error" showIcon title={columnsError} />}
            <Tabs
              activeKey={tab}
              onChange={(key) => select({ tab: key as TabKey })}
              items={[
                { key: 'columns', label: `字段清单（${columns.length}）`,
                  children: (
                    <ColumnTable columns={columns} loading={columnsLoading} selected={column}
                                 onSelect={(name) => select({ column: name, tab: 'lineage' })} />
                  ) },
                { key: 'lineage', label: column ? `字段血缘：${column}` : '字段血缘',
                  children: (
                    <LineagePanel
                      table={tableId}
                      columns={columns}
                      column={column}
                      onColumn={(name) => select({ column: name, tab: 'lineage' })}
                      onOpenField={(id, name) => select({ table: id, column: name, tab: 'lineage' })}
                    />
                  ) },
              ]}
            />
          </>
        )}
      </main>
    </div>
  )
}
