<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { clearAuth, getStoredUser, getToken, type User } from './api'
import LoginPanel from './components/LoginPanel.vue'
import ChatView from './views/ChatView.vue'
import DocsView from './views/DocsView.vue'
import FilesView from './views/FilesView.vue'
import GraphView from './views/GraphView.vue'
import ReviewView from './views/ReviewView.vue'

type Tab = 'search' | 'docs' | 'review' | 'files' | 'graph'

const stored = getStoredUser()
const user = ref<User | null>(getToken() ? stored : null)
const online = ref(false)
const activeTab = ref<Tab>(readTab())

// 登录入口只保留首页那一处（static/index.html 的 CTA 登录框）。
// 直接打开 /app 会走到这里，此时不该甩出一个和首页重复的孤立登录面板，
// 而是带着回跳地址回首页登录区；登录成功后由首页跳回来。
// redirectingToLogin 用来在跳转发生前压住 LoginPanel，避免闪一下。
const redirectingToLogin = !user.value
if (redirectingToLogin) {
  const next = encodeURIComponent(location.pathname + location.search)
  location.replace(`/?next=${next}#login`)
}

const isManager = computed(() => (user.value?.role || 'employee') !== 'employee')
const whoText = computed(() => {
  if (!user.value) return ''
  return [user.value.username, user.value.department, user.value.position].filter(Boolean).join(' · ')
})
const activeView = computed(() => {
  if (activeTab.value === 'search') return ChatView
  if (activeTab.value === 'docs') return DocsView
  if (activeTab.value === 'review') return ReviewView
  if (activeTab.value === 'graph') return GraphView
  return FilesView
})

function readTab(): Tab {
  const value = new URLSearchParams(location.search).get('view')
  if (value === 'docs' || value === 'review' || value === 'files' || value === 'search' || value === 'graph') return value
  return 'search'
}

function setTab(tab: Tab) {
  if (tab === 'review' && !isManager.value) return
  if (tab === 'graph' && !isManager.value) return
  activeTab.value = tab
  const url = new URL(location.href)
  url.searchParams.set('view', tab)
  history.replaceState(null, '', url)
}

function onLogin(next: User) {
  user.value = next
  setTab('search')
}

function logout() {
  clearAuth()
  user.value = null
}

async function checkHealth() {
  try {
    const res = await fetch('/health')
    online.value = res.ok
  } catch {
    online.value = false
  }
}

onMounted(() => {
  window.addEventListener('kb:unauthorized', logout)
  checkHealth()
  if (user.value && !isManager.value && activeTab.value === 'review') activeTab.value = 'search'
  if (user.value && !isManager.value && activeTab.value === 'graph') activeTab.value = 'search'
  const interval = window.setInterval(checkHealth, 30000)
  window.addEventListener('beforeunload', () => window.clearInterval(interval), { once: true })
})
</script>

<template>
  <LoginPanel v-if="!user && !redirectingToLogin" @success="onLogin" />

  <template v-else>
    <header class="nav scrolled">
      <nav class="nav-inner">
        <a class="nav-logo" href="/"><span class="name">企业知识库</span><span class="tm">工作台</span></a>
        <div class="nav-right">
          <span class="who">{{ whoText }}</span>
          <span class="who"><span class="dot" :class="{ off: !online }"></span>{{ online ? '在线' : '离线' }}</span>
          <button class="btn btn-sm btn-outline" @click="logout">退出</button>
        </div>
      </nav>
    </header>

    <main class="wrap">
      <div class="eyebrow">工作台</div>
      <h1 class="head-title">企业内部知识库</h1>

      <div class="tabs">
        <button class="tab" :class="{ on: activeTab === 'search' }" @click="setTab('search')">智能问答</button>
        <button class="tab" :class="{ on: activeTab === 'docs' }" @click="setTab('docs')">知识文档</button>
        <button v-if="isManager" class="tab" :class="{ on: activeTab === 'review' }" @click="setTab('review')">审核</button>
        <button v-if="isManager" class="tab" :class="{ on: activeTab === 'graph' }" @click="setTab('graph')">知识图谱</button>
        <button class="tab" :class="{ on: activeTab === 'files' }" @click="setTab('files')">文件管理</button>
      </div>

      <component :is="activeView" :user="user" @logout="logout" />
    </main>
  </template>
</template>
