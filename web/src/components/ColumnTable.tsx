/** 一张表的字段清单：可以按字段名、中文名、说明筛选；点一行看这个字段的血缘 */
import { ApartmentOutlined, KeyOutlined, SearchOutlined } from '@ant-design/icons'
import { Button, Input, Table, Tag, Tooltip, Typography } from 'antd'
import { useMemo, useState } from 'react'

import type { ColumnInfo } from '../api/types'

interface Props {
  columns: ColumnInfo[]
  loading: boolean
  selected: string | null
  onSelect: (column: string) => void
}

export default function ColumnTable({ columns, loading, selected, onSelect }: Props) {
  const [keyword, setKeyword] = useState('')
  const shown = useMemo(() => {
    const word = keyword.trim().toLowerCase()
    if (!word) return columns
    return columns.filter((column) => [column.column_name, column.column_cn, column.description]
      .some((text) => text.toLowerCase().includes(word)))
  }, [columns, keyword])

  return (
    <div className="column-table">
      <Input
        allowClear
        className="column-filter"
        prefix={<SearchOutlined />}
        placeholder="在这张表里筛字段"
        value={keyword}
        onChange={(event) => setKeyword(event.target.value)}
      />
      <Table<ColumnInfo>
        size="small"
        rowKey="column_name"
        loading={loading}
        dataSource={shown}
        rowClassName={(column) => (column.column_name === selected ? 'row-selected' : '')}
        onRow={(column) => ({ onClick: () => onSelect(column.column_name) })}
        pagination={{ defaultPageSize: 20, showSizeChanger: true, pageSizeOptions: [20, 50, 100, 500],
                      showTotal: (total) => `共 ${total} 个字段`, hideOnSinglePage: true }}
        scroll={{ x: 960 }}
        columns={[
          { key: 'ordinal', title: '#', dataIndex: 'ordinal', width: 52 },
          { key: 'name', title: '字段', dataIndex: 'column_name', width: 200,
            render: (name: string, column) => (
              <span className="column-name">
                <code>{name}</code>
                {column.is_pk && <Tooltip title="主键"><KeyOutlined className="pk-icon" /></Tooltip>}
              </span>
            ) },
          { key: 'cn', title: '中文名', dataIndex: 'column_cn', width: 160 },
          { key: 'type', title: '类型', dataIndex: 'data_type', width: 120,
            render: (type: string) => <Typography.Text type="secondary">{type}</Typography.Text> },
          { key: 'lineage', title: '血缘', width: 110,
            render: (_, column) => (
              column.upstream_count || column.downstream_count ? (
                <Tooltip title={`直接来源 ${column.upstream_count} 条、直接加工出 ${column.downstream_count} 个字段`}>
                  <span className="lineage-count">
                    <Tag variant="filled" color={column.upstream_count ? 'blue' : 'default'}>↑{column.upstream_count}</Tag>
                    <Tag variant="filled" color={column.downstream_count ? 'purple' : 'default'}>↓{column.downstream_count}</Tag>
                  </span>
                </Tooltip>
              ) : <Typography.Text type="secondary">—</Typography.Text>
            ) },
          { key: 'description', title: '口径说明', dataIndex: 'description', ellipsis: { showTitle: false },
            render: (text: string) => <Tooltip title={text} placement="topLeft">{text}</Tooltip> },
          { key: 'enum', title: '码值', dataIndex: 'enum_values', width: 160, ellipsis: { showTitle: false },
            render: (text: string) => <Tooltip title={text} placement="topLeft">{text}</Tooltip> },
          { key: 'action', title: '', width: 72, fixed: 'right',
            render: (_, column) => (
              <Button size="small" type="link" icon={<ApartmentOutlined />}
                      onClick={(event) => { event.stopPropagation(); onSelect(column.column_name) }}>血缘</Button>
            ) },
        ]}
      />
    </div>
  )
}
