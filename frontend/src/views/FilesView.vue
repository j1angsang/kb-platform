<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { deleteFile, downloadFile, getFiles, uploadFile, type FileItem, type User } from '../api'

const props = defineProps<{ user: User }>()
const files = ref<FileItem[]>([])
const total = ref(0)
const page = ref(1)
const pageSize = 10
const department = ref('')
const loading = ref(false)
const error = ref('')
const uploadLog = ref('')
const uploading = ref(false)
const fileInput = ref<HTMLInputElement | null>(null)

const isManager = computed(() => (props.user.role || 'employee') !== 'employee')
const pages = computed(() => Math.max(1, Math.ceil(total.value / pageSize)))
const pageBytes = computed(() => files.value.reduce((sum, file) => sum + file.size_bytes, 0))
const todayCount = computed(() => {
  const today = new Date().toDateString()
  return files.value.filter((file) => file.created_at && new Date(file.created_at).toDateString() === today).length
})

async function load() {
  loading.value = true
  error.value = ''
  try {
    const data = await getFiles({
      page: page.value,
      page_size: pageSize,
      department: isManager.value ? department.value : undefined,
    })
    files.value = data.items || []
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

function onDrop(event: DragEvent) {
  handleFiles(event.dataTransfer?.files || null)
}

async function handleFiles(selected: FileList | null) {
  if (!selected?.length) return
  uploading.value = true
  uploadLog.value = ''
  for (const file of Array.from(selected)) {
    uploadLog.value += `上传 ${file.name}…\n`
    try {
      await uploadFile(file)
      uploadLog.value += `完成 ${file.name}\n`
    } catch (exc) {
      uploadLog.value += `失败 ${file.name}：${exc instanceof Error ? exc.message : '未知错误'}\n`
    }
  }
  uploading.value = false
  page.value = 1
  await load()
}

async function download(file: FileItem) {
  try {
    await downloadFile(file.id, file.filename)
  } catch (exc) {
    error.value = exc instanceof Error ? exc.message : '下载失败'
  }
}

async function remove(file: FileItem) {
  if (!window.confirm(`删除文件 ${file.filename}？`)) return
  try {
    await deleteFile(file.id)
    await load()
  } catch (exc) {
    error.value = exc instanceof Error ? exc.message : '删除失败'
  }
}

function onDepartmentChange() {
  page.value = 1
  load()
}

function fmtSize(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`
  return `${(bytes / 1024 / 1024).toFixed(1)} MB`
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
    <div class="statstrip">
      <div class="bigstat"><b>{{ total }}</b><i>份文件</i></div>
      <div class="bigstat"><b>{{ fmtSize(pageBytes) }}</b><i>本页占用</i></div>
      <div class="bigstat"><b style="color: var(--green)">+{{ todayCount }}</b><i>本页今日新增</i></div>
    </div>
    <div class="grid2">
      <div class="card">
        <h3>上传文件</h3>
        <div class="hint">支持 txt / md / pdf / docx / xlsx；上传后可在文档页提交审核。</div>
        <div class="dropzone" :class="{ over: uploading }" @click="pickFiles" @dragover.prevent @drop.prevent="onDrop">
          <div class="t">{{ uploading ? '上传中…' : '拖拽文件到这里，或点击选择' }}</div>
          <div class="h">文件会进入文件管理，不会自动入库</div>
          <input ref="fileInput" type="file" multiple hidden @change="onFileInput" />
        </div>
        <div v-if="uploadLog" class="upload-log">{{ uploadLog }}</div>
      </div>

      <div class="card">
        <h3>文件列表</h3>
        <div v-if="isManager" class="filter-row">
          <span>部门筛选</span>
          <select v-model="department" @change="onDepartmentChange">
            <option value="">全部</option>
            <option>技术部</option><option>销售部</option><option>人事部</option>
            <option>财务部</option><option>行政部</option><option>未分配</option>
          </select>
        </div>
        <div class="fhead">
          <div>序号</div><div>文件</div><div>上传人</div><div>大小</div><div>时间</div><div>操作</div>
        </div>
        <div v-if="loading" class="empty">加载中…</div>
        <div v-else-if="!files.length" class="empty">还没有文件</div>
        <template v-else>
          <div v-for="file in files" :key="file.id" class="frow">
            <div class="cell no">{{ String(file.id).padStart(4, '0') }}</div>
            <div class="cell fname">
              <div>{{ file.filename }}</div><div class="sub">{{ file.content_type }}</div>
            </div>
            <div class="cell up">{{ file.uploader || '' }}</div>
            <div class="cell size">{{ fmtSize(file.size_bytes) }}</div>
            <div class="cell age">{{ relTime(file.created_at) }}</div>
            <div class="cell ops">
              <button class="act" @click="download(file)">下载</button>
              <button v-if="isManager" class="act bad" @click="remove(file)">删除</button>
            </div>
          </div>
        </template>
        <div class="pager">
          <button :disabled="page <= 1" @click="go(page - 1)">上一页</button>
          <span class="info">{{ page }} / {{ pages }} 共 {{ total }} 条</span>
          <button :disabled="page >= pages" @click="go(page + 1)">下一页</button>
        </div>
        <div v-if="error" class="err-text">{{ error }}</div>
      </div>
    </div>
  </section>
</template>
