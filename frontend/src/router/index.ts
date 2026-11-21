import { createRouter, createWebHistory } from 'vue-router'

import Overview from '@/views/Overview.vue'
import StageFreshnessView from '@/views/StageFreshnessView.vue'
import WorkbenchView from '@/views/WorkbenchView.vue'

const router = createRouter({
  history: createWebHistory(),
  routes: [
    { path: '/', name: 'overview', component: Overview },
    {
      path: '/stage_freshness',
      name: 'stage_freshness',
      component: StageFreshnessView,
    },
    { path: '/:moduleKey', name: 'module', component: WorkbenchView, props: true },
  ],
})

export default router
