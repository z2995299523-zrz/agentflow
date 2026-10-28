/**
 * 接口返回的数据结构，对应后端 nl2sql/schemas.py、nl2sql/history.py 和 main.py 里的模型
 * 只写了界面用到的字段；后端加字段不影响这里
 */

export type Status = 'ok' | 'need_clarify' | 'unvalidated' | 'failed'

/** 流水线的七步，和 nl2sql/schemas.py 的 STEPS、STEP_LABELS 一一对应 */
export const STEPS = ['parse', 'recall', 'evidence', 'plan', 'generate', 'validate', 'output'] as const
export type StepKey = (typeof STEPS)[number]
export const STEP_LABELS: Record<StepKey, string> = {
  parse: '① 需求解析',
  recall: '② 候选召回',
  evidence: '③ 血缘展开',
  plan: '④ 选表定口径',
  generate: '⑤ 生成 SQL',
  validate: '⑥ 静态校验',
  output: '⑦ 输出',
}
export const INTERNAL_STEP = 'internal'

export interface TraceStep {
  step: string
  label: string
  elapsed_ms: number
  summary: string
}

export interface ClarifyOption {
  label: string
  meaning: string
}

export interface Clarify {
  question: string
  options: ClarifyOption[]
}

export interface ParsedRequest {
  is_query: boolean
  reply: string
  answers_clarify?: boolean           // 这一轮是不是在回答上一轮的反问；早先存下的轮次没有这一项
  requirement: string
}

export interface PlanTable {
  table: string
  reason: string
}

export interface PlanField {
  table: string
  column: string
  usage: 'metric' | 'dimension' | 'filter' | 'join' | 'order' | 'other'
  reason: string
}

export interface PlanJoin {
  left_table: string
  left_columns: string[]
  right_table: string
  right_columns: string[]
  join_type: 'INNER' | 'LEFT'
  reason: string
}

export interface QueryPlan {
  decision: 'ok' | 'need_clarify' | 'need_upstream' | 'cannot_answer'
  tables: PlanTable[]
  fields: PlanField[]
  joins: PlanJoin[]
  filters: string[]
  caliber: string
  table_basis: string
  join_basis: string
  lineage_note: string
  assumptions: string[]
  examples_used: string[]
  clarify: Clarify | null
  missing: string[]
  reason: string
  dropped_tables: string[]
}

export interface ValidationSummary {
  ok: boolean
  errors: string[]
  warnings: string[]
  tables: string[]
  columns: string[]
}

export type CellValue = string | number | boolean | null

export interface ExecSummary {
  executed: boolean
  columns: string[]
  rows: CellValue[][]
  row_count: number
  truncated: boolean
  elapsed_ms: number
  error: string
  note: string
}

export interface CallRecord {
  step: string
  attempt: number
  elapsed_ms: number
  input_tokens: number
  output_tokens: number
  ok: boolean
  error: string
}

export interface QueryResult {
  status: Status
  question: string
  requirement: string
  parsed: ParsedRequest | null
  sql: string
  plan: QueryPlan | null
  clarify: Clarify | null
  message: string
  failed_step: string
  assumptions: string[]
  validation: ValidationSummary | null
  first_pass_ok: boolean | null
  repair_rounds: number
  scope_level: number
  candidate_tables: string[]
  recall_notes: string[]
  exec_result: ExecSummary | null
  trace: TraceStep[]
  calls: CallRecord[]
  elapsed_ms: number
}

// ── 会话 ──────────────────────────────────────────

export interface ConversationSummary {
  id: string
  title: string
  created_at: string
  updated_at: string
  turn_count: number
}

export interface Turn {
  id: number
  question: string
  created_at: string
  result: QueryResult
}

export interface ConversationContext {
  prev_requirement: string
  prev_clarify: Clarify | null
  clarify_turn_id: number | null
}

export interface ConversationDetail extends ConversationSummary {
  turns: Turn[]
  context: ConversationContext
}

/** POST /conversations/{id}/turns 最后一条 turn 事件的数据 */
export interface TurnEvent {
  turn: Turn
  context: ConversationContext
}

// ── 元数据 ────────────────────────────────────────

export interface TableInfo {
  table_id: string
  table_cn: string
  layer: string
  table_type: string
  grain_columns: string[]
  date_column: string | null
  description: string
  column_count: number
}

export interface ColumnInfo {
  column_name: string
  column_cn: string
  ordinal: number
  data_type: string
  is_pk: boolean
  description: string
  enum_values: string
  upstream_count: number
  downstream_count: number
}

export interface ColumnHit {
  table_id: string
  table_cn: string
  layer: string
  column_name: string
  column_cn: string
  data_type: string
}

export interface SearchResult {
  tables: TableInfo[]
  columns: ColumnHit[]
  table_total: number
  column_total: number
}

/** 血缘的一跳：target 由 source 加工而来 */
export interface Hop {
  depth: number
  target: string
  source: string
  transform: string
  script_expression: string
  is_passthrough: boolean
  origin: string
  script_name: string
  source_external: boolean
  source_known: boolean
  filters: string[]
  joins: string[]
}

export interface FieldBrief {
  table: string
  column: string
  table_cn: string
  layer: string
  column_cn: string
  known: boolean
}

export interface LineageInfo {
  field: string
  upstream: Hop[]
  downstream: Hop[]
  fields: Record<string, FieldBrief>
}

export interface ExampleInfo {
  example_id: string
  requirement: string
}

export interface Health {
  status: 'ok' | 'no_metadata' | 'error'
  mart_dir: string
  counts: Record<string, number>
  executor: string
  model: string
  dialect: string
  hint: string
}

export const LAYERS = ['贴源层', '标准层', '汇总层', '专题层'] as const
