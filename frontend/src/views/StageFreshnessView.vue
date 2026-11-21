<template>
  <section class="page">
    <header class="page-head">
      <div>
        <h2>分段时效核验</h2>
        <p class="page-desc">
          来源同步、湖仓扫描、缓存刷新分别设定时限和缺证据条件；缓存刷新成功不能抵掉缺失的源扫描。
        </p>
      </div>
    </header>

    <form class="filter-bar" @submit.prevent="runVerify">
      <label class="filter-item">
        <span>批次编号</span>
        <input v-model="form.batchId" data-testid="batch-id" placeholder="BATCH-20261121-01" />
      </label>
      <label class="filter-item">
        <span>目标数据集</span>
        <input v-model="form.dataset" data-testid="dataset" placeholder="analytics" />
      </label>
      <label class="filter-item">
        <span>检查起点（留空取最早证据）</span>
        <input v-model="form.checkStart" data-testid="check-start" placeholder="2026-11-21 00:00:00" />
      </label>
      <label class="filter-item">
        <span>规则版本（留空按起点解析）</span>
        <input v-model="form.ruleVersion" data-testid="rule-version" placeholder="v1 / v2" />
      </label>
      <button class="btn primary" data-testid="verify" type="submit">核验批次</button>
      <button class="btn" data-testid="simulate" type="button" @click="runSimulate">模拟到达顺序</button>
    </form>

    <p v-if="errorMessage" class="error-text">{{ errorMessage }}</p>

    <article v-if="result" class="stat-row">
      <div class="stat-card">
        <span class="stat-label">时效结论</span>
        <strong class="stat-value">{{ result.conclusion }}</strong>
      </div>
      <div class="stat-card">
        <span class="stat-label">采用版本 / 检查起点</span>
        <strong class="stat-value">{{ result.version }} · {{ result.check_start }}</strong>
      </div>
      <div class="stat-card">
        <span class="stat-label">本次新增阶段告警</span>
        <strong class="stat-value">{{ result.new_alerts.length }}</strong>
      </div>
      <div class="stat-card">
        <span class="stat-label">累计计数 / 重复提交</span>
        <strong class="stat-value">{{ result.counted_alerts }} / {{ result.duplicate ? '是' : '否' }}</strong>
      </div>
    </article>
    <p v-if="result?.requested_version_ignored" class="error-text">
      该批次已按 {{ result.version }} 登记，本次请求的版本未生效，结论不与旧口径混用。
    </p>

    <table v-if="result" class="data-table" data-testid="stage-table">
      <thead>
        <tr><th>交付阶段</th><th>判定</th><th>完成时间</th><th>耗时</th><th>时限</th><th>原因</th></tr>
      </thead>
      <tbody>
        <tr v-for="row in result.breakdown" :key="row.环节" :data-testid="`stage-${row.环节}`">
          <td>{{ row.环节 }}</td>
          <td>{{ row.判定 }}</td>
          <td>{{ row.时间 || '—' }}</td>
          <td>{{ row.耗时 === null ? '—' : `${row.耗时}s` }}</td>
          <td>{{ row.时限秒数 }}s</td>
          <td>{{ row.原因 || '—' }}</td>
        </tr>
      </tbody>
    </table>

    <section v-if="simulation" class="sim-block">
      <h3>先后到达模拟</h3>
      <table class="data-table" data-testid="simulate-table">
        <thead>
          <tr><th>到达顺序</th><th>来源同步</th><th>缓存刷新</th><th>结论</th><th>超时原因</th></tr>
        </thead>
        <tbody>
          <tr v-for="item in simulation.arrival_orders" :key="item.顺序">
            <td>{{ item.顺序 }}</td>
            <td>{{ item.来源判定 }}</td>
            <td>{{ item.缓存判定 }}</td>
            <td>{{ item.结论 }}</td>
            <td>{{ formatReasons(item.超时原因) }}</td>
          </tr>
        </tbody>
      </table>

      <h3>规则生效临界点</h3>
      <table class="data-table" data-testid="boundary-table">
        <thead>
          <tr><th>核对点</th><th>检查起点</th><th>采用版本</th><th>阶段时限</th><th>结论</th><th>违约环节</th></tr>
        </thead>
        <tbody>
          <tr v-for="point in simulation.rule_boundary.points" :key="point.核对点">
            <td>{{ point.核对点 }}</td>
            <td>{{ point.检查起点 }}</td>
            <td>{{ point.采用版本 }}</td>
            <td>{{ formatThresholds(point.阶段时限) }}</td>
            <td>{{ point.结论 }}</td>
            <td>{{ point.违约环节.join('、') || '—' }}</td>
          </tr>
        </tbody>
      </table>
      <p v-if="simulation.sla_change?.适用" class="page-desc">{{ simulation.sla_change.登记建议 }}</p>
    </section>
  </section>
</template>

<script setup lang="ts">
import { reactive, ref } from 'vue'

import { simulateDelivery, verifyBatch } from '@/api/freshness'
import type { SimulateResult, VerifyResult } from '@/api/freshness'

const form = reactive({
  batchId: '',
  dataset: '',
  checkStart: '',
  ruleVersion: '',
})
const result = ref<VerifyResult | null>(null)
const simulation = ref<SimulateResult | null>(null)
const errorMessage = ref('')

function formatReasons(reasons: Record<string, string>): string {
  return Object.entries(reasons).map(([stage, reason]) => `${stage}：${reason}`).join('；') || '—'
}

function formatThresholds(thresholds: Record<string, string>): string {
  return Object.entries(thresholds).map(([stage, limit]) => `${stage} ${limit}`).join(' / ')
}

async function runVerify() {
  errorMessage.value = ''
  try {
    result.value = await verifyBatch({
      batch_id: form.batchId,
      dataset: form.dataset,
      check_start: form.checkStart,
      rule_version: form.ruleVersion,
    })
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '核验失败'
  }
}

async function runSimulate() {
  errorMessage.value = ''
  try {
    simulation.value = await simulateDelivery({
      batch_id: form.batchId,
      dataset: form.dataset,
      check_start: form.checkStart,
      rule_version: form.ruleVersion,
    })
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '模拟失败'
  }
}
</script>

<style scoped>
.sim-block { margin-top: 20px; }
.sim-block h3 { font-size: 14px; margin: 16px 0 8px; }
</style>
