# Vue 前端代码讲解

## 1. 文件地图

| 文件 | 作用 |
|---|---|
| `frontend/src/main.ts` | 创建 Vue 应用、加载样式 |
| `frontend/src/App.vue` | 登录态、导航、tab、视图切换 |
| `frontend/src/components/LoginPanel.vue` | 登录表单 |
| `frontend/src/api.ts` | 类型和 API 封装 |
| `frontend/src/views/DocsView.vue` | 文档列表/上传/重新提交 |
| `frontend/src/views/ReviewView.vue` | 审核队列/通过/驳回 |
| `frontend/src/views/ChatView.vue` | 会话/消息/问答/来源下载 |
| `frontend/src/views/FilesView.vue` | 文件列表/上传/下载/删除 |
| `frontend/src/styles/app.css` | 从旧 `app.html` 抽取的样式 |
| `frontend/vite.config.ts` | `base=/app/`，输出到 `static/app/` |

## 2. API 层

`api.ts` 统一：

- `getToken` / `getStoredUser` / `setAuth` / `clearAuth`；
- `api<T>` 注入 `Authorization: Bearer`，401 清 token 并派发 `kb:unauthorized`；
- 错误解析 `{detail}` 或 `{error.message}`；
- 分页请求 `getDocs` / `getFiles`；
- 上传 `uploadFile`（FormData）；
- 文档操作 `submitDoc` / `resubmitDoc` / `approveDoc` / `rejectDoc`；
- 下载 `downloadFile`（blob + a[download]）；
- 会话 `getSessions` / `getMessages` / `deleteSession`；
- 问答 `chatAsk`。

组件只调这些函数，不直接拼 URL/headers。

## 3. App.vue

```ts
const user = ref<User | null>(getToken() ? stored : null)
const activeTab = ref<Tab>(readTab())
const isManager = computed(() => (user.value?.role || 'employee') !== 'employee')
```

- 无 token 渲染 `LoginPanel`；
- 登录成功写入 `user`，进入 `search`；
- `setTab` 同步 `?view=`，刷新后保持当前 tab；
- 监听 `kb:unauthorized` 退出；
- `/health` 每 30 秒检查在线状态；
- `activeView` 映射到四个视图组件。

## 4. 各视图

### DocsView

- `onMounted(load)` 拉 `/knowledge/docs`；
- `page`/`status` 变化触发 load；
- 上传：`uploadFile(file)` 拿到 file_id，再 `submitDoc(file_id)`；
- 驳回文档：提交人本人显示“重新提交”；
- 列表/分页/badge 用旧 CSS 类。

### ReviewView

- `getDocs({status:'pending', scope:'reviewable'})`；
- `window.prompt` 收通过意见/驳回理由；
- 调 approve/reject 后重新加载。

### ChatView

- `getSessions` 渲染侧栏；
- `openSession` 拉历史消息；
- `send`：
  1. 取最近 20 条作为 `priorMessages`；
  2. push 用户消息；
  3. `chatAsk(text, priorMessages, sessionId)`；
  4. push assistant 消息和 sources；
  5. 服务端返回 `session_id` 后写入 URL；
- 来源 chip 点击 `downloadFile`；
- `nextTick` 后滚到底部。

### FilesView

- 分页/部门筛选；
- 上传、下载、删除；
- 统计条计算本页大小和今日新增。

## 5. 样式

`app.css` 是从 `static/app.html` 的 `<style>` 直接抽出来的，保留：

- CSS 变量、字体；
- `.nav`、`.tabs`、`.tab`、`.card`、`.grid2`、`.dropzone`；
- `.khead/.krow`、`.rhead/.rrow`、`.fhead/.frow`；
- `.chat-*`、`.source-chip`、`.badge`、`.pager`。

新增的只有 `.auth-*` 和移动端规则。

## 6. 构建

```bash
cd frontend
pnpm install
pnpm build
```

Vite：

```ts
base: '/app/'
build.outDir = '../static/app'
```

FastAPI：

```python
@app.get("/app", include_in_schema=False)
async def vue_workbench():
    return FileResponse("static/app/index.html")
```

`/app/` 由根 `StaticFiles` 提供，`/app/assets/*` 直接命中构建产物。

## 7. 验证方式

- `vue-tsc --noEmit`；
- `vite build`；
- `curl /app/` 和 assets；
- Edge headless 截图登录页、文档页、问答页、文件页；
- 文档页数据来自真实 `/knowledge/docs`，不是静态 mock。

## 8. 边界

- 单文件上传；分片/秒传/共享弹窗未迁；
- 无 Vue Router；
- 无 SSR；
- 旧 `/app.html` 保留回退。
