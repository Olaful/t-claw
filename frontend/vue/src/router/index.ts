/**
 * Vue Router配置
 */
import { createRouter, createWebHashHistory, RouteRecordRaw } from 'vue-router';

const routes: RouteRecordRaw[] = [
  {
    path: '/',
    name: 'Dashboard',
    component: () => import('../pages/Dashboard'),
  },
  {
    path: '/chat/:agentId?',
    name: 'Chat',
    component: () => import('../pages/chat'),
  },
  {
    path: '/agents',
    name: 'Agents',
    component: () => import('../pages/Agents'),
  },
  {
    path: '/teams',
    name: 'Teams',
    component: () => import('../pages/Teams'),
  },
  {
    path: '/memory',
    name: 'Memory',
    component: () => import('../pages/Memory'),
  },
  {
    path: '/settings',
    name: 'Settings',
    component: () => import('../pages/Settings'),
  },
];

export const router = createRouter({
  history: createWebHashHistory(),
  routes,
});
