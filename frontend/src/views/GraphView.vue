<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import {
  getEntityNeighbors,
  getGraphEntities,
  getGraphStats,
  graphGc,
  graphLinkPreview,
  mergeGraphEntities,
  splitGraphEntity,
  type GraphEdge,
  type GraphEntity,
  type GraphNeighbors,
  type GraphStats,
  type User,
} from '../api'

defineProps<{ user: User }>()

const stats = ref<GraphStats | null>(null)
const entities = ref<GraphEntity[]>([])
const total = ref(0)
const keyword = ref('')
const typeFilter = ref('')
const loading = ref(false)
const error = ref('')
const notice = ref('')

const selected = ref<GraphEntity | null>(null)
const neighbors = ref<GraphNeighbors | null>(null)
const mergeTarget = ref<number | null>(null)

const linkQuery = ref('')
const linkResult = ref<Awaited<ReturnType<typeof graphLinkPreview>> | null>(null)

const TYPE_TEXT: Record<string, string> = {
  department: '部门', position: '岗位', role: '角色', policy: '制度',
  process: '流程', system: '系统', form: '表单', org: '组织',
}
const TYPE_COLOR: Record<string, string> = {
  department: '#185FA5', position: '#0F6E56', role: '#854F0B',
  policy: '#534AB7', process: '#993C1D', system: '#5F5E5A',
  form: '#993556', org: '#3B6D11',
}
const typeText = (t: string) => TYPE_TEXT[t] || t
const typeColor = (t: string) => TYPE_COLOR[t] || '#5F5E5A'

async function loadStats() {
  try {
    stats.value = await getGraphStats()
  } catch (exc) {
    error.value = exc instanceof Error ? exc.message : '统计加载失败'
  }
}

async function loadEntities() {
  loading.value = true
  error.value = ''
  try {
    const data = await getGraphEntities({
      q: keyword.value.trim() || undefined,
      type: typeFilter.value || undefined,
      limit: 50,
    })
    entities.value = data.items || []
    total.value = data.total || 0
  } catch (exc) {
    error.value = exc instanceof Error ? exc.message : '实体加载失败'
  } finally {
    loading.value = false
  }
}

async function pick(entity: GraphEntity) {
  selected.value = entity
  notice.value = ''
  try {
    neighbors.value = await getEntityNeighbors(entity.id)
  } catch (exc) {
    error.value = exc instanceof Error ? exc.message : '邻域加载失败'
  }
}

const ringNodes = computed(() => {
  const edges = neighbors.value?.edges || []
  const seen = new Set<number>()
  const picked: GraphEdge[] = []
  for (const edge of edges) {
    if (!edge.other_id || seen.has(edge.other_id)) continue
    seen.add(edge.other_id)
    picked.push(edge)
    if (picked.length >= 10) break
  }
  const radius = 108
  return picked.map((edge, index) => {
    const angle = (Math.PI * 2 * index) / picked.length - Math.PI / 2
    return {
      edge,
      x: 200 + radius * Math.cos(angle),
      y: 150 + radius * Math.sin(angle),
    }
  })
})

async function doMerge(fromId: number) {
  if (!mergeTarget.value) {
    error.value = '先在左侧点选一个实体作为合并目标'
    return
  }
  if (fromId === mergeTarget.value) {
    error.value = '不能合并到自己'
    return
  }
  try {
    const res = await mergeGraphEntities(fromId, mergeTarget.value)
    notice.value = `已合并，迁移 ${res.moved_edges} 条边`
    mergeTarget.value = null
    await Promise.all([loadStats(), loadEntities()])
  } catch (exc) {
    error.value = exc instanceof Error ? exc.message : '合并失败'
  }
}

async function doSplit(alias: string) {
  if (!selected.value) return
  try {
    await splitGraphEntity(selected.value.id, alias)
    notice.value = `已拆分出实体「${alias}」`
    await Promise.all([loadStats(), loadEntities()])
    await pick(selected.value)
  } catch (exc) {
    error.value = exc instanceof Error ? exc.message : '拆分失败'
  }
}

async function doGc() {
  try {
    const res = await graphGc()
    notice.value = `已清理孤儿实体 ${res.orphans_deleted} 个`
    await loadStats()
  } catch (exc) {
    error.value = exc instanceof Error ? exc.message : '清理失败'
  }
}

async function runLink() {
  if (!linkQuery.value.trim()) return
  error.value = ''
  try {
    linkResult.value = await graphLinkPreview(linkQuery.value.trim())
  } catch (exc) {
    error.value = exc instanceof Error ? exc.message : '链接预览失败'
  }
}

onMounted(async () => {
  await Promise.all([loadStats(), loadEntities()])
})
</script>

