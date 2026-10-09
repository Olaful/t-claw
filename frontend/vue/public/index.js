import { css, LitElement, html } from "lit";
/**
 * @license
 * Copyright 2017 Google LLC
 * SPDX-License-Identifier: BSD-3-Clause
 */
const t$1 = (t2) => (e2, o2) => {
  void 0 !== o2 ? o2.addInitializer(() => {
    customElements.define(t2, e2);
  }) : customElements.define(t2, e2);
};
/**
 * @license
 * Copyright 2019 Google LLC
 * SPDX-License-Identifier: BSD-3-Clause
 */
const t = globalThis, e$1 = t.ShadowRoot && (void 0 === t.ShadyCSS || t.ShadyCSS.nativeShadow) && "adoptedStyleSheets" in Document.prototype && "replace" in CSSStyleSheet.prototype, s = Symbol(), o$2 = /* @__PURE__ */ new WeakMap();
let n$2 = class n {
  constructor(t2, e2, o2) {
    if (this._$cssResult$ = true, o2 !== s) throw Error("CSSResult is not constructable. Use `unsafeCSS` or `css` instead.");
    this.cssText = t2, this.t = e2;
  }
  get styleSheet() {
    let t2 = this.o;
    const s2 = this.t;
    if (e$1 && void 0 === t2) {
      const e2 = void 0 !== s2 && 1 === s2.length;
      e2 && (t2 = o$2.get(s2)), void 0 === t2 && ((this.o = t2 = new CSSStyleSheet()).replaceSync(this.cssText), e2 && o$2.set(s2, t2));
    }
    return t2;
  }
  toString() {
    return this.cssText;
  }
};
const r$2 = (t2) => new n$2("string" == typeof t2 ? t2 : t2 + "", void 0, s), S = (s2, o2) => {
  if (e$1) s2.adoptedStyleSheets = o2.map((t2) => t2 instanceof CSSStyleSheet ? t2 : t2.styleSheet);
  else for (const e2 of o2) {
    const o3 = document.createElement("style"), n3 = t.litNonce;
    void 0 !== n3 && o3.setAttribute("nonce", n3), o3.textContent = e2.cssText, s2.appendChild(o3);
  }
}, c$1 = e$1 ? (t2) => t2 : (t2) => t2 instanceof CSSStyleSheet ? ((t3) => {
  let e2 = "";
  for (const s2 of t3.cssRules) e2 += s2.cssText;
  return r$2(e2);
})(t2) : t2;
/**
 * @license
 * Copyright 2017 Google LLC
 * SPDX-License-Identifier: BSD-3-Clause
 */
