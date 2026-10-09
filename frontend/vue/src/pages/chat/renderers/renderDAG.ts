/**
 * DAG 可视化渲染器
 * 渲染任务依赖图的层级展示
 */
import { h } from 'vue';

// DAG 状态颜色映射
const DAG_STATUS_COLORS: Record<string, { bg: string; border: string; text: string; icon: string }> = {
  pending:   { bg: 'var(--color-bg-hover)', border: 'var(--color-border-dark)', text: 'var(--color-text-secondary)', icon: '⏳' },
  running:   { bg: 'var(--color-info-light)', border: 'var(--color-primary)', text: 'var(--color-info-text)', icon: '🔄' },
  completed: { bg: 'var(--color-success-light)', border: 'var(--color-success)', text: 'var(--color-success-text)', icon: '✅' },
  failed:    { bg: 'var(--color-danger-light)', border: 'var(--color-danger)', text: 'var(--color-danger-text)', icon: '❌' },
};

export function renderDAG(dag: any) {
  if (!dag || !dag.nodes || dag.nodes.length === 0) return null;

  const nodes = dag.nodes as Array<{
    id: string; name: string; status: string; description?: string; role?: string; depends_on?: string[]; result_summary?: string;
  }>;

  // 按层级分组节点（拓扑排序）
  const nodeMap = new Map<string, typeof nodes[0]>();
  nodes.forEach(n => nodeMap.set(n.id, n));

  // 计算每个节点的层级
  const nodeLayer = new Map<string, number>();
  const computeLayer = (nodeId: string, visited = new Set<string>()): number => {
    if (nodeLayer.has(nodeId)) return nodeLayer.get(nodeId)!;
    if (visited.has(nodeId)) return 0;
    visited.add(nodeId);
    const node = nodeMap.get(nodeId);
    if (!node || !node.depends_on || node.depends_on.length === 0) {
      nodeLayer.set(nodeId, 0);
      return 0;
    }
    const maxDepLayer = Math.max(...node.depends_on.map(d => computeLayer(d, visited)));
    const layer = maxDepLayer + 1;
    nodeLayer.set(nodeId, layer);
    return layer;
  };
  nodes.forEach(n => computeLayer(n.id));

  // 按层级分组
  const layers: typeof nodes[] = [];
  nodes.forEach(n => {
    const layer = nodeLayer.get(n.id) || 0;
    if (!layers[layer]) layers[layer] = [];
    layers[layer].push(n);
  });

  // 整体容器
  return h('div', {
    style: 'margin-top:0.5rem;padding:0.75rem;background:var(--color-bg-panel);border:1px solid var(--color-border);border-radius:0.5rem;',
  }, [
    // 标题
    h('div', {
      style: 'font-size:0.75rem;font-weight:600;color:var(--color-text);margin-bottom:0.5rem;display:flex;align-items:center;gap:0.25rem;',
    }, [
      h('span', {}, '🔀'),
      h('span', {}, `DAG 执行图 (${dag.status === 'completed' ? '已完成' : '运行中'})`),
    ]),
    // 节点层级展示
    h('div', {
      style: 'display:flex;flex-direction:column;gap:0.5rem;',
    }, layers.filter(Boolean).map((layerNodes, layerIdx) =>
      h('div', { key: `layer-${layerIdx}` }, [
        // 层级标签
        layerIdx > 0 ? h('div', {
          style: 'display:flex;align-items:center;gap:0.25rem;margin:0.25rem 0;',
        }, [
          h('span', { style: 'font-size:0.65rem;color:var(--color-text-disabled);' }, `↓ 依赖层 ${layerIdx}`),
        ]) : null,
        // 该层级的节点
        h('div', {
          style: 'display:flex;flex-wrap:wrap;gap:0.5rem;',
        }, layerNodes.map(node => {
          const statusStyle = DAG_STATUS_COLORS[node.status] || DAG_STATUS_COLORS.pending;
          return h('div', {
            key: node.id,
            style: `padding:0.5rem 0.75rem;border-radius:0.375rem;border:1px solid ${statusStyle.border};background:${statusStyle.bg};min-width:140px;max-width:220px;flex:1;`,
          }, [
            // 节点标题行
            h('div', {
              style: `display:flex;align-items:center;gap:0.25rem;font-size:0.75rem;font-weight:600;color:${statusStyle.text};`,
            }, [
              h('span', {
                style: node.status === 'running' ? 'animation:pulse 1.5s infinite;' : '',
              }, statusStyle.icon),
              h('span', { style: 'overflow:hidden;text-overflow:ellipsis;white-space:nowrap;' }, node.name),
            ]),
            // 描述
            node.description ? h('div', {
              style: 'font-size:0.65rem;color:var(--color-text-secondary);margin-top:0.25rem;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;',
              title: node.description,
            }, node.description) : null,
            // 结果摘要
            node.result_summary ? h('div', {
              style: 'font-size:0.6rem;color:var(--color-text-disabled);margin-top:0.125rem;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;',
              title: node.result_summary,
            }, node.result_summary) : null,
          ]);
        })),
      ])
    )),
  ]);
}
