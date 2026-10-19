/**
 * 查数页：左边会话列表，右边对话。地址 / 是新会话，/c/:id 是某个会话
 * 发出第一句话时才建会话；每一轮边跑边显示七步进度，跑完显示 SQL、口径说明、执行结果
 */
import { MenuOutlined, ReloadOutlined } from '@ant-design/icons'
import { Alert, Button, Drawer, Skeleton, Typography } from 'antd'
import type { TextAreaRef } from 'antd/es/input/TextArea'
import { useEffect, useRef, useState } from 'react'
import { useNavigate, useParams } from 'react-router'

import { AnswerCard, PendingCard } from '../components/AnswerCard'
import Composer from '../components/Composer'
import ConversationList from '../components/ConversationList'
import { ServiceAlert } from '../components/StatusBar'
import Welcome from '../components/Welcome'
import { useChat } from '../state/chat'
import { useHealth } from '../state/health'
import { formatTime } from '../utils/format'

function Question({ text, time }: { text: string; time?: string }) {
  return (
    <div className="question">
      <div className="question-bubble">{text}</div>
      {time && <div className="question-time">{formatTime(time)}</div>}
    </div>
  )
}

export default function QueryPage() {
  const { conversationId: routeId } = useParams()
  const navigate = useNavigate()
  const chat = useChat()
  const { health } = useHealth()
  const [draft, setDraft] = useState('')
  const [drawerOpen, setDrawerOpen] = useState(false)
  const scrollRef = useRef<HTMLDivElement>(null)
  const inputRef = useRef<TextAreaRef>(null)
  const { open } = chat

  useEffect(() => { open(routeId ?? null) }, [routeId, open])

  const turns = chat.detail?.turns ?? []
  const stepCount = chat.pending?.steps.length ?? 0
  const shownConversation = useRef<string | null>(null)
  useEffect(() => {                   // 有新内容时滚到最下面：刚打开的会话直接跳过去，跑的过程中平滑滚动
    const box = scrollRef.current
    if (!box) return
    const opened = shownConversation.current !== (chat.detail?.id ?? null)
    shownConversation.current = chat.detail?.id ?? null
    box.scrollTo({ top: box.scrollHeight, behavior: opened ? 'auto' : 'smooth' })
  }, [turns.length, stepCount, chat.pending?.error, chat.pending?.question, chat.detail?.id])

  const send = (text: string) => {
    if (!text.trim() || chat.busy) return
    setDraft('')
    void chat.ask(text)
  }

  const retry = () => {
    const question = chat.pending?.question
    chat.dismissPending()
    if (question) send(question)
  }

  const serviceDown = !health || health.status !== 'ok' || !!health.hint
  const showWelcome = !chat.loading && !chat.loadError && turns.length === 0 && !chat.pending

  return (
    <div className="query-page">
      <aside className="conv-panel"><ConversationList /></aside>
      <Drawer open={drawerOpen} placement="left" title="会话" onClose={() => setDrawerOpen(false)} size={300}>
        <ConversationList onNavigate={() => setDrawerOpen(false)} />
      </Drawer>
      <section className="chat">
        <div className="chat-header">
          <Button className="conv-toggle" type="text" icon={<MenuOutlined />} onClick={() => setDrawerOpen(true)} />
          <Typography.Text strong className="chat-title" ellipsis>
            {chat.detail?.title || (chat.conversationId ? '未命名会话' : '新会话')}
          </Typography.Text>
          {chat.detail && chat.detail.turn_count > 0 && (
            <Typography.Text type="secondary" className="chat-count">{chat.detail.turn_count} 轮</Typography.Text>
          )}
        </div>
        <div className="chat-scroll" ref={scrollRef}>
          <div className="chat-inner">
            <ServiceAlert needModel />
            {chat.loading && <Skeleton active paragraph={{ rows: 6 }} />}
            {chat.loadError && (
              <Alert type="error" showIcon title="会话读不出来" description={chat.loadError}
                     action={(
                       <Button size="small" icon={<ReloadOutlined />} onClick={() => navigate('/')}>新会话</Button>
                     )} />
            )}
            {showWelcome && <Welcome onPick={(text) => { setDraft(text); inputRef.current?.focus() }} />}
            {turns.map((turn) => (
              <div className="turn" key={turn.id}>
                <Question text={turn.question} time={turn.created_at} />
                <AnswerCard
                  result={turn.result}
                  clarifyActive={turn.id === chat.detail?.context.clarify_turn_id}
                  busy={chat.busy}
                  dialect={health?.dialect}
                  onPick={send}
                />
              </div>
            ))}
            {chat.pending && (
              <div className="turn">
                <Question text={chat.pending.question} />
                <PendingCard pending={chat.pending} onRetry={retry} onReload={chat.reloadDetail}
                             onDismiss={chat.dismissPending} />
              </div>
            )}
          </div>
        </div>
        <Composer
          value={draft}
          onChange={setDraft}
          onSend={() => send(draft)}
          busy={chat.busy}
          disabled={serviceDown && !!health}
          context={chat.pending ? null : chat.detail?.context ?? null}
          inputRef={inputRef}
        />
      </section>
    </div>
  )
}
