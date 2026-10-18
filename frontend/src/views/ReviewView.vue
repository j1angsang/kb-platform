<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { approveDoc, getDocs, rejectDoc, type DocItem, type User } from '../api'

defineProps<{ user: User }>()
const docs = ref<DocItem[]>([])
const loading = ref(false)
const error = ref('')

const indexText: Record<string, string> = { pending: '未入库', indexing: '索引中', ready: '已入库', failed: '入库失败' }
const indexClass: Record<string, string> = { pending: 'b-gray', indexing: 'b-amber', ready: 'b-green', failed: 'b-red' }

async function load() {
  loading.value = true
  error.value = ''
  try {
    const data = await getDocs({ status: 'pending', scope: 'reviewable', page: 1, page_size: 100 })
    docs.value = data.items || []
  } catch (exc) {
    error.value = exc instanceof Error ? exc.message : '加载失败'
  } finally {
    loading.value = false
  }
}

async function approve(doc: DocItem) {
  const comment = window.prompt('通过意见，可留空：', '')
  if (comment === null) return
  try {
    await approveDoc(doc.id, comment)
    await load()
  } catch (exc) {
    error.value = exc instanceof Error ? exc.message : '操作失败'
  }
}

async function reject(doc: DocItem) {
  const comment = window.prompt('驳回理由，必填：', '')
  if (comment === null) return
  if (!comment.trim()) {
    error.value = '驳回必须填写理由'
    return
  }
  try {
    await rejectDoc(doc.id, comment)
    await load()
  } catch (exc) {
    error.value = exc instanceof Error ? exc.message : '操作失败'
  }
}

function relTime(iso?: string): string {
  if (!iso) return ''
  const minutes = Math.floor((Date.now() - new Date(iso).getTime()) / 60000)
  if (minutes < 1) return '刚刚'
  if (minutes < 60) return `${minutes} 分钟前`
  const hours = Math.floor(minutes / 60)
  if (hours < 24) return `${hours} 小时前`
  return `${Math.floor(hours / 24)} 天前`
}

onMounted(load)
</script>

<template>
  <section class="view">
    <div class="card">
      <h3>审核队列</h3>
      <div class="hint">通过后进入切片入库；驳回必须填写理由。不能审核自己提交的文档。</div>
      <div class="rhead">
        <div>编号</div><div>文档</div><div>提交人</div><div>部门</div><div>入库状态</div><div>操作</div>
      </div>
      <div v-if="loading" class="empty">加载中…</div>
      <div v-else-if="!docs.length" class="empty">本部门没有待审文档</div>
      <template v-else>
        <div v-for="doc in docs" :key="doc.id" class="rrow">
          <div class="cell no">{{ doc.id }}</div>
          <div class="cell fname">
            <div>{{ doc.filename }}</div>
            <div class="sub">#{{ doc.file_id }} · {{ relTime(doc.created_at) }}</div>
          </div>
          <div class="cell hide-s">{{ doc.uploader || '—' }}</div>
          <div class="cell hide-s">{{ doc.department || '—' }}</div>
          <div class="cell"><span class="badge" :class="indexClass[doc.index_status] || 'b-gray'">{{ indexText[doc.index_status] || doc.index_status }}</span></div>
          <div class="cell ops">
            <button class="act good" @click="approve(doc)">通过</button>
            <button class="act bad" @click="reject(doc)">驳回</button>
          </div>
        </div>
      </template>
      <div v-if="error" class="err-text">{{ error }}</div>
    </div>
  </section>
</template>
