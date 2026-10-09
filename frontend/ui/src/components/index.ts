/**
 * AI Agent Framework - Lit原子组件库
 * 基础UI组件：Button, Input, Modal, Card, Table, Badge
 */
import { LitElement, html, css } from 'lit';
import { customElement, property } from 'lit/decorators.js';

// ===== Button组件 =====
@customElement('af-button')
export class AFButton extends LitElement {
  @property({ type: String }) variant: 'primary' | 'secondary' | 'danger' | 'ghost' = 'primary';
  @property({ type: String }) size: 'sm' | 'md' | 'lg' = 'md';
  @property({ type: Boolean }) disabled = false;
  @property({ type: Boolean }) loading = false;

  static styles = css`
    :host { display: inline-block; }
    button {
      display: inline-flex;
      align-items: center;
      justify-content: center;
      gap: 0.5rem;
      border: none;
      border-radius: 0.5rem;
      font-weight: 500;
      cursor: pointer;
      transition: all 0.2s ease;
    }
    button:disabled { opacity: 0.5; cursor: not-allowed; }
    .primary { background: var(--color-primary, #3b82f6); color: white; }
    .primary:hover:not(:disabled) { background: var(--color-primary-dark, #2563eb); }
    .secondary { background: var(--color-secondary, #6b7280); color: white; }
    .secondary:hover:not(:disabled) { background: var(--color-secondary-dark, #4b5563); }
    .danger { background: var(--color-danger, #ef4444); color: white; }
    .danger:hover:not(:disabled) { background: var(--color-danger-dark, #dc2626); }
    .ghost { background: transparent; color: var(--color-text, #374151); border: 1px solid var(--color-border, #d1d5db); }
    .ghost:hover:not(:disabled) { background: var(--color-bg-hover, #f3f4f6); }
    .sm { padding: 0.375rem 0.75rem; font-size: 0.875rem; }
    .md { padding: 0.5rem 1rem; font-size: 1rem; }
    .lg { padding: 0.75rem 1.5rem; font-size: 1.125rem; }
    .spinner {
      width: 1em; height: 1em;
      border: 2px solid currentColor;
      border-right-color: transparent;
      border-radius: 50%;
      animation: spin 0.6s linear infinite;
    }
    @keyframes spin { to { transform: rotate(360deg); } }
  `;

  render() {
    return html`
      <button
        class="${this.variant} ${this.size}"
        ?disabled=${this.disabled || this.loading}
        @click=${this._handleClick}
      >
        ${this.loading ? html`<span class="spinner"></span>` : ''}
        <slot></slot>
      </button>
    `;
  }

  private _handleClick(e: Event) {
    if (this.disabled || this.loading) {
      e.stopPropagation();
      return;
    }
    this.dispatchEvent(new CustomEvent('af-click', { bubbles: true, composed: true }));
  }
}

// ===== Input组件 =====
@customElement('af-input')
export class AFInput extends LitElement {
  @property({ type: String }) value = '';
  @property({ type: String }) placeholder = '';
  @property({ type: String }) label = '';
  @property({ type: String }) type = 'text';
  @property({ type: Boolean }) disabled = false;
  @property({ type: String }) error = '';

  static styles = css`
    :host { display: block; margin-bottom: 1rem; }
    label { display: block; margin-bottom: 0.25rem; font-size: 0.875rem; font-weight: 500; color: var(--color-text, #374151); }
    input {
      width: 100%;
      padding: 0.5rem 0.75rem;
      border: 1px solid var(--color-border, #d1d5db);
      border-radius: 0.5rem;
      font-size: 1rem;
      transition: border-color 0.2s;
      box-sizing: border-box;
    }
    input:focus { outline: none; border-color: var(--color-primary, #3b82f6); box-shadow: 0 0 0 3px rgba(59,130,246,0.1); }
    input:disabled { background: var(--color-bg-disabled, #f3f4f6); cursor: not-allowed; }
    input.error { border-color: var(--color-danger, #ef4444); }
    .error-text { color: var(--color-danger, #ef4444); font-size: 0.75rem; margin-top: 0.25rem; }
  `;

  render() {
    return html`
      ${this.label ? html`<label>${this.label}</label>` : ''}
      <input
        type=${this.type}
        .value=${this.value}
        placeholder=${this.placeholder}
        ?disabled=${this.disabled}
        class=${this.error ? 'error' : ''}
        @input=${this._handleInput}
      />
      ${this.error ? html`<div class="error-text">${this.error}</div>` : ''}
    `;
  }

  private _handleInput(e: Event) {
    const input = e.target as HTMLInputElement;
    this.value = input.value;
    this.dispatchEvent(new CustomEvent('af-input', {
      detail: { value: this.value },
      bubbles: true,
      composed: true,
    }));
  }
}

// ===== Card组件 =====
@customElement('af-card')
export class AFCard extends LitElement {
  @property({ type: String }) title = '';
  @property({ type: Boolean }) hoverable = false;

  static styles = css`
    :host { display: block; }
    .card {
      background: var(--color-bg-card, white);
      border: 1px solid var(--color-border, #e5e7eb);
      border-radius: 0.75rem;
      padding: 1.5rem;
      transition: box-shadow 0.2s;
    }
    .card.hoverable:hover { box-shadow: 0 4px 12px rgba(0,0,0,0.1); }
    .card-title { font-size: 1.125rem; font-weight: 600; margin-bottom: 0.75rem; color: var(--color-text, #111827); }
  `;

