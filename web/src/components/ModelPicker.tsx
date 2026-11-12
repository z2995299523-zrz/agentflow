/** 会话内选择已配置的模型。 */
import { RobotOutlined, SettingOutlined } from '@ant-design/icons'
import { Button, Select, Tag, Tooltip, Typography } from 'antd'
import { Link } from 'react-router'
import { useModel } from '../state/model'
import { useChat } from '../state/chat'
import { useAuth } from '../state/auth'

export default function ModelPicker() {
  const { models, error } = useModel()
  const { model, setModel, modelSaving, modelError, loading, busy, domain, setDomain } = useChat()
  const { user, canManage } = useAuth()
  return (
    <div className="composer-model">
      <Typography.Text type="secondary">本会话模型</Typography.Text>
      {error ? <Tooltip title={error}><Tag color="error">模型列表取不到</Tag></Tooltip> : models?.length ? (
        <Select size="small" className="model-picker" aria-label="查数用的模型" prefix={<RobotOutlined />}
          value={model} onChange={(value) => { void setModel(value) }} disabled={loading || modelSaving || busy}
          popupMatchSelectWidth={false}
          options={model && !models.some((item) => item.name === model)
            ? [{ value: model, label: `${model}（不可用）`, disabled: true }, ...models.map((item) => ({ value: item.name, label: item.label }))]
            : models.map((item) => ({ value: item.name, label: item.label }))} />
      ) : <Tag>暂无可用模型</Tag>}
      {user && (user.admin || user.domains.length > 1) && <Select size="small" aria-label="本轮业务域"
        value={domain ?? ''} disabled={loading || busy} onChange={(value) => setDomain(value || undefined)}
        options={[{ value: '', label: user.admin ? '全部数据表' : '全部授权域' }, ...user.domains.map((item) => ({ value: item.code, label: item.name }))]} />}
      {canManage && <Tooltip title="模型配置"><Link to="/models"><Button size="small" icon={<SettingOutlined />} aria-label="模型配置" /></Link></Tooltip>}
      {modelError && <Typography.Text type="danger" role="alert">{modelError}</Typography.Text>}
      {model && models && !models.some((item) => item.name === model) && <Typography.Text type="danger" role="alert">此会话的模型不可用，请重新选择。</Typography.Text>}
    </div>
  )
}
