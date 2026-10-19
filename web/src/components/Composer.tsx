/**
 * 输入框：Enter 发送，Shift+Enter 换行；输入法选字时按 Enter 不发送
 * 上面一行写明这一句会怎么理解：回答上一轮的反问、接着上一轮追问，还是一个新需求
 */
import { BranchesOutlined, QuestionCircleOutlined, SendOutlined } from '@ant-design/icons'
import { Button, Input } from 'antd'
import type { TextAreaRef } from 'antd/es/input/TextArea'
import type { KeyboardEvent, Ref } from 'react'
import { useNavigate } from 'react-router'

import type { ConversationContext } from '../api/types'
import { todayText } from '../utils/format'

const PLACEHOLDER = '用自然语言描述查数需求：查什么指标、什么时间、按什么维度、口径上有什么限定'

interface Props {
  value: string
  onChange: (value: string) => void
  onSend: () => void
  busy: boolean
  disabled: boolean
  context: ConversationContext | null
  inputRef: Ref<TextAreaRef>
}

export default function Composer({ value, onChange, onSend, busy, disabled, context, inputRef }: Props) {
  const navigate = useNavigate()
  const onKeyDown = (event: KeyboardEvent<HTMLTextAreaElement>) => {
    if (event.key === 'Enter' && !event.shiftKey && !event.nativeEvent.isComposing) {
      event.preventDefault()
      if (!busy && !disabled) onSend()
    }
  }

  return (
    <div className="composer">
      {context?.prev_clarify ? (
        <div className="composer-context clarify">
          <QuestionCircleOutlined /> 上一轮在等你确认：{context.prev_clarify.question} 点上面的选项，或者直接写你的口径
        </div>
      ) : context?.prev_requirement ? (
        <div className="composer-context">
          <BranchesOutlined /> 追问会接着上一轮的需求理解：<span className="context-text">{context.prev_requirement}</span>
          <Button type="link" size="small" onClick={() => navigate('/')}>开新会话</Button>
        </div>
      ) : null}
      <div className="composer-box">
        <Input.TextArea
          ref={inputRef}
          value={value}
          onChange={(event) => onChange(event.target.value)}
          onKeyDown={onKeyDown}
          placeholder={PLACEHOLDER}
          autoSize={{ minRows: 2, maxRows: 6 }}
          maxLength={2000}
          disabled={disabled}
          variant="borderless"
        />
        <Button type="primary" icon={<SendOutlined />} loading={busy} disabled={disabled || !value.trim()} onClick={onSend}>
          {busy ? '处理中' : '发送'}
        </Button>
      </div>
      <div className="composer-hint">
        Enter 发送，Shift+Enter 换行 · 「现在」「今年」「9 月末」这类说法按今天 {todayText()} 换算 · SQL 请审核后再使用
      </div>
    </div>
  )
}
