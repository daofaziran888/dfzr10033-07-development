<template>
  <section class="page">
    <header class="page-head">
      <div>
        <h2>运营概览</h2>
        <p class="page-desc">汇总各业务模块的关键指标，先看总量再看异常。</p>
      </div>
    </header>
    <div class="stat-row">
      <article v-for="card in cards" :key="card.label" class="stat-card">
        <span class="stat-label">{{ card.label }}</span>
        <strong class="stat-value">{{ card.value }}</strong>
      </article>
    </div>
    <table class="data-table">
      <thead>
        <tr><th>业务模块</th><th>今日新增</th><th>待处理</th><th>异常量</th></tr>
      </thead>
      <tbody>
        <tr v-for="row in moduleRows" :key="row.name">
          <td>{{ row.name }}</td>
          <td>{{ row.created }}</td>
          <td>{{ row.pending }}</td>
          <td>{{ row.abnormal }}</td>
        </tr>
      </tbody>
    </table>
  </section>
</template>

<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'

import { fetchJson } from '@/api/client'

type Overview = {
  cards: { label: string; value: number }[]
  modules: { name: string; created: number; pending: number; abnormal: number }[]
}

const cards = ref<Overview['cards']>([])
const moduleRows = ref<Overview['modules']>([])

onMounted(async () => {
  try {
    const payload = await fetchJson<Overview>('/api/overview')
    cards.value = payload.cards
    moduleRows.value = payload.modules
  } catch {
    cards.value = [{"label": "业务模块", "value": 0}, {"label": "今日新增", "value": 0}]
    moduleRows.value = [{"name": "数据源注册", "created": 0, "pending": 0, "abnormal": 0}, {"name": "语义模型", "created": 0, "pending": 0, "abnormal": 0}, {"name": "联邦查询", "created": 0, "pending": 0, "abnormal": 0}, {"name": "多维立方", "created": 0, "pending": 0, "abnormal": 0}, {"name": "指标工厂", "created": 0, "pending": 0, "abnormal": 0}, {"name": "维度树", "created": 0, "pending": 0, "abnormal": 0}, {"name": "聚合缓存", "created": 0, "pending": 0, "abnormal": 0}, {"name": "查询改写", "created": 0, "pending": 0, "abnormal": 0}, {"name": "时序窗口", "created": 0, "pending": 0, "abnormal": 0}, {"name": "数据切片", "created": 0, "pending": 0, "abnormal": 0}, {"name": "下钻配置", "created": 0, "pending": 0, "abnormal": 0}, {"name": "结果集市", "created": 0, "pending": 0, "abnormal": 0}, {"name": "基准集", "created": 0, "pending": 0, "abnormal": 0}, {"name": "查询审计", "created": 0, "pending": 0, "abnormal": 0}, {"name": "成本治理", "created": 0, "pending": 0, "abnormal": 0}, {"name": "新鲜度监控", "created": 0, "pending": 0, "abnormal": 0}, {"name": "分段时效", "created": 0, "pending": 0, "abnormal": 0}, {"name": "违约清单", "created": 0, "pending": 0, "abnormal": 0}, {"name": "分析导出", "created": 0, "pending": 0, "abnormal": 0}, {"name": "定时计算", "created": 0, "pending": 0, "abnormal": 0}, {"name": "湖仓读取", "created": 0, "pending": 0, "abnormal": 0}, {"name": "结果缓存", "created": 0, "pending": 0, "abnormal": 0}, {"name": "数据契约", "created": 0, "pending": 0, "abnormal": 0}, {"name": "实验配置", "created": 0, "pending": 0, "abnormal": 0}]
  }
})
</script>
