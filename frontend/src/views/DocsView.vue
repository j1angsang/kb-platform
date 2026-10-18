<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { getDocs, resubmitDoc, submitDoc, uploadFile, type DocItem, type User } from '../api'

const props = defineProps<{ user: User }>()
const docs = ref<DocItem[]>([])
const total = ref(0)
const page = ref(1)
const pageSize = 10
const status = ref('')
const loading = ref(false)
const error = ref('')
const uploadLog = ref('')
const uploading = ref(false)
const fileInput = ref<HTMLInputElement | null>(null)

const pages = computed(() => Math.max(1, Math.ceil(total.value / pageSize)))
const statusText: Record<string, string> = { pending: '待审核', approved: '已通过', rejected: '已驳回' }
const statusClass: Record<string, string> = { pending: 'b-blue', approved: 'b-green', rejected: 'b-red' }
const indexText: Record<string, string> = { pending: '未入库', indexing: '索引中', ready: '已入库', failed: '入库失败' }
const indexClass: Record<string, string> = { pending: 'b-gray', indexing: 'b-amber', ready: 'b-green', failed: 'b-red' }

async function load() {
  loading.value = true
  error.value = ''
  try {
    const data = await getDocs({ page: page.value, page_size: pageSize, status: status.value })
    docs.value = data.items || []
    total.value = data.total
  } catch (exc) {
    error.value = exc instanceof Error ? exc.message : '加载失败'
  } finally {
    loading.value = false
  }
}

function go(next: number) {
  if (next < 1 || next > pages.value) return
  page.value = next
  load()
}

function pickFiles() {
  fileInput.value?.click()
}

function onFileInput(event: Event) {
  const input = event.target as HTMLInputElement
  handleFiles(input.files)
  input.value = ''
}

function onStatusChange() {
  page.value = 1
  load()
}

async function handleFiles(files: FileList | null) {
  if (!files?.length) return
  uploading.value = true
  uploadLog.value = ''
  for (const file of Array.from(files)) {
    uploadLog.value += `上传 ${file.name}…\n`
    try {
      const item = await uploadFile(file)
      await submitDoc(item.id)
      uploadLog.value += `已提交 ${file.name}\n`
    } catch (exc) {
      uploadLog.value += `失败 ${file.name}：${exc instanceof Error ? exc.message : '未知错误'}\n`
    }
  }
  uploading.value = false
  page.value = 1
  await load()
}

function onDrop(event: DragEvent) {
  handleFiles(event.dataTransfer?.files || null)
}

async function resubmit(doc: DocItem) {
  try {
    await resubmitDoc(doc.id)
    await load()
  } catch (exc) {
    error.value = exc instanceof Error ? exc.message : '提交失败'
  }
}

onMounted(load)
</script>

<template>
  <section class="view">
    <div class="grid2">
      <div class="card">
        <h3>上传并提交审核</h3>
        <div class="hint">上传成功后自动提交审核；支持 txt / md / pdf / docx / xlsx。</div>
        <div class="dropzone" :class="{ over: uploading }" @click="pickFiles" @dragover.prevent @drop.prevent="onDrop">
          <div class="t">{{ uploading ? '上传中…' : '拖拽文件到这里，或点击选择' }}</div>
          <div class="h">员工提交进入待审，领导/管理员提交免审直接入库</div>
          <input ref="fileInput" type="file" multiple hidden @change="onFileInput" />
        </div>
        <div v-if="uploadLog" class="upload-log">{{ uploadLog }}</div>
        <div v-if="error" class="err-text">{{ error }}</div>
      </div>

      <div class="card">
        <h3>文档列表</h3>
        <div class="filter-row">
          <span>状态</span>
          <select v-model="status" @change="onStatusChange">
            <option value="">全部</option>
            <option value="pending">待审核</option>
            <option value="approved">已通过</option>
            <option value="rejected">已驳回</option>
          </select>
          <span class="hint" style="margin: 0">员工仅见本部门文档</span>
        </div>
        <div class="khead">
          <div>编号</div><div>文档</div><div>提交人</div><div>部门</div>
          <div>审核状态</div><div>入库状态</div><div>操作</div>
        </div>
        <div v-if="loading" class="empty">加载中…</div>
        <div v-else-if="!docs.length" class="empty">还没有提交的文档</div>
        <template v-else>
          <div v-for="doc in docs" :key="doc.id" class="krow">
            <div class="cell no">{{ String(doc.id).padStart(4, '0') }}</div>
            <div class="cell fname" :title="doc.filename">
              <div>{{ doc.filename }}</div><div class="sub">#{{ doc.file_id }}</div>
            </div>
            <div class="cell hide-s">{{ doc.uploader || '' }}</div>
            <div class="cell hide-s">{{ doc.department || '' }}</div>
            <div class="cell"><span class="badge" :class="statusClass[doc.status] || 'b-gray'">{{ statusText[doc.status] || doc.status }}</span></div>
            <div class="cell">
              <span class="badge" :class="indexClass[doc.index_status] || 'b-gray'">{{ indexText[doc.index_status] || doc.index_status }}</span>
              <span v-if="doc.error_message" class="err-text">{{ doc.error_message }}</span>
            </div>
            <div class="cell ops">
              <button v-if="doc.status === 'rejected' && doc.uploader === props.user.username" class="act" @click="resubmit(doc)">重新提交</button>
            </div>
          </div>
        </template>
        <div class="pager">
          <button :disabled="page <= 1" @click="go(page - 1)">上一页</button>
          <span class="info">{{ page }} / {{ pages }} 共 {{ total }} 条</span>
          <button :disabled="page >= pages" @click="go(page + 1)">下一页</button>
        </div>
      </div>
    </div>
  </section>
</template>
