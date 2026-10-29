/** 页头右上角的模型下拉框：只列配了 Key 的模型，选择记在本机浏览器里，之后的查数、追问都用它 */
import { RobotOutlined } from '@ant-design/icons'
import { Select, Tag, Tooltip } from 'antd'

import { useModel } from '../state/model'

export default function ModelPicker() {
  const { models, model, error, setModel } = useModel()
  if (error) {
    return <Tooltip title={error}><Tag color="error" icon={<RobotOutlined />}>模型列表取不到</Tag></Tooltip>
  }
  if (!models) return null
  if (!models.length) {
    return (
      <Tooltip title="在 .env 里配好模型的 Key 后重启接口服务">
        <Tag color="warning" icon={<RobotOutlined />}>没有能用的模型</Tag>
      </Tooltip>
    )
  }
  return (
    <Select
      size="small"
      className="model-picker"
      aria-label="查数用的模型"
      prefix={<RobotOutlined />}
      value={model}
      onChange={setModel}
      popupMatchSelectWidth={false}
      options={models.map((item) => ({ value: item.name, label: item.label }))}
    />
  )
}
