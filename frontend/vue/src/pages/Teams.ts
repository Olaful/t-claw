/**
 * 团队协作看板页面
 */
import { defineComponent, h, ref } from 'vue';

export default defineComponent({
  name: 'Teams',
  setup() {
    const teams = ref<any[]>([]);

    return () => h('div', { style: 'height:100%;overflow-y:auto;padding:1.5rem;' }, [
      h('div', { style: 'display:flex;justify-content:space-between;align-items:center;margin-bottom:1.5rem;' }, [
        h('h1', { style: 'font-size:1.5rem;font-weight:700;' }, '团队协作看板'),
        h('af-button', { variant: 'primary' }, '+ 创建团队'),
      ]),

      // 团队视图
      h('div', { style: 'display:grid;grid-template-columns:repeat(2,1fr);gap:1.5rem;margin-bottom:2rem;' }, [
        h('af-card', { title: '团队 Alpha', hoverable: true }, [
          h('div', { style: 'display:flex;gap:0.5rem;margin-bottom:1rem;' }, [
            h('af-badge', { variant: 'success' }, '活跃'),
            h('af-badge', { variant: 'info' }, '3 成员'),
          ]),
          h('div', { style: 'font-size:0.875rem;color:var(--color-text-secondary);' }, '数据处理团队，负责数据清洗和分析任务'),
        ]),
        h('af-card', { title: '团队 Beta', hoverable: true }, [
          h('div', { style: 'display:flex;gap:0.5rem;margin-bottom:1rem;' }, [
            h('af-badge', { variant: 'warning' }, '空闲'),
            h('af-badge', { variant: 'info' }, '2 成员'),
          ]),
          h('div', { style: 'font-size:0.875rem;color:var(--color-text-secondary);' }, '内容生成团队，负责文案和报告撰写'),
        ]),
      ]),

      // 任务白板
      h('af-card', { title: '任务白板' }, [
        h('div', { style: 'display:grid;grid-template-columns:repeat(3,1fr);gap:1rem;' }, [
          // 待认领
          h('div', {}, [
            h('h3', { style: 'font-size:0.875rem;font-weight:600;margin-bottom:0.75rem;color:var(--color-text-secondary);' }, '待认领'),
            h('div', { style: 'padding:0.75rem;background:var(--color-bg-hover);border-radius:0.5rem;margin-bottom:0.5rem;' }, [
              h('div', { style: 'font-weight:500;font-size:0.875rem;' }, '数据清洗任务'),
              h('af-badge', { variant: 'warning' }, '中优先级'),
            ]),
          ]),
          // 进行中
          h('div', {}, [
            h('h3', { style: 'font-size:0.875rem;font-weight:600;margin-bottom:0.75rem;color:var(--color-text-secondary);' }, '进行中'),
            h('div', { style: 'padding:0.75rem;background:var(--color-primary-light);border-radius:0.5rem;margin-bottom:0.5rem;' }, [
              h('div', { style: 'font-weight:500;font-size:0.875rem;' }, '报告生成'),
              h('af-badge', { variant: 'info' }, '团队Alpha'),
            ]),
          ]),
          // 已完成
          h('div', {}, [
            h('h3', { style: 'font-size:0.875rem;font-weight:600;margin-bottom:0.75rem;color:var(--color-text-secondary);' }, '已完成'),
            h('div', { style: 'padding:0.75rem;background:var(--color-success-light);border-radius:0.5rem;margin-bottom:0.5rem;' }, [
              h('div', { style: 'font-weight:500;font-size:0.875rem;' }, '日志分析'),
              h('af-badge', { variant: 'success' }, '完成'),
            ]),
          ]),
        ]),
      ]),
    ]);
  },
});