const { is: i, defineProperty: e, getOwnPropertyDescriptor: h, getOwnPropertyNames: r$1, getOwnPropertySymbols: o$1, getPrototypeOf: n$1 } = Object, a = globalThis, c = a.trustedTypes, l = c ? c.emptyScript : "", p = a.reactiveElementPolyfillSupport, d = (t2, s2) => t2, u = { toAttribute(t2, s2) {
  switch (s2) {
    case Boolean:
      t2 = t2 ? l : null;
      break;
    case Object:
    case Array:
      t2 = null == t2 ? t2 : JSON.stringify(t2);
  }
  return t2;
}, fromAttribute(t2, s2) {
  let i2 = t2;
  switch (s2) {
    case Boolean:
      i2 = null !== t2;
      break;
    case Number:
      i2 = null === t2 ? null : Number(t2);
      break;
    case Object:
    case Array:
      try {
        i2 = JSON.parse(t2);
      } catch (t3) {
        i2 = null;
      }
  }
  return i2;
} }, f = (t2, s2) => !i(t2, s2), b = { attribute: true, type: String, converter: u, reflect: false, useDefault: false, hasChanged: f };
Symbol.metadata ?? (Symbol.metadata = Symbol("metadata")), a.litPropertyMetadata ?? (a.litPropertyMetadata = /* @__PURE__ */ new WeakMap());
class y extends HTMLElement {
  static addInitializer(t2) {
    this._$Ei(), (this.l ?? (this.l = [])).push(t2);
  }
  static get observedAttributes() {
    return this.finalize(), this._$Eh && [...this._$Eh.keys()];
  }
  static createProperty(t2, s2 = b) {
    if (s2.state && (s2.attribute = false), this._$Ei(), this.prototype.hasOwnProperty(t2) && ((s2 = Object.create(s2)).wrapped = true), this.elementProperties.set(t2, s2), !s2.noAccessor) {
      const i2 = Symbol(), h2 = this.getPropertyDescriptor(t2, i2, s2);
      void 0 !== h2 && e(this.prototype, t2, h2);
    }
  }
  static getPropertyDescriptor(t2, s2, i2) {
    const { get: e2, set: r2 } = h(this.prototype, t2) ?? { get() {
      return this[s2];
    }, set(t3) {
      this[s2] = t3;
    } };
    return { get: e2, set(s3) {
      const h2 = e2 == null ? void 0 : e2.call(this);
      r2 == null ? void 0 : r2.call(this, s3), this.requestUpdate(t2, h2, i2);
    }, configurable: true, enumerable: true };
  }
  static getPropertyOptions(t2) {
    return this.elementProperties.get(t2) ?? b;
  }
  static _$Ei() {
    if (this.hasOwnProperty(d("elementProperties"))) return;
    const t2 = n$1(this);
    t2.finalize(), void 0 !== t2.l && (this.l = [...t2.l]), this.elementProperties = new Map(t2.elementProperties);
  }
  static finalize() {
    if (this.hasOwnProperty(d("finalized"))) return;
    if (this.finalized = true, this._$Ei(), this.hasOwnProperty(d("properties"))) {
      const t3 = this.properties, s2 = [...r$1(t3), ...o$1(t3)];
      for (const i2 of s2) this.createProperty(i2, t3[i2]);
    }
    const t2 = this[Symbol.metadata];
    if (null !== t2) {
      const s2 = litPropertyMetadata.get(t2);
      if (void 0 !== s2) for (const [t3, i2] of s2) this.elementProperties.set(t3, i2);
    }
    this._$Eh = /* @__PURE__ */ new Map();
    for (const [t3, s2] of this.elementProperties) {
      const i2 = this._$Eu(t3, s2);
      void 0 !== i2 && this._$Eh.set(i2, t3);
    }
    this.elementStyles = this.finalizeStyles(this.styles);
  }
  static finalizeStyles(s2) {
    const i2 = [];
    if (Array.isArray(s2)) {
      const e2 = new Set(s2.flat(1 / 0).reverse());
      for (const s3 of e2) i2.unshift(c$1(s3));
    } else void 0 !== s2 && i2.push(c$1(s2));
    return i2;
  }
  static _$Eu(t2, s2) {
    const i2 = s2.attribute;
    return false === i2 ? void 0 : "string" == typeof i2 ? i2 : "string" == typeof t2 ? t2.toLowerCase() : void 0;
  }
  constructor() {
    super(), this._$Ep = void 0, this.isUpdatePending = false, this.hasUpdated = false, this._$Em = null, this._$Ev();
  }
  _$Ev() {
    var _a;
    this._$ES = new Promise((t2) => this.enableUpdating = t2), this._$AL = /* @__PURE__ */ new Map(), this._$E_(), this.requestUpdate(), (_a = this.constructor.l) == null ? void 0 : _a.forEach((t2) => t2(this));
  }
  addController(t2) {
    var _a;
    (this._$EO ?? (this._$EO = /* @__PURE__ */ new Set())).add(t2), void 0 !== this.renderRoot && this.isConnected && ((_a = t2.hostConnected) == null ? void 0 : _a.call(t2));
  }
  removeController(t2) {
    var _a;
    (_a = this._$EO) == null ? void 0 : _a.delete(t2);
  }
  _$E_() {
    const t2 = /* @__PURE__ */ new Map(), s2 = this.constructor.elementProperties;
    for (const i2 of s2.keys()) this.hasOwnProperty(i2) && (t2.set(i2, this[i2]), delete this[i2]);
    t2.size > 0 && (this._$Ep = t2);
  }
  createRenderRoot() {
    const t2 = this.shadowRoot ?? this.attachShadow(this.constructor.shadowRootOptions);
    return S(t2, this.constructor.elementStyles), t2;
  }
  connectedCallback() {
    var _a;
    this.renderRoot ?? (this.renderRoot = this.createRenderRoot()), this.enableUpdating(true), (_a = this._$EO) == null ? void 0 : _a.forEach((t2) => {
      var _a2;
      return (_a2 = t2.hostConnected) == null ? void 0 : _a2.call(t2);
    });
  }
  enableUpdating(t2) {
  }
  disconnectedCallback() {
    var _a;
    (_a = this._$EO) == null ? void 0 : _a.forEach((t2) => {
      var _a2;
      return (_a2 = t2.hostDisconnected) == null ? void 0 : _a2.call(t2);
    });
  }
  attributeChangedCallback(t2, s2, i2) {
    this._$AK(t2, i2);
  }
  _$ET(t2, s2) {
    var _a;
    const i2 = this.constructor.elementProperties.get(t2), e2 = this.constructor._$Eu(t2, i2);
    if (void 0 !== e2 && true === i2.reflect) {
      const h2 = (void 0 !== ((_a = i2.converter) == null ? void 0 : _a.toAttribute) ? i2.converter : u).toAttribute(s2, i2.type);
      this._$Em = t2, null == h2 ? this.removeAttribute(e2) : this.setAttribute(e2, h2), this._$Em = null;
    }
  }
  _$AK(t2, s2) {
    var _a, _b;
    const i2 = this.constructor, e2 = i2._$Eh.get(t2);
    if (void 0 !== e2 && this._$Em !== e2) {
      const t3 = i2.getPropertyOptions(e2), h2 = "function" == typeof t3.converter ? { fromAttribute: t3.converter } : void 0 !== ((_a = t3.converter) == null ? void 0 : _a.fromAttribute) ? t3.converter : u;
      this._$Em = e2;
      const r2 = h2.fromAttribute(s2, t3.type);
      this[e2] = r2 ?? ((_b = this._$Ej) == null ? void 0 : _b.get(e2)) ?? r2, this._$Em = null;
    }
  }
  requestUpdate(t2, s2, i2, e2 = false, h2) {
    var _a;
    if (void 0 !== t2) {
      const r2 = this.constructor;
      if (false === e2 && (h2 = this[t2]), i2 ?? (i2 = r2.getPropertyOptions(t2)), !((i2.hasChanged ?? f)(h2, s2) || i2.useDefault && i2.reflect && h2 === ((_a = this._$Ej) == null ? void 0 : _a.get(t2)) && !this.hasAttribute(r2._$Eu(t2, i2)))) return;
      this.C(t2, s2, i2);
    }
    false === this.isUpdatePending && (this._$ES = this._$EP());
  }
  C(t2, s2, { useDefault: i2, reflect: e2, wrapped: h2 }, r2) {
    i2 && !(this._$Ej ?? (this._$Ej = /* @__PURE__ */ new Map())).has(t2) && (this._$Ej.set(t2, r2 ?? s2 ?? this[t2]), true !== h2 || void 0 !== r2) || (this._$AL.has(t2) || (this.hasUpdated || i2 || (s2 = void 0), this._$AL.set(t2, s2)), true === e2 && this._$Em !== t2 && (this._$Eq ?? (this._$Eq = /* @__PURE__ */ new Set())).add(t2));
  }
  async _$EP() {
    this.isUpdatePending = true;
    try {
      await this._$ES;
    } catch (t3) {
      Promise.reject(t3);
    }
    const t2 = this.scheduleUpdate();
    return null != t2 && await t2, !this.isUpdatePending;
  }
  scheduleUpdate() {
    return this.performUpdate();
  }
  performUpdate() {
    var _a;
    if (!this.isUpdatePending) return;
    if (!this.hasUpdated) {
      if (this.renderRoot ?? (this.renderRoot = this.createRenderRoot()), this._$Ep) {
        for (const [t4, s3] of this._$Ep) this[t4] = s3;
        this._$Ep = void 0;
      }
      const t3 = this.constructor.elementProperties;
      if (t3.size > 0) for (const [s3, i2] of t3) {
        const { wrapped: t4 } = i2, e2 = this[s3];
        true !== t4 || this._$AL.has(s3) || void 0 === e2 || this.C(s3, void 0, i2, e2);
      }
    }
    let t2 = false;
    const s2 = this._$AL;
    try {
      t2 = this.shouldUpdate(s2), t2 ? (this.willUpdate(s2), (_a = this._$EO) == null ? void 0 : _a.forEach((t3) => {
        var _a2;
        return (_a2 = t3.hostUpdate) == null ? void 0 : _a2.call(t3);
      }), this.update(s2)) : this._$EM();
    } catch (s3) {
      throw t2 = false, this._$EM(), s3;
    }
    t2 && this._$AE(s2);
  }
  willUpdate(t2) {
  }
  _$AE(t2) {
    var _a;
    (_a = this._$EO) == null ? void 0 : _a.forEach((t3) => {
      var _a2;
      return (_a2 = t3.hostUpdated) == null ? void 0 : _a2.call(t3);
    }), this.hasUpdated || (this.hasUpdated = true, this.firstUpdated(t2)), this.updated(t2);
  }
  _$EM() {
    this._$AL = /* @__PURE__ */ new Map(), this.isUpdatePending = false;
  }
  get updateComplete() {
    return this.getUpdateComplete();
  }
  getUpdateComplete() {
    return this._$ES;
  }
  shouldUpdate(t2) {
    return true;
  }
  update(t2) {
    this._$Eq && (this._$Eq = this._$Eq.forEach((t3) => this._$ET(t3, this[t3]))), this._$EM();
  }
  updated(t2) {
  }
  firstUpdated(t2) {
  }
}
y.elementStyles = [], y.shadowRootOptions = { mode: "open" }, y[d("elementProperties")] = /* @__PURE__ */ new Map(), y[d("finalized")] = /* @__PURE__ */ new Map(), p == null ? void 0 : p({ ReactiveElement: y }), (a.reactiveElementVersions ?? (a.reactiveElementVersions = [])).push("2.1.2");
/**
 * @license
 * Copyright 2017 Google LLC
 * SPDX-License-Identifier: BSD-3-Clause
 */
