/**
 * 会话栏顶部「更多」折叠下拉菜单
 * - 固定菜单栏：位于「更多」上方，展示用户自定义固定的入口
 * - 更多下拉：主导航入口（工作台/Agent管理/团队协作/记忆管理）+ 快速工具（历史/收藏，占位）+ 自定义菜单栏
 * - 自定义菜单栏编辑框：勾选固定项（实时生效），底部栏支持恢复默认（默认固定「工作台」）
 */
import { defineComponent, h, ref, computed, onMounted, onUnmounted } from 'vue';
import { useRoute } from 'vue-router';

interface MenuEntry {
  icon: string;
  label: string;
  href?: string;         // 有 href 为路由跳转项
  action?: () => void;   // 占位项的预留 handler
}

const STORAGE_KEY = 'af-pinned-menu';
const DEFAULT_PINNED = ['工作台'];

export default defineComponent({
  name: 'MoreMenu',
  setup() {
    const entries: MenuEntry[] = [
      { icon: '📊', label: '工作台', href: '#/' },
      { icon: '🤖', label: 'Agent管理', href: '#/agents' },
      { icon: '👥', label: '团队协作', href: '#/teams' },
      { icon: '🧠', label: '记忆管理', href: '#/memory' },
      { icon: '🕐', label: '历史', action: () => { /* 占位: 后续实现历史功能 */ } },
      { icon: '⭐', label: '收藏', action: () => { /* 占位: 后续实现收藏功能 */ } },
    ];

    const open = ref(false);        // 「更多」下拉是否展开
    const editOpen = ref(false);    // 自定义菜单栏编辑框是否展开
    const hoverIdx = ref(-1);       // 下拉/编辑框列表项 hover
    const hoverBtn = ref(false);    // 「更多」按钮 hover
    const hoverPinIdx = ref(-1);    // 固定菜单栏项 hover
    const hoverReset = ref(false);  // 「恢复默认设置」按钮 hover
    const resetFlash = ref(false);  // 「恢复默认设置」点击瞬间底色反馈
    let resetTimer: ReturnType<typeof setTimeout> | null = null;
    const rootRef = ref<HTMLElement | null>(null);

    // 当前路由：用于固定项「所在页面已打开」的激活态高亮
    const route = useRoute();
    const isRouteActive = (href?: string): boolean => {
      if (!href) return false;
      const norm = (p: string) => (p.length > 1 && p.endsWith('/') ? p.slice(0, -1) : p);
      return norm(route.path) === norm(href.replace(/^#/, ''));
    };

    // 读取本地持久化的固定菜单
    const loadPinned = (): string[] => {
      try {
        const raw = localStorage.getItem(STORAGE_KEY);
        if (raw) {
          const arr = JSON.parse(raw);
          if (Array.isArray(arr)) {
            return arr.filter((l: string) => entries.some(e => e.label === l));
          }
        }
      } catch { /* ignore */ }
      return [...DEFAULT_PINNED];
    };

    const pinned = ref<string[]>(loadPinned());

    const persist = (arr: string[]) => {
      try { localStorage.setItem(STORAGE_KEY, JSON.stringify(arr)); } catch { /* ignore */ }
    };

    // 固定菜单栏展示的入口（按 entries 原始顺序）
    const pinnedEntries = computed(() => entries.filter(e => pinned.value.includes(e.label)));

    // 「更多」下拉展示的入口：排除已固定的项（已在固定菜单栏显示），移除固定后自动恢复到此
    const moreEntries = computed(() => entries.filter(e => !pinned.value.includes(e.label)));

    const togglePin = (label: string) => {
      pinned.value = pinned.value.includes(label)
        ? pinned.value.filter(l => l !== label)
        : [...pinned.value, label];
      persist(pinned.value);
    };

    const resetDefault = () => {
      pinned.value = [...DEFAULT_PINNED];
      persist(pinned.value);
      // 点击反馈：底色瞬间改变后自动还原（不改文字）
      resetFlash.value = true;
      if (resetTimer) clearTimeout(resetTimer);
      resetTimer = setTimeout(() => { resetFlash.value = false; }, 180);
    };

    const closeAll = () => { open.value = false; editOpen.value = false; hoverIdx.value = -1; };

    // 点击组件外部时关闭下拉与编辑框
    const onDocumentClick = (e: MouseEvent) => {
      if (!open.value && !editOpen.value) return;
      if (rootRef.value && !rootRef.value.contains(e.target as Node)) closeAll();
    };

    onMounted(() => document.addEventListener('click', onDocumentClick));
    onUnmounted(() => {
      document.removeEventListener('click', onDocumentClick);
      if (resetTimer) clearTimeout(resetTimer);
    });

    const itemBase = 'display:flex;align-items:center;gap:0.5rem;padding:0.5rem 1rem;font-size:0.8125rem;color:var(--color-text);cursor:pointer;text-decoration:none;';
    const panelStyle = 'position:absolute;top:100%;left:0;right:0;z-index:1000;background:var(--color-bg-card);border:1px solid var(--color-border);border-radius:0.5rem;box-shadow:0 4px 12px rgba(0,0,0,0.15);margin:0.25rem 0;';

    return () => h('div', {
      ref: rootRef,
      style: 'display:flex;flex-direction:column;flex-shrink:0;',
    }, [
      // ── 固定菜单栏（无固定项时不渲染、不占位）──
      pinnedEntries.value.length ? h('div', {
        style: 'display:flex;flex-direction:column;gap:0;padding:0.5rem 0 0;margin-bottom:0.375rem;',
      }, pinnedEntries.value.map((e, i) => {
        const active = isRouteActive(e.href);
        const bg = active
          ? 'background:var(--color-primary-light);'
          : hoverPinIdx.value === i
            ? 'background:var(--color-bg-hover);'
            : 'background:transparent;';
        const accent = active ? 'box-shadow:inset 0.1875rem 0 0 var(--color-primary);' : '';
        const handlers = {
          onMouseenter: () => { hoverPinIdx.value = i; },
          onMouseleave: () => { hoverPinIdx.value = -1; },
        };
        const pinStyle = `display:flex;align-items:center;gap:0.375rem;padding:0.5rem 0.5rem 0.5rem 1rem;font-size:0.875rem;color:${active ? 'var(--color-primary)' : 'var(--color-text)'};cursor:pointer;text-decoration:none;transition:background 0.2s,color 0.2s;${accent}${bg}`;
        const children = [
          h('span', { style: 'font-size:1rem;line-height:1;' }, e.icon),
          h('span', {}, e.label),
        ];
        return e.href
          ? h('a', { href: e.href, style: pinStyle, ...handlers }, children)
          : h('div', { onClick: () => e.action?.(), style: pinStyle, ...handlers }, children);
      })) : null,

      // ── 「更多」容器 ──
      h('div', { style: 'position:relative;' }, [
        // 触发按钮
        h('button', {
          onClick: () => { open.value = !open.value; editOpen.value = false; },
          onMouseenter: () => { hoverBtn.value = true; },
          onMouseleave: () => { hoverBtn.value = false; },
          style: `width:100%;display:flex;align-items:center;gap:0.5rem;padding:0.625rem 1rem;background:${hoverBtn.value ? 'var(--color-bg-hover)' : 'transparent'};border:none;cursor:pointer;font-size:0.875rem;color:var(--color-text);transition:background 0.2s;`,
        }, [
          h('span', { style: 'font-size:1rem;line-height:1;' }, '⋯'),
          h('span', {}, '更多'),
          h('span', {
            style: `margin-left:auto;font-size:0.625rem;color:var(--color-text-secondary);transition:transform 0.2s;transform:${open.value ? 'rotate(180deg)' : 'none'};`,
          }, '▼'),
        ]),

        // 下拉面板
        open.value ? h('div', { style: panelStyle + 'padding:0.25rem 0;' }, [
          ...moreEntries.value.map((it, idx) => {
            const bg = hoverIdx.value === idx ? 'background:var(--color-bg-hover);' : 'background:transparent;';
            const handlers = {
              onMouseenter: () => { hoverIdx.value = idx; },
              onMouseleave: () => { hoverIdx.value = -1; },
            };
            const children = [
              h('span', { style: 'font-size:1rem;line-height:1;' }, it.icon),
              h('span', {}, it.label),
            ];
            return it.href
              ? h('a', { href: it.href, onClick: closeAll, style: itemBase + bg, ...handlers }, children)
              : h('div', { onClick: () => { it.action?.(); closeAll(); }, style: itemBase + bg, ...handlers }, children);
          }),
          // 自定义菜单栏入口
          h('div', {
            onClick: (e: MouseEvent) => { e.stopPropagation(); open.value = false; editOpen.value = !editOpen.value; hoverIdx.value = -1; },
            onMouseenter: () => { hoverIdx.value = moreEntries.value.length; },
            onMouseleave: () => { hoverIdx.value = -1; },
            style: itemBase + (hoverIdx.value === moreEntries.value.length ? 'background:var(--color-bg-hover);' : 'background:transparent;'),
          }, [
            h('span', { style: 'font-size:1rem;line-height:1;' }, '📌'),
            h('span', {}, '自定义菜单栏'),
          ]),
        ]) : null,

        // 自定义菜单栏编辑框
        editOpen.value ? h('div', { style: panelStyle + 'overflow:hidden;' }, [
          // 菜单显示栏
          h('div', { style: 'padding:0.25rem 0;max-height:16rem;overflow-y:auto;' },
            entries.map((it, idx) => {
              const selected = pinned.value.includes(it.label);
              const bg = hoverIdx.value === idx ? 'background:var(--color-bg-hover);' : 'background:transparent;';
              return h('div', {
                onClick: () => togglePin(it.label),
                onMouseenter: () => { hoverIdx.value = idx; },
                onMouseleave: () => { hoverIdx.value = -1; },
                style: 'display:flex;align-items:center;gap:0.5rem;padding:0.5rem 1rem;font-size:0.875rem;color:var(--color-text);cursor:pointer;' + bg,
              }, [
                h('span', { style: 'width:1rem;text-align:center;font-size:0.75rem;color:var(--color-primary);line-height:1;' }, selected ? '✓' : ''),
                h('span', { style: 'font-size:1rem;line-height:1;' }, it.icon),
                h('span', {}, it.label),
              ]);
            }),
          ),
          // 底部栏
          h('div', { style: 'display:flex;justify-content:flex-end;padding:0.5rem;border-top:1px solid var(--color-border);' },
            h('button', {
              onClick: resetDefault,
              onMouseenter: () => { hoverReset.value = true; },
              onMouseleave: () => { hoverReset.value = false; },
              style: `background:${resetFlash.value ? 'var(--color-border)' : hoverReset.value ? 'var(--color-bg-hover)' : 'transparent'};border:1px solid var(--color-border);border-radius:0.375rem;padding:0.25rem 0.75rem;font-size:0.8125rem;color:var(--color-text);cursor:pointer;transition:background 0.15s;`,
            }, '恢复默认设置'),
          ),
        ]) : null,
      ]),
    ]);
  },
});
