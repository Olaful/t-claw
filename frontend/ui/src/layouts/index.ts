/**
 * AI Agent Framework - 布局组件
 */
import { LitElement, html, css } from 'lit';
import { customElement, property } from 'lit/decorators.js';

// ===== 主布局 =====
@customElement('af-layout')
export class AFLayout extends LitElement {
  static styles = css`
    :host {
      display: flex;
      flex-direction: column;
      min-height: 100vh;
      font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
    }
    .header {
      display: flex;
      align-items: center;
      justify-content: space-between;
      padding: 0 1.5rem;
      height: 3.5rem;
      background: var(--color-bg-header, #1f2937);
      color: white;
      border-bottom: 1px solid var(--color-border, #374151);
    }
    .header-left { display: flex; align-items: center; gap: 2rem; }
    .logo { font-size: 1.25rem; font-weight: 700; }
    .nav { display: flex; gap: 0.5rem; }
    .nav-item {
      padding: 0.5rem 1rem;
      border-radius: 0.375rem;
      color: rgba(255,255,255,0.7);
      cursor: pointer;
      transition: all 0.2s;
      text-decoration: none;
      font-size: 0.875rem;
    }
    .nav-item:hover, .nav-item.active { background: rgba(255,255,255,0.1); color: white; }
    .header-right { display: flex; align-items: center; gap: 1rem; }
    .body { display: flex; flex: 1; overflow: hidden; }
    .sidebar {
      width: 16rem;
      background: var(--color-bg-sidebar, #f9fafb);
      border-right: 1px solid var(--color-border, #e5e7eb);
      overflow-y: auto;
      padding: 1rem;
    }
    .main { flex: 1; overflow-y: auto; padding: 1.5rem; background: var(--color-bg-main, #ffffff); }
    .footer {
      padding: 0.5rem 1.5rem;
      background: var(--color-bg-footer, #f9fafb);
      border-top: 1px solid var(--color-border, #e5e7eb);
      font-size: 0.75rem;
      color: var(--color-text-secondary, #6b7280);
      display: flex;
      justify-content: space-between;
    }
    /* Shadow DOM 内滚动条主题化（变量可继承）*/
    ::-webkit-scrollbar { width: 6px; height: 6px; }
    ::-webkit-scrollbar-track { background: transparent; }
    ::-webkit-scrollbar-thumb { background: var(--color-border-dark, #d1d5db); border-radius: 9999px; }
    ::-webkit-scrollbar-thumb:hover { background: var(--color-secondary, #6b7280); }
  `;

  render() {
    return html`
      <header class="header">
        <div class="header-left">
          <span class="logo">🤖 AI Agent</span>
          <nav class="nav">
            <slot name="nav"></slot>
          </nav>
        </div>
        <div class="header-right">
          <slot name="header-right"></slot>
        </div>
      </header>
      <div class="body">
        <aside class="sidebar">
          <slot name="sidebar"></slot>
        </aside>
        <main class="main">
          <slot></slot>
        </main>
      </div>
      <footer class="footer">
        <slot name="footer"></slot>
      </footer>
    `;
  }
}

// ===== 侧边栏会话列表 =====
@customElement('af-conversation-list')
export class AFConversationList extends LitElement {
  @property({ type: Array }) conversations: Array<{ id: string; title: string; message_count: number }> = [];
  @property({ type: String }) activeId = '';
  @property({ type: String }) hoveredId = '';  // 当前悬停的会话 ID
  @property({ type: Boolean }) showMenu = false;  // 是否显示菜单
  @property({ type: Object }) menuPosition: { x: number; y: number } | null = null;  // 菜单位置
  @property({ type: String }) menuConversationId = '';  // 菜单对应的会话 ID

