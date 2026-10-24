<script setup lang="ts">
import { nextTick, onMounted, ref } from 'vue'
import {
  chatAsk,
  deleteSession,
  downloadFile,
  getMessages,
  getSessions,
  type ChatMessage,
  type ChatSession,
  type Source,
} from '../api'

defineProps<{ user?: unknown }>()

const sessions = ref<ChatSession[]>([])
const messages = ref<ChatMessage[]>([])
const sessionId = ref<number | null>(null)
const input = ref('')
const loading = ref(false)
const status = ref('')
const chatBox = ref<HTMLElement | null>(null)

async function loadSessions() {
  try {
    sessions.value = await getSessions()
  } catch (exc) {
    status.value = exc instanceof Error ? exc.message : '会话加载失败'
  }
}

async function openSession(id: number) {
  try {
    const data = await getMessages(id)
    sessionId.value = id
    messages.value = data.map((message) => ({
      ...message,
      content: typeof message.content === 'string' ? message.content : JSON.stringify(message.content),
    }))
    await scrollBottom()
  } catch (exc) {
    status.value = exc instanceof Error ? exc.message : '消息加载失败'
  }
}

function newSession() {
  sessionId.value = null
  messages.value = []
  input.value = ''
  const url = new URL(location.href)
  url.searchParams.delete('session')
  history.replaceState(null, '', url)
}

async function removeSession(id: number) {
  if (!window.confirm(`删除会话 #${id}？删除后不可恢复。`)) return
  try {
    await deleteSession(id)
    if (sessionId.value === id) newSession()
    await loadSessions()
  } catch (exc) {
    status.value = exc instanceof Error ? exc.message : '删除失败'
  }
}

async function send() {
  const text = input.value.trim()
  if (!text || loading.value) return
  const priorMessages = messages.value.slice(-20).map((message) => ({
    role: message.role,
    content: message.content,
  }))
  input.value = ''
  messages.value.push({ role: 'user', content: text })
  loading.value = true
  status.value = 'AI 思考中'
  await scrollBottom()
  try {
    const answer = await chatAsk(text, priorMessages, sessionId.value)
    messages.value.push({
      role: 'assistant',
      content: answer.answer,
      sources: answer.sources || [],
      data_sources: answer.data_sources || [],
    })
    if (answer.session_id) {
      sessionId.value = answer.session_id
      const url = new URL(location.href)
      url.searchParams.set('session', String(answer.session_id))
      history.replaceState(null, '', url)
    }
    await loadSessions()
    await scrollBottom()
  } catch (exc) {
    status.value = exc instanceof Error ? exc.message : '发送失败'
  } finally {
    loading.value = false
  }
}

async function scrollBottom() {
  await nextTick()
  if (chatBox.value) chatBox.value.scrollTop = chatBox.value.scrollHeight
}

async function openSource(source: Source) {
  if (!source.file_id) return
  try {
    await downloadFile(source.file_id, source.filename || `file-${source.file_id}`)
  } catch (exc) {
    status.value = exc instanceof Error ? exc.message : '下载失败'
  }
}

// 正文里的 [N] 是引用标记，N 对应 sources[N-1]（后端契约见 agents/graph.py）。
// 视觉与交互对齐旧版 app.html：渲染成黑底圆角徽章，点击弹出「来源文件」卡片。
// 必须转义后再替换：内容来自检索到的文档，属于不可信输入，直接 v-html 会开出 XSS 口子。
const HTML_ESCAPE: Record<string, string> = {
  '&': '&amp;',
  '<': '&lt;',
  '>': '&gt;',
  '"': '&quot;',
  "'": '&#39;',
}