  render() {
    return html`
      <div class="card ${this.hoverable ? 'hoverable' : ''}">
        ${this.title ? html`<div class="card-title">${this.title}</div>` : ''}
        <slot></slot>
      </div>
    `;
  }
}

// ===== Modal组件 =====
@customElement('af-modal')
export class AFModal extends LitElement {
  @property({ type: Boolean }) open = false;
  @property({ type: String }) title = '';
  @property({ type: String }) size: 'sm' | 'md' | 'lg' = 'md';

  static styles = css`
    :host { display: contents; }
    .overlay {
      position: fixed; inset: 0;
      background: rgba(0,0,0,0.5);
      display: flex; align-items: center; justify-content: center;
      z-index: 1000;
      animation: fadeIn 0.2s ease;
    }
    .modal {
      background: var(--color-bg-card, white);
      border-radius: 0.75rem;
      padding: 1.5rem;
      max-height: 90vh;
      overflow-y: auto;
      animation: slideUp 0.2s ease;
    }
    .sm { width: 24rem; }
    .md { width: 32rem; }
    .lg { width: 48rem; }
    .header { display: flex; justify-content: space-between; align-items: center; margin-bottom: 1rem; }
    .title { font-size: 1.25rem; font-weight: 600; }
    .close { background: none; border: none; font-size: 1.5rem; cursor: pointer; color: var(--color-text-secondary, #6b7280); }
    @keyframes fadeIn { from { opacity: 0; } to { opacity: 1; } }
    @keyframes slideUp { from { transform: translateY(20px); opacity: 0; } to { transform: translateY(0); opacity: 1; } }
  `;

  render() {
    if (!this.open) return html``;
    return html`
      <div class="overlay" @click=${this._handleOverlayClick}>
        <div class="modal ${this.size}" @click=${(e: Event) => e.stopPropagation()}>
          <div class="header">
            <span class="title">${this.title}</span>
            <button class="close" @click=${this._close}>&times;</button>
          </div>
          <slot></slot>
        </div>
      </div>
    `;
  }

  private _handleOverlayClick() { this._close(); }
  private _close() {
    this.open = false;
    this.dispatchEvent(new CustomEvent('af-close', { bubbles: true, composed: true }));
  }
}

// ===== Badge组件 =====
@customElement('af-badge')
export class AFBadge extends LitElement {
  @property({ type: String }) variant: 'default' | 'success' | 'warning' | 'danger' | 'info' = 'default';

  static styles = css`
    :host { display: inline-block; }
    span {
      display: inline-flex;
      align-items: center;
      padding: 0.125rem 0.625rem;
      border-radius: 9999px;
      font-size: 0.75rem;
      font-weight: 500;
    }
    .default { background: var(--badge-default-bg, #f3f4f6); color: var(--badge-default-text, #374151); }
    .success { background: var(--badge-success-bg, #d1fae5); color: var(--badge-success-text, #065f46); }
    .warning { background: var(--badge-warning-bg, #fef3c7); color: var(--badge-warning-text, #92400e); }
    .danger { background: var(--badge-danger-bg, #fee2e2); color: var(--badge-danger-text, #991b1b); }
    .info { background: var(--badge-info-bg, #dbeafe); color: var(--badge-info-text, #1e40af); }
  `;

  render() {
    return html`<span class="${this.variant}"><slot></slot></span>`;
  }
}

// ===== Table组件 =====
@customElement('af-table')
export class AFTable extends LitElement {
  @property({ type: Array }) columns: Array<{ key: string; label: string; width?: string }> = [];
  @property({ type: Array }) data: Array<Record<string, any>> = [];

  static styles = css`
    :host { display: block; overflow-x: auto; }
    table { width: 100%; border-collapse: collapse; }
    th {
      text-align: left;
      padding: 0.75rem 1rem;
      background: var(--color-bg-header, #f9fafb);
      font-weight: 600;
      font-size: 0.875rem;
      color: var(--color-text-secondary, #6b7280);
      border-bottom: 2px solid var(--color-border, #e5e7eb);
    }
    td {
      padding: 0.75rem 1rem;
      border-bottom: 1px solid var(--color-border, #e5e7eb);
      font-size: 0.875rem;
    }
    tr:hover td { background: var(--color-bg-hover, #f9fafb); }
  `;

  render() {
    return html`
      <table>
        <thead>
          <tr>
            ${this.columns.map(col => html`
              <th style=${col.width ? `width: ${col.width}` : ''}>${col.label}</th>
            `)}
          </tr>
        </thead>
        <tbody>
          ${this.data.map(row => html`
            <tr>
              ${this.columns.map(col => html`<td>${row[col.key] ?? ''}</td>`)}
            </tr>
          `)}
        </tbody>
      </table>
    `;
  }
}

// ===== 类型声明 =====
declare global {
  interface HTMLElementTagNameMap {
    'af-button': AFButton;
    'af-input': AFInput;
    'af-card': AFCard;
    'af-modal': AFModal;
    'af-badge': AFBadge;
    'af-table': AFTable;
  }
}
