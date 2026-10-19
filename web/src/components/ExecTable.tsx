/** 执行结果：分页表格、导出 CSV；结果被截断时写明只取回了多少行 */
import { DownloadOutlined } from '@ant-design/icons'
import { Alert, Button, Flex, Table, Typography } from 'antd'
import type { ColumnsType } from 'antd/es/table'
import { useMemo } from 'react'

import type { CellValue, ExecSummary } from '../api/types'
import { downloadCsv } from '../utils/download'
import { formatCell, formatMs, isNumeric } from '../utils/format'

type Row = Record<string, CellValue> & { key: number }

export default function ExecTable({ exec }: { exec: ExecSummary }) {
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