<template>
  <section class="view">
    <div class="card">
      <h3>知识图谱</h3>
      <div class="hint">
        实体只存名字与关系，不含正文；邻域里的证据按部门权限过滤，无权内容只计数不返回。
      </div>
      <div class="gstats" v-if="stats">
        <span class="gstat"><b>{{ stats.entities }}</b> 实体</span>
        <span class="gstat"><b>{{ stats.relations }}</b> 关系</span>
        <span class="gstat"><b>{{ stats.value_relations }}</b> 字面值边</span>
        <span class="gstat"><b>{{ stats.merged_entities }}</b> 已合并</span>
        <span class="gstat" :class="stats.kg_enabled ? 'on' : 'off'">
          {{ stats.kg_enabled ? 'KG 已启用' : 'KG 未启用' }}
        </span>
        <span v-for="t in stats.by_type" :key="t.type" class="gtype" :style="{ borderColor: typeColor(t.type) }">
          {{ typeText(t.type) }} {{ t.count }}
        </span>
      </div>

      <div class="gtools">
        <input v-model="keyword" class="ginput" placeholder="搜索实体名或别名" @keyup.enter="loadEntities" />
        <select v-model="typeFilter" class="ginput gselect" @change="loadEntities">
          <option value="">全部类型</option>
          <option v-for="(label, key) in TYPE_TEXT" :key="key" :value="key">{{ label }}</option>
        </select>
        <button class="btn btn-sm btn-outline" @click="loadEntities">查询</button>
        <button class="btn btn-sm btn-outline" @click="doGc">清理孤儿</button>
      </div>

      <div class="ggrid">
        <div class="glist">
          <div class="glist-head">实体（{{ total }}）</div>
          <div v-if="loading" class="empty">加载中…</div>
          <div v-else-if="!entities.length" class="empty">还没有实体，先跑 scripts/build_graph.py</div>
          <div
            v-for="item in entities"
            :key="item.id"
            class="gentity"
            :class="{ active: selected?.id === item.id }"
            @click="pick(item)"
          >
            <span class="gdot" :style="{ background: typeColor(item.type) }"></span>
            <span class="gname">{{ item.name }}</span>
            <span class="gmeta">{{ typeText(item.type) }} · {{ item.mention_count }}</span>
          </div>
        </div>

        <div class="gcanvas">
          <div v-if="!selected" class="empty">左侧选一个实体，看它的邻域</div>
          <template v-else>
            <div class="gcenter-title">
              {{ selected.name }}
              <span class="gmeta">{{ typeText(selected.type) }}</span>
            </div>
            <svg viewBox="0 0 400 300" class="gsvg">
              <line
                v-for="node in ringNodes"
                :key="'l' + (node.edge.other_id || 0)"
                :x1="200" :y1="150" :x2="node.x" :y2="node.y"
                stroke="#C9C7BE" stroke-width="1"
              />
              <circle cx="200" cy="150" r="30" :fill="typeColor(selected.type)" opacity="0.9" />
              <text x="200" y="150" text-anchor="middle" dominant-baseline="central" fill="#fff" font-size="11">
                {{ selected.name.slice(0, 5) }}
              </text>
              <g v-for="node in ringNodes" :key="node.edge.other_id || 0">
                <circle :cx="node.x" :cy="node.y" r="20" :fill="typeColor(node.edge.other_type || '')" opacity="0.85" />
                <text
                  :x="node.x" :y="node.y" text-anchor="middle" dominant-baseline="central"
                  fill="#fff" font-size="10"
                >{{ (node.edge.other_name || '').slice(0, 4) }}</text>
                <text
                  :x="(200 + node.x) / 2" :y="(150 + node.y) / 2" text-anchor="middle"
                  fill="#6B6A63" font-size="9"
                >{{ node.edge.predicate }}</text>
              </g>
            </svg>

            <div class="gedges">
              <div v-if="neighbors?.hidden_edges" class="ghidden">
                有 {{ neighbors.hidden_edges }} 条边的证据属于其他部门，已隐藏内容，仅保留结构
              </div>
              <div v-for="(edge, i) in neighbors?.edges || []" :key="i" class="gedge">
                <span class="gpred">{{ edge.predicate }}</span>
                <span class="gdir">{{ edge.direction === 'out' ? '→' : '←' }}</span>
                <span class="gother">{{ edge.other_name || edge.value || '—' }}</span>
                <span class="gconf">置信 {{ (edge.confidence * 100).toFixed(0) }}%</span>
                <div v-if="edge.evidence" class="gevidence">{{ edge.evidence }}</div>
                <div v-else class="gevidence muted">证据无权查看（chunk {{ edge.chunk_id }}）</div>
              </div>
              <div v-if="!neighbors?.edges?.length" class="empty">该实体还没有关系</div>
            </div>

            <div class="gops">
              <span class="gmeta">合并：把「{{ selected.name }}」并入左侧选中的实体</span>
              <button class="btn btn-sm btn-outline" @click="doMerge(selected.id)">合并</button>
              <button
                class="btn btn-sm btn-outline"
                v-for="alias in selected.aliases"
                :key="alias"
                @click="doSplit(alias)"
              >拆出「{{ alias }}」</button>
            </div>
          </template>
        </div>
      </div>

      <div class="glink">
        <div class="glist-head">链接预览（调试）</div>
        <div class="hint">输入一个问题，看会链接到哪些实体、扩出哪些 chunk。这是排查"图为什么没召回"的入口。</div>
        <div class="gtools">
          <input v-model="linkQuery" class="ginput" placeholder="例如：年假有几天？" @keyup.enter="runLink" />
          <button class="btn btn-sm btn-outline" @click="runLink">试算</button>
        </div>
        <template v-if="linkResult">
          <div class="gmeta">命中实体：{{ linkResult.entities.map((e) => `${e.name}(${e.via})`).join('、') || '无' }}</div>
          <div class="gmeta">扩展 chunk：{{ linkResult.chunks.length }} 条</div>
          <div v-for="chunk in linkResult.chunks" :key="chunk.chunk_id" class="gchunk">
            <span class="gconf">hop {{ chunk.hop }}</span>
            <span class="gmeta">{{ chunk.department }}</span>
            <span>{{ chunk.preview }}</span>
          </div>
        </template>
      </div>

      <div v-if="notice" class="gnotice">{{ notice }}</div>
      <div v-if="error" class="err-text">{{ error }}</div>
    </div>
  </section>
