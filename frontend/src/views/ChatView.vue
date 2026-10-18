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
              <div class="content">{{ message.content }}</div>
              <div v-if="message.data_sources?.length" class="data-card">
                数据来源：<span v-for="source in message.data_sources" :key="source.label">{{ source.label }} </span>
              </div>
              <div v-if="message.sources?.length" class="source-row">
                <button
                  v-for="source in message.sources"
                  :key="source.file_id"
                  class="source-chip"
                  @click="openSource(source)"
                >
                  [{{ source.file_id }}] {{ source.filename || '来源文件' }}
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
  </section>
</template>