  static styles = css`
    :host { display: flex; flex-direction: column; height: 100%; overflow: hidden; }
    .list-header { display: flex; justify-content: space-between; align-items: center; padding: 1rem 1rem 0.75rem; flex-shrink: 0; border-bottom: 1px solid var(--color-border, #e5e7eb); }
    .list-title { font-weight: 600; font-size: 0.875rem; color: var(--color-text, #374151); }
    .new-btn { background: var(--color-primary, #3b82f6); color: white; border: none; border-radius: 0.375rem; padding: 0.25rem 0.5rem; cursor: pointer; font-size: 0.75rem; }
    .list-body { flex: 1; overflow-y: auto; padding: 0.75rem 1rem; }
    /* Shadow DOM 内滚动条主题化（变量可继承）*/
    ::-webkit-scrollbar { width: 6px; height: 6px; }
    ::-webkit-scrollbar-track { background: transparent; }
    ::-webkit-scrollbar-thumb { background: var(--color-border-dark, #d1d5db); border-radius: 9999px; }
    ::-webkit-scrollbar-thumb:hover { background: var(--color-secondary, #6b7280); }
    .item {
      display: flex; align-items: center; justify-content: space-between;
      padding: 0.5rem 0.75rem;
      border-radius: 0.375rem;
      cursor: pointer;
      transition: background 0.15s;
      margin-bottom: 0.25rem;
      position: relative;
    }
    .item:hover { background: var(--color-bg-hover, #f3f4f6); }
    .item.active { background: var(--color-primary-light, #dbeafe); }
    .item-left { display: flex; align-items: center; gap: 0.5rem; flex: 1; overflow: hidden; }
    .item-title { font-size: 0.875rem; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; max-width: 10rem; }
    .item-count { font-size: 0.75rem; color: var(--color-text-secondary, #9ca3af); }
    .item-actions {
      opacity: 0;
      transition: opacity 0.15s;
      display: flex;
      align-items: center;
    }
    .item:hover .item-actions {
      opacity: 1;
    }
    .menu-btn {
      background: none;
      border: none;
      cursor: pointer;
      padding: 0.25rem;
      border-radius: 0.25rem;
      color: var(--color-text-secondary, #6b7280);
      display: flex;
      align-items: center;
      justify-content: center;
    }
    .menu-btn:hover {
      background: var(--color-bg-hover, #e5e7eb);
      color: var(--color-text, #374151);
    }
    .dots {
      display: flex;
      gap: 2px;
    }
    .dot {
      width: 3px;
      height: 3px;
      background: currentColor;
      border-radius: 50%;
    }
    .section { margin-top: 1.5rem; }
    .section-title { font-size: 0.75rem; font-weight: 600; color: var(--color-text-secondary, #6b7280); margin-bottom: 0.5rem; text-transform: uppercase; }
    
    /* 下拉菜单样式 */
    .dropdown-menu {
      position: fixed;
      background: var(--color-bg-card, white);
      border: 1px solid var(--color-border, #e5e7eb);
      border-radius: 0.5rem;
      box-shadow: 0 4px 12px rgba(0,0,0,0.1);
      z-index: 1000;
      min-width: 120px;
      padding: 0.25rem 0;
    }
    .menu-item {
      padding: 0.5rem 1rem;
      font-size: 0.875rem;
      cursor: pointer;
      display: flex;
      align-items: center;
      gap: 0.5rem;
      color: var(--color-text, #374151);
    }
    .menu-item:hover {
      background: var(--color-bg-hover, #f3f4f6);
    }
    .menu-item.danger {
      color: var(--color-danger, #ef4444);
    }
    .menu-icon {
      font-size: 1rem;
    }
  `;

  render() {
    return html`
      <div class="list-header">
        <span class="list-title">会话列表</span>
        <button class="new-btn" @click=${this._newConversation}>+ 新建</button>
      </div>
      <div class="list-body">
      ${this.conversations.map(conv => html`
        <div 
          class="item ${conv.id === this.activeId ? 'active' : ''}" 
          @mouseenter=${() => this._handleMouseEnter(conv.id)}
          @mouseleave=${() => this._handleMouseLeave()}
        >
          <div class="item-left" @click=${() => this._selectConversation(conv.id)}>
            <span class="item-title">${conv.title || '新会话'}</span>
            <span class="item-count">${conv.message_count}</span>
          </div>
          <div class="item-actions">
            <button 
              class="menu-btn"
              @click=${(e: MouseEvent) => this._showMenu(e, conv.id)}
              title="更多操作"
            >
              <div class="dots">
                <span class="dot"></span>
                <span class="dot"></span>
                <span class="dot"></span>
              </div>
            </button>
          </div>
        </div>
      `)}
      </div>
      
      <!-- 下拉菜单 -->
      ${this.showMenu && this.menuPosition ? html`
        <div 
          class="dropdown-menu"
          style="left: ${this.menuPosition.x}px; top: ${this.menuPosition.y}px;"
          @click=${(e: MouseEvent) => e.stopPropagation()}
        >
          <div class="menu-item" @click=${() => this._renameConversation()}>
            <span class="menu-icon">✏️</span>
            <span>重命名</span>
          </div>
          <div class="menu-item danger" @click=${() => this._deleteConversation()}>
            <span class="menu-icon">🗑️</span>
            <span>删除</span>
          </div>
        </div>
      ` : ''}
    `;
  }

