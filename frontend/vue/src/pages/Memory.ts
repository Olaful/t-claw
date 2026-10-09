/**
 * 记忆管理页面 - Memory
 * 左侧记忆文件列表（按 mtime 降序），右侧 Markdown 内容展示，顶部 MEMORY.md 索引
 */
import { defineComponent, h, ref, computed, onMounted } from 'vue';
import { marked } from 'marked';

const API_BASE = 'http://localhost:8000/api/v1';

interface MemoryItem {
  filename: string;
  path: string;
  name: string;
  description: string;
  type: string;
  scope: string;
  mtime: number;
}

// 记忆类型标签颜色
const TYPE_COLORS: Record<string, string> = {
  user: 'var(--color-primary)',
  feedback: '#f59e0b',
  reference: '#8b5cf6',
  project: '#10b981',
};

function formatTime(ts: number): string {
  if (!ts) return '-';
  const d = new Date(ts * 1000);
  const pad = (n: number) => String(n).padStart(2, '0');
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}`;
}

export default defineComponent({
  name: 'Memory',
  setup() {
    const scope = ref<'personal' | 'team'>('personal');
    const items = ref<MemoryItem[]>([]);
    const indexContent = ref('');
    const selected = ref<MemoryItem | null>(null);
    const selectedContent = ref('');
    const selectedMeta = ref<Record<string, string>>({});
    const loading = ref(false);
    const error = ref('');

    const totalLabel = computed(() => `共 ${items.value.length} 条记忆`);

    async function loadData(force = false) {
      loading.value = true;
      error.value = '';
      try {
        const [listRes, indexRes] = await Promise.all([
          fetch(`${API_BASE}/memory/list?scope=${scope.value}${force ? '&refresh=true' : ''}`),
          fetch(`${API_BASE}/memory/index?scope=${scope.value}`),
        ]);
        if (!listRes.ok) throw new Error(`HTTP ${listRes.status}`);
        const listData = await listRes.json();
        items.value = listData.items || [];
        const indexData = indexRes.ok ? await indexRes.json() : { content: '' };
        indexContent.value = indexData.content || '';
      } catch (e) {
        error.value = e instanceof Error ? e.message : '加载失败';
        items.value = [];
        indexContent.value = '';
      } finally {
        loading.value = false;
      }
    }

    async function openFile(item: MemoryItem) {
      selected.value = item;
      selectedContent.value = '';
      selectedMeta.value = {};
      try {
        const res = await fetch(
          `${API_BASE}/memory/file?path=${encodeURIComponent(item.path)}&scope=${item.scope}`,
        );
        if (!res.ok) throw new Error(`HTTP ${res.status}`);
        const data = await res.json();
        selectedContent.value = data.content || '';
        selectedMeta.value = data.frontmatter || {};
      } catch (e) {
        selectedContent.value = `> 读取失败：${e instanceof Error ? e.message : '未知错误'}`;
      }
    }

    function typeTag(type: string) {
      const color = TYPE_COLORS[type] || '#6b7280';
      return h(
        'span',
        {
          style: `display:inline-block;padding:1px 8px;border-radius:10px;font-size:0.7rem;color:#fff;background:${color};margin-left:0.5rem;vertical-align:middle;`,
        },
        type || 'unknown',
      );
    }

    // ===== 左侧列表 =====
    function renderList() {
      if (loading.value) {
        return h('div', { style: 'padding:1rem;color:var(--color-text-secondary);' }, '加载中...');
      }
      if (!items.value.length) {
        return h('div', { style: 'padding:1rem;color:var(--color-text-secondary);' }, '暂无记忆文件');
      }
      return items.value.map((item) =>
        h(
          'div',
          {
            key: item.path,
            onClick: () => openFile(item),
            style: `padding:0.6rem 0.8rem;border-radius:6px;cursor:pointer;margin-bottom:4px;border:1px solid ${
              selected.value?.path === item.path ? 'var(--color-primary)' : 'var(--color-border)'
            };background:${selected.value?.path === item.path ? 'var(--color-primary-lighter)' : 'var(--color-bg-card)'};`,
          },
          [
            h('div', { style: 'display:flex;align-items:center;' }, [
              h('span', { style: 'font-weight:600;font-size:0.85rem;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;' }, item.name),
              typeTag(item.type),
            ]),
            h(
              'div',
              { style: 'font-size:0.75rem;color:var(--color-text-secondary);margin-top:2px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;' },
              item.description || '(无描述)',
            ),
            h('div', { style: 'font-size:0.7rem;color:var(--color-text-disabled);margin-top:2px;' }, formatTime(item.mtime)),
          ],
        ),
      );
    }

    // ===== 顶部索引 =====
    function renderIndex() {
      const html = indexContent.value
        ? (marked.parse(indexContent.value, { async: false }) as string)
        : '<em style="color:var(--color-text-disabled);">索引为空</em>';
      return h('af-card', { title: `MEMORY.md 索引（${scope.value}）` }, [
        h('div', { class: 'markdown-body', style: 'max-height:180px;overflow-y:auto;font-size:0.85rem;', innerHTML: html }),
      ]);
    }

    // ===== 右侧内容 =====
    function renderDetail() {
      if (!selected.value) {
        return h('div', { style: 'color:var(--color-text-disabled);padding:2rem;text-align:center;' }, '从左侧选择一个记忆文件查看内容');
      }
      const meta = selectedMeta.value;
      const metaItems = ['type', 'scope', 'created_at', 'updated_at']
        .filter((k) => meta[k])
        .map((k) =>
          h('span', { key: k, style: 'margin-right:1rem;font-size:0.75rem;color:var(--color-text-secondary);' }, `${k}: ${meta[k]}`),
        );
      const html = marked.parse(selectedContent.value || '', { async: false }) as string;
      return [
        h('div', { style: 'display:flex;align-items:center;gap:0.5rem;margin-bottom:0.5rem;' }, [
          h('span', { style: 'font-size:1.1rem;font-weight:700;' }, selected.value.name),
          typeTag(selected.value.type),
          h('span', { style: 'font-size:0.75rem;color:var(--color-text-disabled);margin-left:auto;' }, selected.value.path),
        ]),
        metaItems.length ? h('div', { style: 'margin-bottom:0.8rem;' }, metaItems) : null,
        h('div', { class: 'markdown-body', style: 'border:1px solid var(--color-border);border-radius:8px;padding:1rem;overflow-y:auto;', innerHTML: html }),
      ];
    }

    onMounted(() => loadData());

    return () =>
      h('div', { class: 'memory-page', style: 'height:100%;overflow-y:auto;padding:1.5rem;' }, [
        // 页头：标题 + scope 切换 + 刷新
        h('div', { style: 'display:flex;align-items:center;gap:1rem;margin-bottom:1rem;' }, [
          h('h1', { style: 'font-size:1.5rem;font-weight:700;' }, '记忆管理'),
          h('div', { style: 'display:flex;gap:0.5rem;margin-left:auto;' }, [
            h(
              'af-button',
              {
                variant: scope.value === 'personal' ? 'primary' : 'secondary',
                onClick: () => { scope.value = 'personal'; selected.value = null; loadData(); },
              },
              '个人记忆',
            ),
            h(
              'af-button',
              {
                variant: scope.value === 'team' ? 'primary' : 'secondary',
                onClick: () => { scope.value = 'team'; selected.value = null; loadData(); },
              },
              '团队记忆',
            ),
            h('af-button', { variant: 'ghost', onClick: () => loadData(true) }, '刷新'),
          ]),
        ]),

        error.value
          ? h('div', { style: 'color:var(--color-danger);margin-bottom:1rem;' }, `错误：${error.value}`)
          : null,

        // 顶部索引
        h('div', { style: 'margin-bottom:1rem;' }, [renderIndex()]),

        // 主体：左右分栏
        h('div', { style: 'display:grid;grid-template-columns:320px 1fr;gap:1rem;align-items:start;' }, [
          h('af-card', { title: totalLabel.value }, [
            h('div', { style: 'max-height:65vh;overflow-y:auto;' }, renderList()),
          ]),
          h('af-card', { title: '记忆内容' }, [
            h('div', { style: 'min-height:300px;' }, renderDetail()),
          ]),
        ]),
      ]);
  },
});
