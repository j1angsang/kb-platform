export interface User {
  username: string
  department?: string
  role?: string
  position?: string
}

export interface PageResult<T> {
  items: T[]
  page: number
  page_size: number
  total: number
}

export interface DocItem {
  id: number
  file_id: number
  filename: string
  uploader?: string
  department?: string
  status: string
  index_status: string
  error_message?: string
  is_shared?: boolean
  shared_departments?: string
  created_at?: string
}

export interface FileItem {
  id: number
  filename: string
  content_type: string
  size_bytes: number
  uploader?: string
  department?: string
  created_at?: string
  is_shared?: boolean
  shared_departments?: string
}

export interface ChatSession {
  id: number
  title?: string
  last_message?: string
  updated_at?: string
}

export interface Source {
  file_id: number
  filename?: string
  [key: string]: unknown
}

export interface DataSource {
  kind?: string
  label?: string
  department?: string
}

export interface ChatMessage {
  role: 'user' | 'assistant'
  content: string
  sources?: Source[]
  data_sources?: DataSource[]
}

export interface ChatAnswer {
  answer: string
  sources?: Source[]
  data_sources?: DataSource[]
  session_id?: number
}

const TOKEN_KEY = 'kb_token'
const USER_KEY = 'kb_user'

export class ApiError extends Error {
  status: number
  constructor(status: number, message: string) {
    super(message)
    this.status = status
  }
}

export function getToken(): string {
  return localStorage.getItem(TOKEN_KEY) || ''
}

export function getStoredUser(): User | null {
  try {
    return JSON.parse(localStorage.getItem(USER_KEY) || 'null')
  } catch {
    return null
  }
}

export function setAuth(data: User & { access_token: string }): User {
  localStorage.setItem(TOKEN_KEY, data.access_token)
  const user = {
    username: data.username,
    department: data.department,
    role: data.role,
    position: data.position,
  }
  localStorage.setItem(USER_KEY, JSON.stringify(user))
  return user
}

export function clearAuth(): void {
  localStorage.removeItem(TOKEN_KEY)
  localStorage.removeItem(USER_KEY)
}

function headers(json = false): HeadersInit {
  const result: Record<string, string> = {}
  if (json) result['Content-Type'] = 'application/json'
  const token = getToken()
  if (token) result.Authorization = `Bearer ${token}`
  return result
}

async function errorMessage(res: Response): Promise<string> {
  const data = await res.json().catch(() => ({}))
  return data.detail || data.error?.message || `请求失败 (${res.status})`
}

export async function api<T>(url: string, init: RequestInit = {}): Promise<T> {
  const res = await fetch(url, {
    ...init,
    headers: { ...(init.headers || {}), ...headers(!!init.body && !(init.body instanceof FormData)) },
  })
  if (res.status === 401) {
    clearAuth()
    window.dispatchEvent(new CustomEvent('kb:unauthorized'))
    throw new ApiError(401, '登录已过期')
  }
  if (!res.ok) throw new ApiError(res.status, await errorMessage(res))
  return (await res.json()) as T
}

export async function login(username: string, password: string): Promise<User> {
  const res = await fetch('/auth/login', {
    method: 'POST',
    headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
    body: new URLSearchParams({ username, password }),
  })
  if (!res.ok) throw new ApiError(res.status, await errorMessage(res))
  return setAuth(await res.json())
}

export function getDocs(params: Record<string, string | number | undefined>): Promise<PageResult<DocItem>> {
  const query = new URLSearchParams()
  Object.entries(params).forEach(([key, value]) => {
    if (value !== undefined && value !== '') query.set(key, String(value))
  })
  return api(`/knowledge/docs?${query.toString()}`)
}

export function getFiles(params: Record<string, string | number | undefined>): Promise<PageResult<FileItem>> {
  const query = new URLSearchParams()
  Object.entries(params).forEach(([key, value]) => {
    if (value !== undefined && value !== '') query.set(key, String(value))
  })
  return api(`/files?${query.toString()}`)
}

export async function uploadFile(file: File, shared = false): Promise<FileItem> {
  const form = new FormData()
  form.append('file', file)
  form.append('shared', String(shared))
  return api<FileItem>('/files/upload', { method: 'POST', body: form })
}

export function submitDoc(fileId: number): Promise<DocItem> {
  return api('/knowledge/submit', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ file_id: fileId }),
  })
}