function escapeHtml(text: string): string {
  return text.replace(/[&<>"']/g, (ch) => HTML_ESCAPE[ch] || ch)
}

function formatContent(text: string, sources?: Source[]): string {
  const safe = escapeHtml(text || '')
  if (!sources?.length) return safe
  return safe.replace(/\[(\d+)\]/g, (raw, digits: string) => {
    const index = Number(digits)
    if (!Number.isInteger(index) || index < 1 || index > sources.length) return raw
    return `<span class="ref-tag" data-cite="${index}" title="点击查看来源">${index}</span>`
  })
}

const sourceCard = ref<{ index: number; source: Source } | null>(null)

function openSourceCard(index: number, sources?: Source[]) {
  const source = sources?.[index - 1]
  if (!source) return
  sourceCard.value = { index, source }
}

function closeSourceCard() {
  sourceCard.value = null
}

function onContentClick(event: MouseEvent, sources?: Source[]) {
  const target = (event.target as HTMLElement | null)?.closest('.ref-tag') as HTMLElement | null
  if (!target) return
  openSourceCard(Number(target.dataset.cite || 0), sources)
}

function fmtSize(bytes: unknown): string {
  const size = Number(bytes)
  if (!Number.isFinite(size) || size <= 0) return '—'
  if (size < 1024) return `${size} B`
  if (size < 1024 * 1024) return `${(size / 1024).toFixed(1)} KB`
  return `${(size / 1024 / 1024).toFixed(1)} MB`
}

onMounted(async () => {
  await loadSessions()
  const id = Number(new URLSearchParams(location.search).get('session') || 0)
  if (id) await openSession(id)
})
</script>

<template>
  <section class="view">
    <div class="card">
      <div class="chat-head">
        <div>
          <h3>智能问答</h3>
          <div class="hint">基于企业知识库对话；回答里的 [N] 编号对应来源文件，可点击下载。</div>
        </div>
        <button class="btn btn-sm btn-outline" @click="newSession">＋ 新建会话</button>
      </div>
      <div class="chat-layout">
        <aside class="chat-side">
          <div class="session-list">
            <div v-if="!sessions.length" class="session-empty">暂无会话</div>
            <div
              v-for="session in sessions"
              :key="session.id"
              class="session-item"
              :class="{ on: session.id === sessionId }"
              @click="openSession(session.id)"
            >
              <span class="s-title" :title="session.title || session.last_message">
                {{ session.title || session.last_message || `会话 #${session.id}` }}
              </span>
              <span class="s-id">#{{ session.id }}</span>
              <button class="s-del" title="删除会话" @click.stop="removeSession(session.id)">×</button>
            </div>
          </div>
        </aside>
        <div class="chat-wrap">
          <div ref="chatBox" class="chat-box">
            <div v-if="!messages.length" class="chat-empty">
              你好，我是企业知识助手。<br />
              可以问我：午餐政策、报销流程、技术部有哪些文档。
            </div>
            <div v-for="(message, index) in messages" :key="index" class="chat-msg" :class="message.role">
              <div
                class="content"
                v-html="formatContent(message.content, message.sources)"
                @click="onContentClick($event, message.sources)"
              ></div>
              <div v-if="message.data_sources?.length" class="data-card">
                数据来源：<span v-for="source in message.data_sources" :key="source.label">{{ source.label }} </span>
              </div>
              <div v-if="message.sources?.length" class="source-row">
                <button
                  v-for="(source, si) in message.sources"
                  :key="`${si}-${source.file_id}`"
                  class="source-chip"
                  @click="openSourceCard(si + 1, message.sources)"
                >
                  [{{ si + 1 }}] {{ source.filename || '来源文件' }}
                </button>
              </div>
            </div>
          </div>
          <div class="chat-composer">
            <textarea
              v-model="input"
              placeholder="输入你的问题，Enter 发送，Shift+Enter 换行"
              @keydown.enter.exact.prevent="send"
            />
            <button :disabled="loading" @click="send">{{ loading ? '思考中' : '发送' }}</button>
          </div>
          <div class="chat-status" :class="{ error: status.includes('失败') || status.includes('错误') }">{{ status }}</div>
        </div>
      </div>
    </div>
    <div v-if="sourceCard" class="src-mask" @click.self="closeSourceCard">
      <div class="src-card">
        <span class="src-close" @click="closeSourceCard">×</span>
        <h3>来源 [{{ sourceCard.index }}]</h3>
        <div class="src-kv">
          <span class="k">文件名</span><span>{{ sourceCard.source.filename || '—' }}</span>
          <span class="k">部门</span><span>{{ sourceCard.source.department || '—' }}</span>
          <span class="k">大小</span><span>{{ fmtSize(sourceCard.source.size_bytes) }}</span>
          <span class="k">类型</span><span>{{ sourceCard.source.content_type || '—' }}</span>
        </div>
        <div class="src-actions">
          <button class="btn btn-sm btn-outline" @click="closeSourceCard">关闭</button>
          <button class="btn btn-sm btn-dark" @click="openSource(sourceCard.source)">下载</button>
        </div>
      </div>
    </div>
  </section>
</template>

<style scoped>
/* 与旧版 app.html 的 .ref-tag 保持一致：黑底白字圆角徽章，视觉上就是个可点的标记。
   用纯色文字或上标会让人以为是排版符号，不会想去点。 */
.content :deep(.ref-tag) {
  display: inline-block;
  min-width: 18px;
  height: 18px;
  line-height: 18px;
  text-align: center;
  font-size: 11px;
  color: var(--background, #fff);
  background: var(--foreground, #161616);
  border-radius: 999px;
  padding: 0 4px;
  margin: 0 3px;
  cursor: pointer;
  vertical-align: 2px;
  user-select: none;
  font-family: var(--font-mono, monospace);
}
.content :deep(.ref-tag:hover) {
  background: rgba(22, 22, 22, 0.8);
}

.src-mask {
  position: fixed;
  inset: 0;
  background: rgba(0, 0, 0, 0.35);
  z-index: 100;
  display: flex;
  align-items: center;
  justify-content: center;
}
.src-card {
  background: var(--card, #fff);
  border: 1px solid var(--border, #e4e2da);
  border-radius: 16px;
  width: 460px;
  max-width: 92vw;
  padding: 18px 20px;
  position: relative;
}
.src-card h3 {
  font-size: 15px;
  font-weight: 500;
  margin-bottom: 12px;
}
.src-kv {
  display: grid;
  grid-template-columns: 84px 1fr;
  gap: 7px 10px;
  font-size: 13.5px;
}
.src-kv .k {
  color: var(--muted, #7c7c7c);
}
.src-actions {
  display: flex;
  gap: 10px;
  margin-top: 16px;
}
.src-actions button {
  flex: 1;
}
.src-close {
  position: absolute;
  top: 12px;
  right: 16px;
  color: var(--muted, #7c7c7c);
  cursor: pointer;
  font-size: 18px;
  line-height: 1;
}
</style>
