/**
 * AI Agent Framework - Lit UI组件库
 * 主入口文件
 */

// 导入所有组件以触发 @customElement 注册
import './components/index';
import './layouts/index';

// 导出类型供 TypeScript 使用
export type { AFButton, AFInput, AFCard, AFModal, AFBadge } from './components/index';
export type { AFLayout, AFConversationList, AFStatusBar } from './layouts/index';

// 导入样式
import './styles/theme.css';
