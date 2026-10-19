/** 页头右侧：当前连的资料目录、执行层、模型；元数据没导入、模型没配置时标红；ServiceAlert 是页面里的提示条 */
import { ApiOutlined, DatabaseOutlined, PlayCircleOutlined, RobotOutlined } from '@ant-design/icons'
import { Alert, Spin, Tag, Tooltip } from 'antd'

import { useHealth } from '../state/health'

const EXECUTOR_LABELS: Record<string, string> = { duckdb: '执行：DuckDB 模拟库', none: '只生成不执行' }

export default function StatusBar() {
  const { health, error } = useHealth()
  if (error) {
    return <Tooltip title={error}><Tag color="error" icon={<ApiOutlined />}>接口没连上</Tag></Tooltip>
  }
  if (!health) return <Spin size="small" />
  const counts = Object.entries(health.counts).map(([name, count]) => `${name} ${count.toLocaleString()}`).join(' · ')
  return (
    <div className="status-bar">
      <Tooltip title={counts || health.hint}>
        <Tag color="blue" icon={<DatabaseOutlined />} className="status-mart">资料目录：{health.mart_dir}</Tag>
      </Tooltip>
      <Tag icon={<PlayCircleOutlined />} className="status-extra">{EXECUTOR_LABELS[health.executor] ?? `执行：${health.executor}`}</Tag>
      <Tag icon={<RobotOutlined />} className="status-extra">{health.model}</Tag>
      {health.status !== 'ok' && (
        <Tooltip title={health.hint}><Tag color="error">{health.status === 'no_metadata' ? '元数据没导入' : '元数据出错'}</Tag></Tooltip>
      )}
      {health.status === 'ok' && health.hint && <Tooltip title={health.hint}><Tag color="warning">查数暂不可用</Tag></Tooltip>}
    </div>
  )
}

/** 页面顶部的提示条：接口没连上、元数据没导入；needModel 时没配模型 Key 也提示 */
export function ServiceAlert({ needModel = false }: { needModel?: boolean }) {
  const { health, error } = useHealth()
  if (error) {
    return (
      <Alert type="error" showIcon className="service-alert" title="连不上接口服务"
             description={<>{error}。请确认接口服务在运行：<code>uvicorn main:app --port 8000</code></>} />
    )
  }
  if (!health) return null
  if (health.status !== 'ok') {
    return (
      <Alert type="error" showIcon className="service-alert"
             title={health.status === 'no_metadata' ? '元数据库还没有导入' : '元数据读不出来'}
             description={<span className="pre-wrap">{health.hint}</span>} />
    )
  }
  if (needModel && health.hint) {
    return <Alert type="warning" showIcon className="service-alert" title="查数暂时不能用" description={health.hint} />
  }
  return null
}