</template>

<style scoped>
.gstats { display: flex; flex-wrap: wrap; gap: 10px; align-items: center; margin: 12px 0 4px; }
.gstat { font-size: 13px; color: var(--muted, #6B6A63); }
.gstat b { color: var(--text, #2C2C2A); font-weight: 500; font-size: 15px; }
.gstat.on { color: #3B6D11; }
.gstat.off { color: #993C1D; }
.gtype { font-size: 12px; border: 1px solid; border-radius: 999px; padding: 1px 8px; color: var(--muted, #6B6A63); }
.gtools { display: flex; gap: 8px; margin: 12px 0; flex-wrap: wrap; }
.ginput { flex: 1; min-width: 180px; padding: 7px 10px; border: 1px solid #D3D1C7; border-radius: 8px; font-size: 13px; background: #fff; }
.gselect { flex: 0 0 130px; }
.ggrid { display: grid; grid-template-columns: 260px 1fr; gap: 16px; align-items: start; }
.glist { border: 1px solid #E4E2DA; border-radius: 10px; max-height: 520px; overflow: auto; }
.glist-head { font-size: 12px; color: var(--muted, #6B6A63); padding: 8px 10px; border-bottom: 1px solid #E4E2DA; background: #FAFAF7; }
.gentity { display: flex; align-items: center; gap: 8px; padding: 7px 10px; cursor: pointer; border-bottom: 1px solid #F1EFE8; }
.gentity:hover { background: #FAFAF7; }
.gentity.active { background: #EEEDFE; }
.gdot { width: 7px; height: 7px; border-radius: 50%; flex: 0 0 auto; }
.gname { font-size: 13px; color: var(--text, #2C2C2A); flex: 1; }
.gmeta { font-size: 11px; color: var(--muted, #6B6A63); }
.gcanvas { border: 1px solid #E4E2DA; border-radius: 10px; padding: 12px; }
.gcenter-title { font-size: 14px; font-weight: 500; margin-bottom: 6px; display: flex; gap: 8px; align-items: baseline; }
.gsvg { width: 100%; max-height: 320px; }
.gedges { margin-top: 8px; max-height: 220px; overflow: auto; }
.gedge { padding: 6px 0; border-bottom: 1px dashed #F1EFE8; font-size: 12px; }
.gpred { font-weight: 500; margin-right: 6px; }
.gdir { color: var(--muted, #6B6A63); margin-right: 6px; }
.gother { margin-right: 8px; }
.gconf { font-size: 11px; color: var(--muted, #6B6A63); }
.gevidence { font-size: 12px; color: #5F5E5A; margin-top: 3px; line-height: 1.5; }
.gevidence.muted { color: #A32D2D; }
.ghidden { font-size: 12px; color: #A32D2D; padding: 6px 0; }
.gops { display: flex; gap: 8px; align-items: center; margin-top: 10px; flex-wrap: wrap; }
.glink { margin-top: 20px; border-top: 1px solid #E4E2DA; padding-top: 12px; }
.gchunk { font-size: 12px; padding: 4px 0; border-bottom: 1px dashed #F1EFE8; display: flex; gap: 8px; }
.gnotice { margin-top: 10px; font-size: 13px; color: #3B6D11; }
@media (max-width: 760px) {
  .ggrid { grid-template-columns: 1fr; }
}
</style>
