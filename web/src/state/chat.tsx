/**
 * 查数会话的状态：当前会话、正在跑的这一轮、会话列表
 * 放在路由外层：切到元数据页再切回来，正在跑的查询和进度都还在。
 * 追问、反问的上下文由服务端从会话里算，这里只发本轮的问题和页头选的模型；服务端返回的 context 决定哪一轮的反问选项能点。
 */
import { createContext, useCallback, useContext, useEffect, useMemo, useRef, useState, type ReactNode } from 'react'
import { useNavigate } from 'react-router'

import { api, errorText, streamTurn } from '../api/client'
import type { ConversationDetail, ConversationSummary, TraceStep } from '../api/types'
import { useModel } from './model'

/** 正在跑（或者跑失败了）的一轮 */
export interface PendingTurn {
  conversationId: string | null
  question: string
  steps: TraceStep[]
  startedAt: number
  error: string                       // 非空表示这一轮没跑完：没连上、接口报错、连接中断
}

interface ChatState {
  conversationId: string | null
  detail: ConversationDetail | null
  loading: boolean
  loadError: string
  pending: PendingTurn | null
  busy: boolean                       // 有一轮正在跑（不管是不是当前会话的）：一次只跑一轮
  conversations: ConversationSummary[]
  listError: string
  open: (id: string | null) => void
  ask: (question: string) => Promise<void>
  dismissPending: () => void
  reloadDetail: () => void
  refreshList: () => void
  rename: (id: string, title: string) => Promise<void>
  remove: (id: string) => Promise<void>
}

const ChatContext = createContext<ChatState | null>(null)

export function ChatProvider({ children }: { children: ReactNode }) {
  const navigate = useNavigate()
  const { model } = useModel()
  const [conversationId, setConversationId] = useState<string | null>(null)
  const [detail, setDetail] = useState<ConversationDetail | null>(null)
  const [loading, setLoading] = useState(false)
  const [loadError, setLoadError] = useState('')
  const [pending, setPending] = useState<PendingTurn | null>(null)
  const [conversations, setConversations] = useState<ConversationSummary[]>([])
  const [listError, setListError] = useState('')
  const [running, setRunning] = useState(false)
  const activeId = useRef<string | null>(null)       // 回调里判断事件是不是当前会话的
  const runningRef = useRef(false)

  const refreshList = useCallback(() => {
    api.conversations()
      .then((items) => {
        setConversations(items)
        setListError('')
      })
      .catch((error: unknown) => setListError(errorText(error)))
  }, [])

  useEffect(refreshList, [refreshList])

  const load = useCallback((id: string) => {
    setLoading(true)
    setLoadError('')
    api.conversation(id)
      .then((value) => {
        if (activeId.current === id) setDetail(value)
      })
      .catch((error: unknown) => {
        if (activeId.current === id) setLoadError(errorText(error))
      })
      .finally(() => {
        if (activeId.current === id) setLoading(false)
      })
  }, [])

  const open = useCallback((id: string | null) => {
    if (id === activeId.current) return
    activeId.current = id
    setConversationId(id)
    setDetail(null)
    setLoadError('')
    setPending((current) => (current && current.conversationId === id ? current : null))
    if (id) load(id)
    else setLoading(false)
  }, [load])

  const ask = useCallback(async (question: string) => {
    const text = question.trim()
    if (!text || runningRef.current) return
    runningRef.current = true
    setRunning(true)
    let id = activeId.current
    setPending({ conversationId: id, question: text, steps: [], startedAt: Date.now(), error: '' })
    try {
      if (!id) {
        const created = await api.createConversation()
        id = created.id
        activeId.current = id
        setConversationId(id)
        setDetail({ ...created, turns: [], context: { prev_requirement: '', prev_clarify: null, clarify_turn_id: null } })
        setPending((current) => (current ? { ...current, conversationId: id } : current))
        navigate(`/c/${id}`, { replace: true })
        refreshList()
      }
      const target = id
      await streamTurn(target, text, model, {
        onStep: (step) => {
          if (activeId.current !== target) return
          setPending((current) => (current ? { ...current, steps: [...current.steps, step] } : current))
        },
        onTurn: ({ turn, context }) => {
          if (activeId.current !== target) return
          setDetail((current) => current && {
            ...current,
            title: current.title || turn.question.trim().split(/\s+/).join(' ').slice(0, 40),
            turn_count: current.turn_count + 1,
            updated_at: turn.created_at,
            turns: [...current.turns, turn],
            context,
          })
          setPending(null)
        },
      })
    } catch (error) {
      const message = errorText(error)
      if (activeId.current === id) {
        setPending((current) => (current ? { ...current, error: message } : current))
      }
    } finally {
      runningRef.current = false
      setRunning(false)
      refreshList()
    }
  }, [navigate, refreshList, model])

  const dismissPending = useCallback(() => setPending(null), [])

  const reloadDetail = useCallback(() => {
    if (activeId.current) {
      setPending(null)
      load(activeId.current)
    }
  }, [load])

  const rename = useCallback(async (id: string, title: string) => {
    const summary = await api.renameConversation(id, title)
    setConversations((items) => items.map((item) => (item.id === id ? summary : item)))
    setDetail((current) => (current && current.id === id ? { ...current, title: summary.title } : current))
  }, [])

  const remove = useCallback(async (id: string) => {
    await api.deleteConversation(id)
    setConversations((items) => items.filter((item) => item.id !== id))
    if (activeId.current === id) navigate('/')
  }, [navigate])

  const value = useMemo<ChatState>(() => ({
    conversationId, detail, loading, loadError, pending,
    busy: running,
    conversations, listError, open, ask, dismissPending, reloadDetail, refreshList, rename, remove,
  }), [conversationId, detail, loading, loadError, pending, running, conversations, listError, open, ask,
       dismissPending, reloadDetail, refreshList, rename, remove])

  return <ChatContext.Provider value={value}>{children}</ChatContext.Provider>
}

export function useChat(): ChatState {
  const value = useContext(ChatContext)
  if (!value) throw new Error('useChat 要放在 ChatProvider 里面用')
  return value
}
