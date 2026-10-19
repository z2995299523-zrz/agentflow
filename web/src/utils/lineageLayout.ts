/**
 * 血缘图的排版：按跳数分列，上游在左、下游在右，选中的字段在正中
 * - 上游第 d 跳的来源字段放在第 -d 列，下游第 d 跳加工出的字段放在第 +d 列；
 *   同一个字段在几跳里都出现时，放在离中心最近的那一列（接口给的跳是广度优先的，先出现的就是最近的）
 * - 每一列里按「和里面一列相连的字段的平均位置」排序（重心法），连线交叉少；从中心一列一列往外排
 * - 同一对字段之间的多条血缘（比如两个脚本都写了它）合成一条连线，hops 里保留全部
 * 和接口返回的数据结构绑定，不依赖画图组件，方便单独测试
 */
import type { Hop, LineageInfo } from '../api/types'

export const NODE_WIDTH = 220
export const NODE_HEIGHT = 66
const GAP_X = 80
const GAP_Y = 16

export interface LayoutNode {
  id: string                     // 模式.表.字段
  column: number                 // 负数是上游，正数是下游，0 是选中的字段
  x: number
  y: number
}

export interface LayoutEdge {
  id: string
  source: string
  target: string                 // target 由 source 加工而来
  direction: 'upstream' | 'downstream'
  hops: Hop[]
}

export interface LineageLayout {
  nodes: LayoutNode[]
  edges: LayoutEdge[]
}

export function layoutLineage(info: LineageInfo): LineageLayout {
  const column = new Map<string, number>([[info.field, 0]])
  for (const hop of info.upstream) if (!column.has(hop.source)) column.set(hop.source, -hop.depth)
  for (const hop of info.downstream) if (!column.has(hop.target)) column.set(hop.target, hop.depth)

  const edges = new Map<string, LayoutEdge>()
  const neighbors = new Map<string, Set<string>>()
  const link = (a: string, b: string) => {
    if (!neighbors.has(a)) neighbors.set(a, new Set())
    neighbors.get(a)!.add(b)
  }
  const addEdge = (hop: Hop, direction: LayoutEdge['direction']) => {
    if (hop.source === hop.target) return                 // 自己加工自己（脚本里的 UPDATE）不画线
    const id = `${hop.source}->${hop.target}`
    const edge = edges.get(id)
    if (edge) {
      edge.hops.push(hop)
      return
    }
    edges.set(id, { id, source: hop.source, target: hop.target, direction, hops: [hop] })
    link(hop.source, hop.target)
    link(hop.target, hop.source)
  }
  info.upstream.forEach((hop) => addEdge(hop, 'upstream'))
  info.downstream.forEach((hop) => addEdge(hop, 'downstream'))

  const byColumn = new Map<number, string[]>()
  for (const [id, index] of column) {
    if (!byColumn.has(index)) byColumn.set(index, [])
    byColumn.get(index)!.push(id)
  }
  const position = new Map<string, number>()
  const order = [...byColumn.keys()].sort((a, b) => Math.abs(a) - Math.abs(b) || a - b)
  for (const index of order) {
    const ids = byColumn.get(index)!
    if (index !== 0) {
      const inner = index < 0 ? index + 1 : index - 1
      const weight = (id: string): number => {
        const places = [...(neighbors.get(id) ?? [])]
          .filter((other) => column.get(other) === inner && position.has(other))
          .map((other) => position.get(other)!)
        return places.length ? places.reduce((sum, value) => sum + value, 0) / places.length : Number.MAX_SAFE_INTEGER
      }
      const weights = new Map(ids.map((id) => [id, weight(id)]))
      ids.sort((a, b) => weights.get(a)! - weights.get(b)!)          // sort 是稳定的：同分时保持接口给的顺序
    }
    ids.forEach((id, place) => position.set(id, place))
  }

  const nodes: LayoutNode[] = []
  for (const [index, ids] of byColumn) {
    ids.forEach((id, place) => {
      nodes.push({
        id,
        column: index,
        x: index * (NODE_WIDTH + GAP_X),
        y: (place - (ids.length - 1) / 2) * (NODE_HEIGHT + GAP_Y),
      })
    })
  }
  return { nodes, edges: [...edges.values()] }
}

/** 一跳的加工写法：脚本表达式，没有时用血缘清单里的加工逻辑，直取写「直取」 */
export function hopExpression(hop: Hop): string {
  return hop.script_expression || hop.transform || (hop.is_passthrough ? '直取' : '')
}
