/** 反问：每个选项一张可以点的卡片，写明对应哪个字段、怎么加工；点一下就是回答，发出下一轮 */
import { QuestionCircleFilled } from '@ant-design/icons'
import { Typography } from 'antd'

import type { Clarify } from '../api/types'

interface Props {
  clarify: Clarify
  active: boolean                     // 只有还没回答的那次反问能点
  skipped: boolean                    // 不能点了，是因为用户没回答、问了新问题（不是回答过了）
  disabled: boolean                   // 有一轮正在跑
  onPick: (label: string) => void
}

function tipOf(active: boolean, skipped: boolean): string {
  if (active) return '点一个选项继续，也可以在下面直接写你的口径；不想回答，直接问新的问题也行'
  return skipped ? '这次反问没有回答，后面换成了新的问题' : '这次反问已经回答过了'
}

export default function ClarifyPanel({ clarify, active, skipped, disabled, onPick }: Props) {
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
      <Typography.Text type="secondary" className="clarify-tip">{tipOf(active, skipped)}</Typography.Text>
    </div>
  )
}
