/**
 * 工作台页面 - Dashboard
 */
import { defineComponent, h, ref, onMounted } from 'vue';
import { useAgentStore } from '../stores/agent';

export default defineComponent({
  name: 'Dashboard',
  setup() {
    const agentStore = useAgentStore();
    const stats = ref({
      todayTasks: 0,
      activeAgents: 0,
      toolCalls: 0,
      errorRate: 0,
    });

    onMounted(async () => {
      await agentStore.fetchAgents();
      stats.value.activeAgents = agentStore.activeAgentCount;
    });

    return () => h('div', { class: 'dashboard', style: 'height:100%;overflow-y:auto;padding:1.5rem;' }, [
      h('h1', { style: 'font-size:1.5rem;font-weight:700;margin-bottom:1.5rem;' }, '工作台'),
      
      // 概览卡片
      h('div', { style: 'display:grid;grid-template-columns:repeat(4,1fr);gap:1rem;margin-bottom:2rem;' }, [
        h('af-card', { title: '今日任务' }, [
          h('div', { style: 'font-size:2rem;font-weight:700;color:var(--color-primary);' }, String(stats.value.todayTasks)),
        ]),
        h('af-card', { title: '活跃Agent' }, [
          h('div', { style: 'font-size:2rem;font-weight:700;color:var(--color-success);' }, String(stats.value.activeAgents)),
        ]),
        h('af-card', { title: '工具调用' }, [
          h('div', { style: 'font-size:2rem;font-weight:700;color:var(--color-info);' }, String(stats.value.toolCalls)),
        ]),
        h('af-card', { title: '错误率' }, [
          h('div', { style: 'font-size:2rem;font-weight:700;color:var(--color-danger);' }, `${stats.value.errorRate}%`),
        ]),
      ]),

      // 快捷操作
      h('af-card', { title: '快捷操作' }, [
        h('div', { style: 'display:flex;gap:1rem;' }, [
          h('af-button', { variant: 'primary' }, '新建对话'),
          h('af-button', { variant: 'secondary' }, '创建Agent'),
          h('af-button', { variant: 'ghost' }, '上传文件'),
        ]),
      ]),

      // 系统状态
      h('af-card', { title: '系统状态', hoverable: true }, [
        h('div', { style: 'display:flex;gap:2rem;margin-top:0.5rem;' }, [
          h('div', {}, [
            h('span', { style: 'color:var(--color-text-secondary);' }, 'LLM API延迟: '),
            h('span', { style: 'font-weight:500;' }, '120ms'),
          ]),
          h('div', {}, [
            h('span', { style: 'color:var(--color-text-secondary);' }, '队列积压: '),
            h('span', { style: 'font-weight:500;' }, '0'),
          ]),
          h('div', {}, [
            h('span', { style: 'color:var(--color-text-secondary);' }, '内存使用: '),
            h('span', { style: 'font-weight:500;' }, '256MB'),
          ]),
        ]),
      ]),
    ]);
  },
});