const o = { attribute: true, type: String, converter: u, reflect: false, hasChanged: f }, r = (t2 = o, e2, r2) => {
  const { kind: n3, metadata: i2 } = r2;
  let s2 = globalThis.litPropertyMetadata.get(i2);
  if (void 0 === s2 && globalThis.litPropertyMetadata.set(i2, s2 = /* @__PURE__ */ new Map()), "setter" === n3 && ((t2 = Object.create(t2)).wrapped = true), s2.set(r2.name, t2), "accessor" === n3) {
    const { name: o2 } = r2;
    return { set(r3) {
      const n4 = e2.get.call(this);
      e2.set.call(this, r3), this.requestUpdate(o2, n4, t2, true, r3);
    }, init(e3) {
      return void 0 !== e3 && this.C(o2, void 0, t2, e3), e3;
    } };
  }
  if ("setter" === n3) {
    const { name: o2 } = r2;
    return function(r3) {
      const n4 = this[o2];
      e2.call(this, r3), this.requestUpdate(o2, n4, t2, true, r3);
    };
  }
  throw Error("Unsupported decorator location: " + n3);
};
function n2(t2) {
  return (e2, o2) => "object" == typeof o2 ? r(t2, e2, o2) : ((t3, e3, o3) => {
    const r2 = e3.hasOwnProperty(o3);
    return e3.constructor.createProperty(o3, t3), r2 ? Object.getOwnPropertyDescriptor(e3, o3) : void 0;
  })(t2, e2, o2);
}
var __defProp$1 = Object.defineProperty;
var __getOwnPropDesc$1 = Object.getOwnPropertyDescriptor;
var __decorateClass$1 = (decorators, target, key, kind) => {
  var result = kind > 1 ? void 0 : kind ? __getOwnPropDesc$1(target, key) : target;
  for (var i2 = decorators.length - 1, decorator; i2 >= 0; i2--)
    if (decorator = decorators[i2])
      result = (kind ? decorator(target, key, result) : decorator(result)) || result;
  if (kind && result) __defProp$1(target, key, result);
  return result;
};
let AFButton = class extends LitElement {
  constructor() {
    super(...arguments);
    this.variant = "primary";
    this.size = "md";
    this.disabled = false;
    this.loading = false;
  }
  render() {
    return html`
      <button
        class="${this.variant} ${this.size}"
        ?disabled=${this.disabled || this.loading}
        @click=${this._handleClick}
      >
        ${this.loading ? html`<span class="spinner"></span>` : ""}
        <slot></slot>
      </button>
    `;
  }
  _handleClick(e2) {
    if (this.disabled || this.loading) {
      e2.stopPropagation();
      return;
    }
    this.dispatchEvent(new CustomEvent("af-click", { bubbles: true, composed: true }));
  }
};
AFButton.styles = css`
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
__decorateClass$1([
  n2({ type: String })
], AFButton.prototype, "variant", 2);
__decorateClass$1([
  n2({ type: String })
], AFButton.prototype, "size", 2);
__decorateClass$1([
  n2({ type: Boolean })
], AFButton.prototype, "disabled", 2);
__decorateClass$1([
  n2({ type: Boolean })
], AFButton.prototype, "loading", 2);
AFButton = __decorateClass$1([
  t$1("af-button")
], AFButton);
let AFInput = class extends LitElement {
  constructor() {
    super(...arguments);
    this.value = "";
    this.placeholder = "";
    this.label = "";
    this.type = "text";
    this.disabled = false;
    this.error = "";
  }
  render() {
    return html`
      ${this.label ? html`<label>${this.label}</label>` : ""}
      <input
        type=${this.type}
        .value=${this.value}
        placeholder=${this.placeholder}
        ?disabled=${this.disabled}
        class=${this.error ? "error" : ""}
        @input=${this._handleInput}
      />
      ${this.error ? html`<div class="error-text">${this.error}</div>` : ""}
    `;
  }
  _handleInput(e2) {
    const input = e2.target;
    this.value = input.value;
    this.dispatchEvent(new CustomEvent("af-input", {
      detail: { value: this.value },
      bubbles: true,
      composed: true
    }));
  }
};
AFInput.styles = css`
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
__decorateClass$1([
  n2({ type: String })
], AFInput.prototype, "value", 2);
__decorateClass$1([
  n2({ type: String })
], AFInput.prototype, "placeholder", 2);
__decorateClass$1([
  n2({ type: String })
], AFInput.prototype, "label", 2);
__decorateClass$1([
  n2({ type: String })
], AFInput.prototype, "type", 2);
__decorateClass$1([
  n2({ type: Boolean })
], AFInput.prototype, "disabled", 2);
__decorateClass$1([
  n2({ type: String })
], AFInput.prototype, "error", 2);
AFInput = __decorateClass$1([
  t$1("af-input")
], AFInput);
let AFCard = class extends LitElement {
  constructor() {
    super(...arguments);
    this.title = "";
    this.hoverable = false;
  }
  render() {
    return html`
      <div class="card ${this.hoverable ? "hoverable" : ""}">
        ${this.title ? html`<div class="card-title">${this.title}</div>` : ""}
        <slot></slot>
      </div>
    `;
  }
};
AFCard.styles = css`
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
__decorateClass$1([
  n2({ type: String })
], AFCard.prototype, "title", 2);
__decorateClass$1([
  n2({ type: Boolean })
], AFCard.prototype, "hoverable", 2);
AFCard = __decorateClass$1([
  t$1("af-card")
], AFCard);
let AFModal = class extends LitElement {
  constructor() {
    super(...arguments);
    this.open = false;
    this.title = "";
    this.size = "md";
  }
  render() {
    if (!this.open) return html``;
    return html`
      <div class="overlay" @click=${this._handleOverlayClick}>
        <div class="modal ${this.size}" @click=${(e2) => e2.stopPropagation()}>
          <div class="header">
            <span class="title">${this.title}</span>
            <button class="close" @click=${this._close}>&times;</button>
          </div>
          <slot></slot>
        </div>
      </div>
    `;
  }
  _handleOverlayClick() {
    this._close();
  }
  _close() {
    this.open = false;
    this.dispatchEvent(new CustomEvent("af-close", { bubbles: true, composed: true }));
  }
};
AFModal.styles = css`
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
__decorateClass$1([
  n2({ type: Boolean })
], AFModal.prototype, "open", 2);
__decorateClass$1([
  n2({ type: String })
], AFModal.prototype, "title", 2);
__decorateClass$1([
  n2({ type: String })
], AFModal.prototype, "size", 2);
AFModal = __decorateClass$1([
  t$1("af-modal")
], AFModal);
let AFBadge = class extends LitElement {
  constructor() {
    super(...arguments);
    this.variant = "default";
  }
  render() {
    return html`<span class="${this.variant}"><slot></slot></span>`;
  }
};
AFBadge.styles = css`
    :host { display: inline-block; }
    span {
      display: inline-flex;
      align-items: center;
      padding: 0.125rem 0.625rem;
      border-radius: 9999px;
      font-size: 0.75rem;
      font-weight: 500;
    }
    .default { background: #f3f4f6; color: #374151; }
    .success { background: #d1fae5; color: #065f46; }
    .warning { background: #fef3c7; color: #92400e; }
    .danger { background: #fee2e2; color: #991b1b; }
    .info { background: #dbeafe; color: #1e40af; }
  `;
