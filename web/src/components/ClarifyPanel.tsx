/** 反问：每个选项一张可以点的卡片，写明对应哪个字段、怎么加工；点一下就是回答，发出下一轮 */
import { QuestionCircleFilled } from '@ant-design/icons'
import { Typography } from 'antd'

import type { Clarify } from '../api/types'

interface Props {
  clarify: Clarify
  active: boolean                     // 只有还没回答的那次反问能点
  disabled: boolean                   // 有一轮正在跑
  onPick: (label: string) => void
}

export default function ClarifyPanel({ clarify, active, disabled, onPick }: Props) {
  return (
    <div className="clarify">
      <div className="clarify-question"><QuestionCircleFilled /> {clarify.question}</div>
      <div className="clarify-options">
        {clarify.options.map((option) => (
          <button
            key={option.label}
            type="button"
            className="clarify-option"
            disabled={!active || disabled}
            onClick={() => onPick(option.label)}
          >
            <span className="clarify-label">{option.label}</span>
            {option.meaning && <span className="clarify-meaning">{option.meaning}</span>}
          </button>
        ))}
      </div>
      <Typography.Text type="secondary" className="clarify-tip">
        {active ? '点一个选项继续，也可以在下面直接输入你的口径' : '这次反问已经回答过了'}
      </Typography.Text>
    </div>
  )
}