  private _newConversation() {
    this.dispatchEvent(new CustomEvent('af-new-conversation', { bubbles: true, composed: true }));
  }

  private _selectConversation(id: string) {
    // 关闭菜单
    this.showMenu = false;
    this.menuPosition = null;
    
    this.dispatchEvent(new CustomEvent('af-select-conversation', {
      detail: { id },
      bubbles: true,
      composed: true,
    }));
  }

  private _handleMouseEnter(id: string) {
    this.hoveredId = id;
  }

  private _handleMouseLeave() {
    this.hoveredId = '';
  }

  private _showMenu(e: MouseEvent, conversationId: string) {
    e.stopPropagation();
    this.menuConversationId = conversationId;
    this.showMenu = true;
    
    // 计算菜单位置（相对于视口）
    const rect = (e.target as HTMLElement).getBoundingClientRect();
    this.menuPosition = {
      x: rect.right + 5,  // 按钮右侧 5px
      y: rect.top,         // 与按钮顶部对齐
    };
  }

  private _closeMenu() {
    this.showMenu = false;
    this.menuPosition = null;
    this.menuConversationId = '';
  }

  private _renameConversation() {
    const conversationId = this.menuConversationId;
    this._closeMenu();
    
    // 获取当前会话标题
    const conv = this.conversations.find(c => c.id === conversationId);
    if (!conv) return;
    
    // 弹出输入框
    const newTitle = prompt('请输入新的会话名称:', conv.title || '新会话');
    if (newTitle && newTitle.trim()) {
      this.dispatchEvent(new CustomEvent('af-rename-conversation', {
        detail: { id: conversationId, title: newTitle.trim() },
        bubbles: true,
        composed: true,
      }));
    }
  }

  private _deleteConversation() {
    const conversationId = this.menuConversationId;
    this._closeMenu();
    
    // 确认删除
    if (confirm('确定要删除这个会话吗？此操作不可恢复。')) {
      this.dispatchEvent(new CustomEvent('af-delete-conversation', {
        detail: { id: conversationId },
        bubbles: true,
        composed: true,
      }));
    }
  }

  connectedCallback() {
    super.connectedCallback();
    // 点击页面其他地方时关闭菜单
    document.addEventListener('click', this._handleDocumentClick.bind(this));
  }

  disconnectedCallback() {
    super.disconnectedCallback();
    document.removeEventListener('click', this._handleDocumentClick.bind(this));
  }

  private _handleDocumentClick(e: MouseEvent) {
    // 如果点击的不是菜单按钮或菜单本身，则关闭菜单
    const target = e.target as HTMLElement;
    if (!target.closest('.menu-btn') && !target.closest('.dropdown-menu')) {
      this._closeMenu();
    }
  }
}

// ===== 状态栏 =====
@customElement('af-status-bar')
export class AFStatusBar extends LitElement {
  @property({ type: String }) status = 'connected';
  @property({ type: Number }) tokenUsage = 0;
  @property({ type: Number }) activeAgents = 0;

  static styles = css`
    :host { display: block; }
    .bar { display: flex; align-items: center; gap: 1.5rem; font-size: 0.75rem; }
    .indicator { display: flex; align-items: center; gap: 0.375rem; }
    .dot { width: 0.5rem; height: 0.5rem; border-radius: 50%; }
    .dot.connected { background: #10b981; }
    .dot.disconnected { background: #ef4444; }
    .label { color: var(--color-text-secondary, #6b7280); }
    .value { font-weight: 500; }
  `;

  render() {
    return html`
      <div class="bar">
        <div class="indicator">
          <span class="dot ${this.status}"></span>
          <span class="label">服务状态:</span>
          <span class="value">${this.status === 'connected' ? '正常' : '断开'}</span>
        </div>
        <div class="indicator">
          <span class="label">Token使用:</span>
          <span class="value">${this.tokenUsage.toLocaleString()}</span>
        </div>
        <div class="indicator">
          <span class="label">活跃Agent:</span>
          <span class="value">${this.activeAgents}</span>
        </div>
      </div>
    `;
  }
}

declare global {
  interface HTMLElementTagNameMap {
    'af-layout': AFLayout;
    'af-conversation-list': AFConversationList;
    'af-status-bar': AFStatusBar;
  }
}