__decorateClass$1([
  n2({ type: String })
], AFBadge.prototype, "variant", 2);
AFBadge = __decorateClass$1([
  t$1("af-badge")
], AFBadge);
let AFTable = class extends LitElement {
  constructor() {
    super(...arguments);
    this.columns = [];
    this.data = [];
  }
  render() {
    return html`
      <table>
        <thead>
          <tr>
            ${this.columns.map((col) => html`
              <th style=${col.width ? `width: ${col.width}` : ""}>${col.label}</th>
            `)}
          </tr>
        </thead>
        <tbody>
          ${this.data.map((row) => html`
            <tr>
              ${this.columns.map((col) => html`<td>${row[col.key] ?? ""}</td>`)}
            </tr>
          `)}
        </tbody>
      </table>
    `;
  }
};
AFTable.styles = css`
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
__decorateClass$1([
  n2({ type: Array })
], AFTable.prototype, "columns", 2);
__decorateClass$1([
  n2({ type: Array })
], AFTable.prototype, "data", 2);
AFTable = __decorateClass$1([
  t$1("af-table")
], AFTable);
var __defProp = Object.defineProperty;
var __getOwnPropDesc = Object.getOwnPropertyDescriptor;
var __decorateClass = (decorators, target, key, kind) => {
  var result = kind > 1 ? void 0 : kind ? __getOwnPropDesc(target, key) : target;
  for (var i2 = decorators.length - 1, decorator; i2 >= 0; i2--)
    if (decorator = decorators[i2])
      result = (kind ? decorator(target, key, result) : decorator(result)) || result;
  if (kind && result) __defProp(target, key, result);
  return result;
};
let AFLayout = class extends LitElement {
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
};
AFLayout.styles = css`
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
  `;
AFLayout = __decorateClass([
  t$1("af-layout")
], AFLayout);
let AFConversationList = class extends LitElement {
  constructor() {
    super(...arguments);
    this.conversations = [];
    this.activeId = "";
  }
  render() {
    return html`
      <div class="list-header">
        <span class="list-title">会话列表</span>
        <button class="new-btn" @click=${this._newConversation}>+ 新建</button>
      </div>
      ${this.conversations.map((conv) => html`
        <div class="item ${conv.id === this.activeId ? "active" : ""}" @click=${() => this._selectConversation(conv.id)}>
          <span class="item-title">${conv.title || "新会话"}</span>
          <span class="item-count">${conv.message_count}</span>
        </div>
      `)}
      <div class="section">
        <div class="section-title">快速工具</div>
        <div class="item" @click=${() => this.dispatchEvent(new CustomEvent("af-search"))}>
          <span class="item-title">🔍 搜索</span>
        </div>
        <div class="item" @click=${() => this.dispatchEvent(new CustomEvent("af-history"))}>
          <span class="item-title">📋 历史</span>
        </div>
        <div class="item" @click=${() => this.dispatchEvent(new CustomEvent("af-favorites"))}>
          <span class="item-title">⭐ 收藏</span>
        </div>
      </div>
    `;
  }
  _newConversation() {
    this.dispatchEvent(new CustomEvent("af-new-conversation", { bubbles: true, composed: true }));
  }
  _selectConversation(id) {
    this.dispatchEvent(new CustomEvent("af-select-conversation", {
      detail: { id },
      bubbles: true,
      composed: true
    }));
  }
};
AFConversationList.styles = css`
    :host { display: block; }
    .list-header { display: flex; justify-content: space-between; align-items: center; margin-bottom: 0.75rem; }
    .list-title { font-weight: 600; font-size: 0.875rem; color: var(--color-text, #374151); }
    .new-btn { background: var(--color-primary, #3b82f6); color: white; border: none; border-radius: 0.375rem; padding: 0.25rem 0.5rem; cursor: pointer; font-size: 0.75rem; }
    .item {
      display: flex; align-items: center; justify-content: space-between;
      padding: 0.5rem 0.75rem;
      border-radius: 0.375rem;
      cursor: pointer;
      transition: background 0.15s;
      margin-bottom: 0.25rem;
    }
    .item:hover { background: var(--color-bg-hover, #f3f4f6); }
    .item.active { background: var(--color-primary-light, #dbeafe); }
    .item-title { font-size: 0.875rem; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; max-width: 10rem; }
    .item-count { font-size: 0.75rem; color: var(--color-text-secondary, #9ca3af); }
    .section { margin-top: 1.5rem; }
    .section-title { font-size: 0.75rem; font-weight: 600; color: var(--color-text-secondary, #6b7280); margin-bottom: 0.5rem; text-transform: uppercase; }
  `;
__decorateClass([
  n2({ type: Array })
], AFConversationList.prototype, "conversations", 2);
__decorateClass([
  n2({ type: String })
], AFConversationList.prototype, "activeId", 2);
AFConversationList = __decorateClass([
  t$1("af-conversation-list")
], AFConversationList);
let AFStatusBar = class extends LitElement {
  constructor() {
    super(...arguments);
    this.status = "connected";
    this.tokenUsage = 0;
    this.activeAgents = 0;
  }
  render() {
    return html`
      <div class="bar">
        <div class="indicator">
          <span class="dot ${this.status}"></span>
          <span class="label">服务状态:</span>
          <span class="value">${this.status === "connected" ? "正常" : "断开"}</span>
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
};
AFStatusBar.styles = css`
    :host { display: block; }
    .bar { display: flex; align-items: center; gap: 1.5rem; font-size: 0.75rem; }
    .indicator { display: flex; align-items: center; gap: 0.375rem; }
    .dot { width: 0.5rem; height: 0.5rem; border-radius: 50%; }
    .dot.connected { background: #10b981; }
    .dot.disconnected { background: #ef4444; }
    .label { color: var(--color-text-secondary, #6b7280); }
    .value { font-weight: 500; }
  `;
__decorateClass([
  n2({ type: String })
], AFStatusBar.prototype, "status", 2);
__decorateClass([
  n2({ type: Number })
], AFStatusBar.prototype, "tokenUsage", 2);
__decorateClass([
  n2({ type: Number })
], AFStatusBar.prototype, "activeAgents", 2);
AFStatusBar = __decorateClass([
  t$1("af-status-bar")
], AFStatusBar);
