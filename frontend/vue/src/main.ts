/**
 * Vue应用入口 - TypeScript动态生成
 */
import { createApp, defineComponent, h, ref, onMounted } from 'vue';
import { RouterView, useRoute } from 'vue-router';
import { createPinia } from 'pinia';
import { router } from './router';
import { useAgentStore } from './stores/agent';
import { useChatStore } from './stores/chat';
import Layout from './components/Layout.vue';
import MoreMenu from './components/MoreMenu';
import './styles/markdown.css';  // ✅ 导入 Markdown 样式

// 导入Lit组件库（通过vite别名）
import 'lit-components';

// ===== 主题初始化（在挂载前应用，避免刷新闪烁）=====
type ThemeMode = 'light' | 'dark';
const THEME_STORAGE_KEY = 'af-theme';
const getSavedTheme = (): ThemeMode => {
  try {
    const saved = localStorage.getItem(THEME_STORAGE_KEY);
    return saved === 'dark' ? 'dark' : 'light';
  } catch {
    return 'light';
  }
};
const applyTheme = (theme: ThemeMode) => {
  document.documentElement.setAttribute('data-theme', theme);
};
// 立即应用已保存的主题
applyTheme(getSavedTheme());

// ===== 主应用组件 =====
const App = defineComponent({
  name: 'App',
  setup() {
    const route = useRoute();
    const agentStore = useAgentStore();
    const chatStore = useChatStore();
    const currentTheme = ref<ThemeMode>(getSavedTheme());

    const toggleTheme = () => {
      currentTheme.value = currentTheme.value === 'light' ? 'dark' : 'light';
      applyTheme(currentTheme.value);
      try {
        localStorage.setItem(THEME_STORAGE_KEY, currentTheme.value);
      } catch {
        // 忽略存储异常（如隐私模式）
      }
    };

    // 处理新建会话：即时创建 NEW 会话（含工作区）并跳转到聊天页面
    const handleNewConversation = async () => {
      await chatStore.createConversation(getAgentId());
      router.push('/chat/');
    };

    // 获取当前 agentId（从路由参数或默认值）
    const getAgentId = () => {
      const routeAgentId = route.params.agentId;
      return routeAgentId && typeof routeAgentId === 'string' ? routeAgentId : 'default-agent';
    };

    // 全局加载会话列表（在所有页面都执行）
    onMounted(async () => {
      const agentId = getAgentId();
      await chatStore.loadConversations(agentId);
      
      // 如果当前在聊天页面且有会话，加载消息
      if (route.path.startsWith('/chat') && chatStore.activeConversationId) {
        await chatStore.loadMessages(agentId, chatStore.activeConversationId);
      }
    });

    return () => h(Layout, {}, {
      nav: () => [
        // logo 右侧：搜索图标按钮（占位入口）
        h('button', {
          title: '搜索',
          onClick: () => { /* 占位: 后续实现搜索功能 */ },
          style: 'display:flex;align-items:center;justify-content:center;background:transparent;border:none;padding:0.375rem;border-radius:0.375rem;cursor:pointer;font-size:1rem;line-height:1;color:var(--color-text-secondary);transition:background 0.2s;',
        }, '🔍'),
      ],
      'header-right': () => [
        h('span', { style: 'color:var(--color-text-secondary);font-size:0.875rem;' }, '管理员'),
      ],
      sidebar: () => h('div', { style: 'display:flex;flex-direction:column;height:100%;' }, [
        h(MoreMenu),
        h('div', { style: 'flex:1;min-height:0;' }, [
          h('af-conversation-list', {
        conversations: chatStore.conversations,
        activeId: chatStore.activeConversationId,
        'onAf-new-conversation': handleNewConversation,
        'onAf-select-conversation': async (e: CustomEvent) => {
          // 从路由参数获取 agentId，如果没有则使用默认值
          const routeAgentId = router.currentRoute.value.params.agentId;
          const agentId = routeAgentId && typeof routeAgentId === 'string' ? routeAgentId : 'default-agent';
          
          // ✅ 先加载会话消息
          await chatStore.selectConversation(agentId, e.detail.id);
          
          // ✅ 然后跳转到聊天页面（如果当前不在聊天页面）
          if (!router.currentRoute.value.path.startsWith('/chat')) {
            await router.push('/chat/');
          }
        },
        'onAf-rename-conversation': async (e: CustomEvent) => {
          // 处理重命名会话
          const { id, title } = e.detail;
          const routeAgentId = router.currentRoute.value.params.agentId;
          const agentId = routeAgentId && typeof routeAgentId === 'string' ? routeAgentId : 'default-agent';
          
          try {
            const response = await fetch(`http://localhost:8000/api/v1/chat/conversations/${id}?agent_id=${agentId}`, {
              method: 'PUT',
              headers: { 'Content-Type': 'application/json' },
              body: JSON.stringify({ title }),
            });
            
            if (response.ok) {
              // 更新本地状态
              const conv = chatStore.conversations.find(c => c.id === id);
              if (conv) {
                conv.title = title;
              }
              console.log('Conversation renamed successfully:', id, title);
            } else {
              console.error('Failed to rename conversation:', response.status);
            }
          } catch (error) {
            console.error('Error renaming conversation:', error);
          }
        },
        'onAf-delete-conversation': async (e: CustomEvent) => {
          // 处理删除会话
          const { id } = e.detail;
          const routeAgentId = router.currentRoute.value.params.agentId;
          const agentId = routeAgentId && typeof routeAgentId === 'string' ? routeAgentId : 'default-agent';
          
          try {
            const response = await fetch(`http://localhost:8000/api/v1/chat/conversations/${id}?agent_id=${agentId}`, {
              method: 'DELETE',
            });
            
            if (response.ok) {
              // 从本地状态中移除
              chatStore.conversations = chatStore.conversations.filter(c => c.id !== id);
              
              // 如果删除的是当前激活的会话，清空消息
              if (chatStore.activeConversationId === id) {
                chatStore.activeConversationId = '';
                chatStore.messages = [];
                
                // 如果有其他会话，选择第一个
                if (chatStore.conversations.length > 0) {
                  const firstConv = chatStore.conversations[0];
                  await chatStore.selectConversation(agentId, firstConv.id);
                  if (!router.currentRoute.value.path.startsWith('/chat')) {
                    await router.push('/chat/');
                  }
                } else {
                  // 没有会话了，跳转到工作台
                  await router.push('/');
                }
              }
              
              console.log('Conversation deleted successfully:', id);
            } else {
              console.error('Failed to delete conversation:', response.status);
            }
          } catch (error) {
            console.error('Error deleting conversation:', error);
          }
        },
          }),
        ]),
      ]),
      footer: () => [
        // 底部栏左侧：设置 + 主题切换（纯图标）
        h('div', { style: 'display:flex;align-items:center;gap:0.5rem;' }, [
          h('button', {
            title: '设置',
            onClick: () => router.push('/settings'),
            style: 'display:flex;align-items:center;justify-content:center;background:transparent;border:none;padding:0.25rem;cursor:pointer;font-size:1rem;line-height:1;opacity:0.85;transition:opacity 0.2s;',
          }, '⚙️'),
          h('button', {
            onClick: toggleTheme,
            title: currentTheme.value === 'light' ? '切换到暗黑模式' : '切换到浅色模式',
            style: 'display:flex;align-items:center;justify-content:center;background:transparent;border:none;padding:0.25rem;cursor:pointer;font-size:1rem;line-height:1;opacity:0.85;transition:opacity 0.2s;',
          }, currentTheme.value === 'light' ? '☀️' : '🌙'),
        ]),
        // 底部栏右侧：状态栏
        h('af-status-bar', {
          status: agentStore.isConnected ? 'connected' : 'disconnected',
          tokenUsage: agentStore.totalTokenUsage,
          activeAgents: agentStore.activeAgentCount,
        }),
      ],
      default: () => h(RouterView),
    });
  },
});

// 创建应用
const app = createApp(App);
const pinia = createPinia();

app.use(pinia);
app.use(router);

// 添加打字机动画样式
const style = document.createElement('style');
style.textContent = `
@keyframes typing-bounce {
  0%, 80%, 100% {
    transform: scale(0.6);
    opacity: 0.5;
  }
  40% {
    transform: scale(1);
    opacity: 1;
  }
}
`;
document.head.appendChild(style);

// 挂载应用
app.mount('#app');

export { App };
