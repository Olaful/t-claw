<template>
  <div class="layout">
    <!-- Header -->
    <header class="header">
      <div class="header-left">
        <span class="logo"> AI Agent</span>
        <nav class="nav">
          <slot name="nav"></slot>
        </nav>
      </div>
      <div class="header-right">
        <slot name="header-right"></slot>
      </div>
    </header>
    
    <!-- Body -->
    <div class="body">
      <!-- Sidebar -->
      <aside class="sidebar">
        <slot name="sidebar"></slot>
      </aside>
      
      <!-- Main Content -->
      <main class="main">
        <slot></slot>
      </main>
    </div>
    
    <!-- Footer -->
    <footer class="footer">
      <slot name="footer"></slot>
    </footer>
  </div>
</template>

<style scoped>
.layout {
  display: flex;
  flex-direction: column;
  height: 100vh;
  overflow: hidden;
  font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
}

.header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 0 1.5rem;
  height: 3.5rem;
  flex-shrink: 0;
  background: var(--color-bg-header);
  color: var(--color-text);
  border-bottom: 1px solid var(--color-border);
}

.header-left {
  display: flex;
  align-items: center;
  gap: 2rem;
}

.logo {
  font-size: 1.25rem;
  font-weight: 700;
}

.nav {
  display: flex;
  gap: 0.5rem;
}

.nav-item {
  padding: 0.5rem 1rem;
  border-radius: 0.375rem;
  color: var(--color-text-secondary);
  cursor: pointer;
  transition: all 0.2s;
  text-decoration: none;
  font-size: 0.875rem;
}

.nav-item:hover,
.nav-item.active {
  background: var(--color-bg-hover);
  color: var(--color-text);
}

.header-right {
  display: flex;
  align-items: center;
  gap: 1rem;
}

.body {
  display: flex;
  flex: 1;
  overflow: hidden;
}

.sidebar {
  width: 16rem;
  flex-shrink: 0;
  background: var(--color-bg-sidebar);
  border-right: 1px solid var(--color-border);
  display: flex;
  flex-direction: column;
  overflow: hidden;
}

.main {
  flex: 1;
  overflow: hidden;
  background: var(--color-bg-main);
}

.footer {
  padding: 0.5rem 1.5rem;
  background: var(--color-bg-footer);
  border-top: 1px solid var(--color-border);
  font-size: 0.75rem;
  color: var(--color-text-secondary);
  display: flex;
  align-items: center;
  justify-content: space-between;
  flex-shrink: 0;
}
</style>
