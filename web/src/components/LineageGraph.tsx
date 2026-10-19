/**
 * 字段血缘图：上游在左、下游在右，选中的字段在正中（排版见 utils/lineageLayout）
 * 点字段看字段说明，点连线看这一跳的加工表达式、过滤条件、关联条件，双击字段以它为中心重新展开
 * 连线：虚线是直取，蓝线是有加工表达式，橙线是加工时带了过滤或关联条件
 */
import {
  Background,
  Controls,
  Handle,
  MarkerType,
  MiniMap,
  Position,
  ReactFlow,
  type Edge,
  type Node,
  type NodeProps,
} from '@xyflow/react'
import '@xyflow/react/dist/style.css'
import { useMemo } from 'react'

import type { FieldBrief, LineageInfo } from '../api/types'
import { layerStyle, splitField } from '../utils/format'
import { NODE_HEIGHT, NODE_WIDTH, hopExpression, layoutLineage, type LayoutEdge } from '../utils/lineageLayout'

const EDGE_LABEL_LIMIT = 16                 // 连线不多于这么多条时，线上写加工表达式的开头
const LABEL_CHARS = 18
const FOCUS_COLUMNS = 2                     // 打开时让中心字段前后两跳占满画布，更远的拖动或看小地图
const MIN_FIT_ZOOM = 0.4                    // 字段很多时不再缩小，免得字看不清
const COLORS = { pass: '#94a3b8', calc: '#3b82f6', cond: '#f59e0b' }
const LAYER_COLORS: Record<string, string> = { ods: '#16a34a', dwd: '#2563eb', ads: '#7c3aed', ext: '#94a3b8' }

export type FieldNodeData = { brief: FieldBrief; center: boolean; column: number }
type FieldNode = Node<FieldNodeData, 'field'>

function briefOf(info: LineageInfo, id: string): FieldBrief {
  const known = info.fields[id]
  if (known) return known
  const { table, column } = splitField(id)
  return { table, column, table_cn: '', layer: '', column_cn: '', known: false }
}

function FieldNodeView({ data }: NodeProps<FieldNode>) {
  const { brief, center } = data
  const layer = layerStyle(brief.layer)
  return (
    <div className={`field-node layer-${layer.key}${center ? ' center' : ''}${brief.known ? '' : ' unknown'}`}
         style={{ width: NODE_WIDTH, height: NODE_HEIGHT }}>
      <Handle type="target" position={Position.Left} isConnectable={false} />
      <div className="fn-table" title={brief.table}>
        <span className="fn-layer">{layer.short}</span>{brief.table_cn || brief.table}
      </div>
      <div className="fn-column" title={`${brief.table}.${brief.column}`}>{brief.column}</div>
      <div className="fn-cn">{brief.column_cn || (brief.known ? '' : '不在字段清单里')}</div>
      <Handle type="source" position={Position.Right} isConnectable={false} />
    </div>
  )
}

const nodeTypes = { field: FieldNodeView }

function edgeKind(edge: LayoutEdge): keyof typeof COLORS {
  if (edge.hops.some((hop) => hop.filters.length > 0 || hop.joins.length > 0)) return 'cond'
  return edge.hops.every((hop) => hop.is_passthrough && !hop.script_expression) ? 'pass' : 'calc'
}

function edgeLabel(edge: LayoutEdge): string {
  const text = hopExpression(edge.hops[0]).replace(/\s+/g, ' ')
  return text.length > LABEL_CHARS ? `${text.slice(0, LABEL_CHARS)}…` : text
}

interface Props {
  info: LineageInfo
  onSelectNode: (fieldId: string) => void
  onSelectEdge: (edge: LayoutEdge) => void
  onClear: () => void
  onOpenField: (fieldId: string) => void
}

export default function LineageGraph({ info, onSelectNode, onSelectEdge, onClear, onOpenField }: Props) {
  const layout = useMemo(() => layoutLineage(info), [info])
  const edgeById = useMemo(() => new Map(layout.edges.map((edge) => [edge.id, edge])), [layout])
  const fitViewOptions = useMemo(() => ({
    nodes: layout.nodes.filter((node) => Math.abs(node.column) <= FOCUS_COLUMNS).map((node) => ({ id: node.id })),
    padding: 0.1,
    minZoom: MIN_FIT_ZOOM,
    maxZoom: 1,
  }), [layout])

  const nodes = useMemo<FieldNode[]>(() => layout.nodes.map((node) => ({
    id: node.id,
    type: 'field',
    position: { x: node.x, y: node.y },
    data: { brief: briefOf(info, node.id), center: node.id === info.field, column: node.column },
    selected: node.id === info.field,
  })), [layout, info])

  const edges = useMemo<Edge[]>(() => {
    const showLabels = layout.edges.length <= EDGE_LABEL_LIMIT
    return layout.edges.map((edge) => {
      const color = COLORS[edgeKind(edge)]
      return {
        id: edge.id,
        source: edge.source,
        target: edge.target,
        type: 'default',
        label: showLabels ? edgeLabel(edge) : undefined,
        labelBgPadding: [4, 2] as [number, number],
        labelBgBorderRadius: 4,
        labelStyle: { fontSize: 11, fill: '#334155' },
        style: { stroke: color, strokeWidth: 1.6, strokeDasharray: edgeKind(edge) === 'pass' ? '5 4' : undefined },
        markerEnd: { type: MarkerType.ArrowClosed, color, width: 16, height: 16 },
        interactionWidth: 16,
      }
    })
  }, [layout])

  return (
    <div className="lineage-canvas">
      <ReactFlow<FieldNode, Edge>
        defaultNodes={nodes}
        defaultEdges={edges}
        nodeTypes={nodeTypes}
        fitView
        fitViewOptions={fitViewOptions}
        minZoom={0.15}
        maxZoom={1.6}
        nodesConnectable={false}
        onNodeClick={(_, node) => onSelectNode(node.id)}
        onNodeDoubleClick={(_, node) => onOpenField(node.id)}
        onEdgeClick={(_, edge) => {
          const found = edgeById.get(edge.id)
          if (found) onSelectEdge(found)
        }}
        onPaneClick={onClear}
      >
        <Background gap={18} size={1} color="#e2e8f0" />
        <Controls showInteractive={false} />
        <MiniMap pannable zoomable style={{ width: 150, height: 96 }}
                 nodeColor={(node) => LAYER_COLORS[layerStyle((node.data as FieldNodeData).brief.layer).key]} />
      </ReactFlow>
      <div className="lineage-legend">
        <span><i className="lg-line pass" />直取</span>
        <span><i className="lg-line calc" />有加工表达式</span>
        <span><i className="lg-line cond" />带过滤或关联条件</span>
        <span className="lg-tip">点连线看加工，双击字段以它为中心展开</span>
      </div>
    </div>
  )
}