export function resubmitDoc(docId: number): Promise<DocItem> {
  return api(`/knowledge/${docId}/resubmit`, { method: 'POST' })
}

export function approveDoc(docId: number, comment: string): Promise<DocItem> {
  return api(`/knowledge/${docId}/approve`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ comment }),
  })
}

export function rejectDoc(docId: number, comment: string): Promise<DocItem> {
  return api(`/knowledge/${docId}/reject`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ comment }),
  })
}

export function deleteFile(fileId: number): Promise<{ ok: boolean }> {
  return api(`/files/${fileId}`, { method: 'DELETE' })
}

export async function downloadFile(fileId: number, filename: string): Promise<void> {
  const res = await fetch(`/files/${fileId}/download`, { headers: headers() })
  if (res.status === 401) {
    clearAuth()
    window.dispatchEvent(new CustomEvent('kb:unauthorized'))
    throw new ApiError(401, '登录已过期')
  }
  if (!res.ok) throw new ApiError(res.status, '下载失败')
  const url = URL.createObjectURL(await res.blob())
  const anchor = document.createElement('a')
  anchor.href = url
  anchor.download = filename
  document.body.appendChild(anchor)
  anchor.click()
  anchor.remove()
  URL.revokeObjectURL(url)
}

export function getSessions(): Promise<ChatSession[]> {
  return api('/chat/sessions')
}

export function getMessages(sessionId: number): Promise<ChatMessage[]> {
  return api(`/chat/sessions/${sessionId}/messages`)
}

export function deleteSession(sessionId: number): Promise<{ ok: boolean }> {
  return api(`/chat/sessions/${sessionId}`, { method: 'DELETE' })
}

export function chatAsk(message: string, history: ChatMessage[], sessionId: number | null): Promise<ChatAnswer> {
  return api('/chat/ask', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ message, history, session_id: sessionId }),
  })
}

export interface GraphEntity {
  id: number
  name: string
  type: string
  aliases: string[]
  mention_count: number
}

export interface GraphEdge {
  direction: 'in' | 'out'
  predicate: string
  other_id?: number | null
  other_name?: string | null
  other_type?: string | null
  value?: string | null
  chunk_id: string
  confidence: number
  evidence?: string | null
  evidence_filename?: string | null
}

export interface GraphNeighbors {
  entity: GraphEntity | null
  edges: GraphEdge[]
  hidden_edges: number
}

export interface GraphStats {
  entities: number
  merged_entities: number
  relations: number
  value_relations: number
  by_type: { type: string; count: number }[]
  kg_enabled: boolean
  docs_by_status: Record<string, number>
}

export function getGraphStats(): Promise<GraphStats> {
  return api('/graph/stats')
}

export function getGraphEntities(params: {
  q?: string
  type?: string
  limit?: number
  offset?: number
}): Promise<{ items: GraphEntity[]; total: number }> {
  const query = new URLSearchParams()
  Object.entries(params).forEach(([key, value]) => {
    if (value !== undefined && value !== '') query.set(key, String(value))
  })
  return api(`/graph/entities?${query.toString()}`)
}

export function getEntityNeighbors(id: number, limit = 40): Promise<GraphNeighbors> {
  return api(`/graph/entities/${id}/neighbors?limit=${limit}`)
}

export function mergeGraphEntities(sourceId: number, targetId: number): Promise<{ moved_edges: number }> {
  return api('/graph/entities/merge', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ source_id: sourceId, target_id: targetId }),
  })
}

export function splitGraphEntity(id: number, alias: string): Promise<{ new_entity_id: number }> {
  return api(`/graph/entities/${id}/split`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ alias }),
  })
}

export function graphGc(): Promise<{ orphans_deleted: number }> {
  return api('/graph/gc', { method: 'POST' })
}

export function rebuildDocGraph(docId: number): Promise<Record<string, unknown>> {
  return api(`/graph/docs/${docId}/rebuild`, { method: 'POST' })
}

export function graphLinkPreview(q: string): Promise<{
  query: string
  max_hops: number
  entities: { id: number; name: string; type: string; score: number; via: string }[]
  chunks: { chunk_id: string; hop: number; confidence: number; department?: string; preview: string }[]
}> {
  return api(`/graph/link?q=${encodeURIComponent(q)}`)
}
