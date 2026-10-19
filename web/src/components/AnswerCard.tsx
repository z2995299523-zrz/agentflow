/** 一轮的回答：结论、七步进度、完整需求、反问或 SQL、校验结果、各项说明；正在跑的一轮单独一个卡片 */
import { LoadingOutlined, RedoOutlined, ReloadOutlined } from '@ant-design/icons'
import { Alert, Button, Space, Tag, Typography } from 'antd'
import { useEffect, useState } from 'react'

import { INTERNAL_STEP, STEP_LABELS, type QueryResult, type StepKey } from '../api/types'
import type { PendingTurn } from '../state/chat'
import { describeStatus, formatMs } from '../utils/format'
import ClarifyPanel from './ClarifyPanel'
import ResultTabs from './ResultTabs'
import SqlBlock from './SqlBlock'
import StepProgress from './StepProgress'

const SLOW_STEP_MS = 15000            // 这么久没有新的一步就说明可能的原因

function usageText(result: QueryResult): string {
  const parts = [`模型调用 ${result.calls.length} 次`, `用时 ${formatMs(result.elapsed_ms)}`]
  if (result.scope_level) parts.push(`上溯 ${result.scope_level} 层`)
  if (result.repair_rounds) parts.push(`修正 ${result.repair_rounds} 次`)
  return parts.join(' · ')
}

function failedAt(result: QueryResult): string {
  if (result.failed_step === INTERNAL_STEP) return '程序内部'
  return STEP_LABELS[result.failed_step as StepKey] ?? ''
}

function StatusAlert({ result }: { result: QueryResult }) {
  if (result.status === 'unvalidated') {
    return (
      <Alert type="warning" showIcon title={`修正 ${result.repair_rounds} 次后静态校验仍有错误，请人工检查后再使用`} />
    )
  }
  if (result.status !== 'failed') return null
  if (result.parsed && !result.parsed.is_query) {
    return <Alert type="info" showIcon title="这不是一个查数需求" description={result.message} />
  }
  const where = failedAt(result)
  return (
    <Alert type="error" showIcon title={`没有生成 SQL${where ? `，停在「${where}」` : ''}`}
           description={<span className="pre-wrap">{result.message}</span>} />
  )
}

function ValidationAlerts({ result }: { result: QueryResult }) {
  const validation = result.validation
  if (!validation || !result.sql) return null
  return (
    <>
      {validation.errors.length > 0 && (
        <Alert type="error" showIcon title={`静态校验错误（${validation.errors.length} 条）`}
               description={<ul className="plain-list">{validation.errors.map((item) => <li key={item}>{item}</li>)}</ul>} />
      )}
      {validation.warnings.length > 0 && (
        <Alert type="warning" showIcon title={`静态校验提醒（${validation.warnings.length} 条）`}
               description={<ul className="plain-list">{validation.warnings.map((item) => <li key={item}>{item}</li>)}</ul>} />
      )}
    </>
  )
}

interface AnswerProps {
  result: QueryResult
  clarifyActive: boolean
  busy: boolean
  dialect?: string
  onPick: (label: string) => void
}

export function AnswerCard({ result, clarifyActive, busy, dialect, onPick }: AnswerProps) {
  const status = describeStatus(result)
  return (
    <div className={`answer answer-${status.tone}`}>
      <div className="answer-head">
        <Tag color={status.color} className="status-tag">{status.text}</Tag>
        <Typography.Text type="secondary" className="answer-meta">{usageText(result)}</Typography.Text>
      </div>
      <StepProgress trace={result.trace} failedStep={result.failed_step} />
      {result.requirement && (
        <div className="requirement"><span className="requirement-label">完整需求</span>{result.requirement}</div>
      )}
      <StatusAlert result={result} />
      {result.status === 'need_clarify' && result.clarify && (
        <ClarifyPanel clarify={result.clarify} active={clarifyActive} disabled={busy} onPick={onPick} />
      )}
      {result.sql && (
        <SqlBlock sql={result.sql} dialect={dialect} validated={result.validation ? result.validation.ok : null} />
      )}
      <ValidationAlerts result={result} />
      <ResultTabs result={result} />
    </div>
  )
}

interface PendingProps {
  pending: PendingTurn
  onRetry: () => void
  onReload: () => void
  onDismiss: () => void
}

/** 正在跑的一轮：已经走完的步和它们的摘要；跑失败时给出原因，可以重发或刷新会话 */
export function PendingCard({ pending, onRetry, onReload, onDismiss }: PendingProps) {
  const [now, setNow] = useState(() => Date.now())
  const [lastEventAt, setLastEventAt] = useState(pending.startedAt)
  const running = !pending.error
  useEffect(() => {
    if (!running) return undefined
    const timer = window.setInterval(() => setNow(Date.now()), 200)
    return () => window.clearInterval(timer)
  }, [running])
  useEffect(() => { setLastEventAt(Date.now()) }, [pending.steps.length])
  const last = pending.steps[pending.steps.length - 1]
  const slow = running && now - lastEventAt > SLOW_STEP_MS

  return (
    <div className={`answer ${running ? 'answer-running' : 'answer-error'}`}>
      <div className="answer-head">
        {running ? (
          <Tag color="processing" icon={<LoadingOutlined />} className="status-tag">正在处理</Tag>
        ) : (
          <Tag color="error" className="status-tag">没有完成</Tag>
        )}
        <Typography.Text type="secondary" className="answer-meta">
          {running ? `已用 ${((now - pending.startedAt) / 1000).toFixed(1)} 秒` : ''}
          {last ? `${running ? ' · ' : ''}刚完成 ${last.label}` : ''}
        </Typography.Text>
      </div>
      <StepProgress trace={pending.steps} />
      {slow && (
        <Typography.Text type="secondary" className="slow-hint">
          这一步还在跑。接口刚启动时第一问要先加载模型库和向量模型，机械盘上可能要等几分钟；之后每一步通常几秒。
        </Typography.Text>
      )}
      {pending.steps.length > 0 && (
        <ul className="live-steps">
          {pending.steps.map((step, index) => (
            <li key={`${step.step}-${index}`}>
              <span className="trace-label">{step.label}</span>
              <span className="trace-ms">{formatMs(step.elapsed_ms)}</span>
              {step.summary && <span className="trace-summary">{step.summary}</span>}
            </li>
          ))}
        </ul>
      )}
      {pending.error && (
        <Alert
          type="error"
          showIcon
          title={pending.error}
          action={(
            <Space orientation="vertical" size="small">
              <Button size="small" type="primary" icon={<RedoOutlined />} onClick={onRetry}>重新发送</Button>
              <Button size="small" icon={<ReloadOutlined />} onClick={onReload}>刷新会话</Button>
              <Button size="small" type="text" onClick={onDismiss}>关闭</Button>
            </Space>
          )}
        />
      )}
    </div>
  )
}
