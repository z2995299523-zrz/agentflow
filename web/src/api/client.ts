/**
 * 接口调用：普通请求和 SSE 流式请求
 * 所有请求都发到 /api 下，由 Vite 开发服务器或 nginx 去掉前缀后转给接口服务，浏览器不跨域。
 */
import type {
  ColumnInfo,
  ConversationDetail,
  ConversationSummary,
  ExampleInfo,
  Health,
  LineageInfo,
  ModelList,
  SearchResult,
  TableInfo,
  TraceStep,
  TurnEvent,
} from './types'

const API_BASE = '/api'

/** 接口报错：status 为 0 表示没连上 */
export class ApiError extends Error {
  readonly status: number

  constructor(status: number, message: string) {
    super(message)
    this.status = status
    this.name = 'ApiError'
  }
}

/** 从错误响应里取出原因：FastAPI 的 detail 是字符串（业务错误）或数组（参数校验错误） */
async function toApiError(response: Response): Promise<ApiError> {
  let detail = ''
  try {
    const body = (await response.json()) as { detail?: unknown }
    if (typeof body.detail === 'string') detail = body.detail
    else if (Array.isArray(body.detail)) detail = body.detail.map((item: { msg?: string }) => item.msg ?? '').join('；')
  } catch {
    // 不是 JSON：多半是代理没连上接口服务
  }
  if (!detail) {
    detail = response.status >= 500
      ? `接口服务出错或者没有启动（HTTP ${response.status}）`
      : `HTTP ${response.status} ${response.statusText}`
  }
  return new ApiError(response.status, detail)
}

/** 发请求，不成功时抛 ApiError */
async function send(path: string, init?: RequestInit): Promise<Response> {
  let response: Response
  try {
    response = await fetch(`${API_BASE}${path}`, {
      ...init,
      headers: { 'Content-Type': 'application/json', ...init?.headers },
    })
  } catch (error) {
    throw new ApiError(0, `连不上接口服务：${(error as Error).message}`)
  }
  if (!response.ok) throw await toApiError(response)
  return response
}

async function getJson<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await send(path, init)
  return (response.status === 204 ? undefined : await response.json()) as T
}

function query(params: Record<string, string | number | undefined>): string {
  const search = new URLSearchParams()
  for (const [key, value] of Object.entries(params)) {
    if (value !== undefined && value !== '') search.set(key, String(value))
  }
  const text = search.toString()
  return text ? `?${text}` : ''
}

// ── SSE ──────────────────────────────────────────

/** 把一段 SSE 消息拆成事件名和数据；冒号开头的是保活注释，没有数据行的块不算事件 */
function parseBlock(block: string): { event: string; data: unknown } | null {
  let event = 'message'
  const data: string[] = []
  for (const raw of block.split('\n')) {
    const line = raw.replace(/\r$/, '')
    if (!line || line.startsWith(':')) continue
    const colon = line.indexOf(':')
    const field = colon < 0 ? line : line.slice(0, colon)
    const value = colon < 0 ? '' : line.slice(colon + 1).replace(/^ /, '')
    if (field === 'event') event = value
    else if (field === 'data') data.push(value)
  }
  return data.length ? { event, data: JSON.parse(data.join('\n')) } : null
}

/** 边收边拆 SSE 响应体，每收到一条完整的事件就回调一次 */
export async function readEventStream(response: Response,
                                      onEvent: (event: string, data: unknown) => void): Promise<void> {
  if (!response.body) throw new ApiError(0, '浏览器不支持流式读取')
  const reader = response.body.pipeThrough(new TextDecoderStream()).getReader()
  let buffer = ''
  for (;;) {
    const { value, done } = await reader.read()
    if (done) break
    buffer += value
    let end = buffer.indexOf('\n\n')
    while (end >= 0) {
      const parsed = parseBlock(buffer.slice(0, end))
      buffer = buffer.slice(end + 2)
      if (parsed) onEvent(parsed.event, parsed.data)
      end = buffer.indexOf('\n\n')
    }
  }
  const rest = parseBlock(buffer)
  if (rest) onEvent(rest.event, rest.data)
}

export interface TurnHandlers {
  onStep: (step: TraceStep) => void
  onTurn: (event: TurnEvent) => void
}

/** 在会话里问一轮：边跑边回调每一步，最后回调存下的这一轮；出错或没收到结果时抛 ApiError。model 不传用默认模型 */
export async function streamTurn(conversationId: string, question: string, model: string | undefined,
                                 handlers: TurnHandlers): Promise<void> {
  const response = await send(`/conversations/${encodeURIComponent(conversationId)}/turns`, {
    method: 'POST',
    body: JSON.stringify({ question, model }),
    headers: { Accept: 'text/event-stream' },
  })
  let finished = false
  try {
    await readEventStream(response, (event, data) => {
      if (event === 'step') {
        handlers.onStep(data as TraceStep)
      } else if (event === 'turn') {
        finished = true
        handlers.onTurn(data as TurnEvent)
      } else if (event === 'error') {
        finished = true
        throw new ApiError(500, (data as { detail?: string }).detail ?? '接口出错')
      }
    })
  } catch (error) {
    if (error instanceof ApiError) throw error
    throw new ApiError(0, `连接中断：${(error as Error).message}`)
  }
  if (!finished) throw new ApiError(0, '连接中断：没有收到结果。这一轮可能已经在后台跑完，可以刷新会话看看')
}

// ── 普通接口 ──────────────────────────────────────

export const api = {
  health: () => getJson<Health>('/health'),
  models: () => getJson<ModelList>('/nl2sql/models'),

  conversations: (limit = 100) => getJson<ConversationSummary[]>(`/conversations${query({ limit })}`),
  createConversation: () => getJson<ConversationSummary>('/conversations', { method: 'POST', body: '{}' }),
  conversation: (id: string) => getJson<ConversationDetail>(`/conversations/${encodeURIComponent(id)}`),
  renameConversation: (id: string, title: string) =>
    getJson<ConversationSummary>(`/conversations/${encodeURIComponent(id)}`, {
      method: 'PATCH',
      body: JSON.stringify({ title }),
    }),
  deleteConversation: (id: string) =>
    getJson<void>(`/conversations/${encodeURIComponent(id)}`, { method: 'DELETE' }),

  tables: (layer?: string) => getJson<TableInfo[]>(`/metadata/tables${query({ layer })}`),
  columns: (table: string) => getJson<ColumnInfo[]>(`/metadata/columns${query({ table })}`),
  search: (q: string, limit = 50) => getJson<SearchResult>(`/metadata/search${query({ q, limit })}`),
  lineage: (table: string, column: string, depth: number) =>
    getJson<LineageInfo>(`/metadata/lineage${query({ table, column, depth })}`),
  examples: (limit = 6) => getJson<ExampleInfo[]>(`/metadata/examples${query({ limit })}`),
}

/** 错误转成给人看的一句话 */
export function errorText(error: unknown): string {
  if (error instanceof Error) return error.message
  return String(error)
}
