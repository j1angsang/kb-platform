<script setup lang="ts">
import { ref } from 'vue'
import { login, type User } from '../api'

const emit = defineEmits<{ success: [user: User] }>()
const username = ref('')
const password = ref('')
const loading = ref(false)
const error = ref('')

async function submit() {
  if (!username.value || !password.value) {
    error.value = '请输入账号和密码'
    return
  }
  loading.value = true
  error.value = ''
  try {
    emit('success', await login(username.value, password.value))
  } catch (exc) {
    error.value = exc instanceof Error ? exc.message : '登录失败'
  } finally {
    loading.value = false
  }
}
</script>

<template>
  <main class="auth-wrap">
    <div class="auth-card card">
      <div class="eyebrow">KB PLATFORM</div>
      <h1 class="head-title auth-title">企业知识库</h1>
      <p class="hint">登录后进入工作台：上传文档、审核入库、检索问答。</p>
      <form @submit.prevent="submit">
        <label class="auth-field">
          <span>账号</span>
          <input v-model.trim="username" autocomplete="username" autofocus placeholder="username" />
        </label>
        <label class="auth-field">
          <span>密码</span>
          <input v-model="password" type="password" autocomplete="current-password" placeholder="password" />
        </label>
        <button class="btn btn-dark btn-lg auth-submit" :disabled="loading" type="submit">
          {{ loading ? '登录中' : '登录工作台' }}
        </button>
      </form>
      <div v-if="error" class="auth-error">{{ error }}</div>
      <p class="auth-foot">JWT 鉴权 · 部门隔离 · 知识检索</p>
    </div>
  </main>
</template>
