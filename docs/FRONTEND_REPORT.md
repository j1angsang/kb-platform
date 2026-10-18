# Vue 3 前端改造报告

## 1. 为什么改

原来 `static/index.html` 是落地页，`static/app.html` 是一个 1300 多行的单文件工作台：HTML、CSS、JS 全在一起，DOM 用 `getElementById` 和字符串模板更新。功能能跑，但：

- 状态分散在全局变量和 DOM 上，改一个视图容易影响另一个；
- 上传、聊天、文档列表的加载/错误/分页逻辑重复；
- 没有类型约束，接口字段改名只能运行时发现；
- 面试里“前端”只能讲原生 JS，讲不了组件化和构建链。

目标不是重做视觉，而是**保留现有 CSS/字体/布局，把工作台迁到 Vue 3 + Vite + TypeScript**。

## 2. 结构

```text
frontend/
  index.html
  vite.config.ts
  src/
    main.ts
    App.vue
    api.ts
    components/LoginPanel.vue
    views/DocsView.vue
    views/ReviewView.vue
    views/ChatView.vue
    views/FilesView.vue
    styles/app.css
```

构建产物写入 `static/app/`，FastAPI 通过 `/app` 和静态目录提供访问；旧 `/app.html` 保留回退。

## 3. 页面与功能

### 登录

- `/auth/login` 表单登录；
- token 和用户信息继续用 `kb_token` / `kb_user`，与旧版共用登录态；
- 401 统一清理登录态并回到登录页。

### 知识文档

- 文档列表、状态筛选、分页；
- 单文件上传后自动提交审核；
- 驳回文档可由提交人重新提交；
- 状态/入库状态用原有 badge 样式。

### 审核

- 仅领导/管理员显示“审核”tab；
- 待审列表、通过/驳回，驳回理由必填；
- 审核接口复用 `/knowledge/{id}/approve|reject`。

### 智能问答

- 会话列表、历史消息、新建/删除会话；
- `/chat/ask` 发送问题，保留最近 20 条上下文；
- 回答里的来源文件以 chip 展示，点击下载；
- 数据源卡片显示“员工统计”等标签。

### 文件管理

- 文件列表、分页、部门筛选（领导/管理员）；
- 单文件上传、下载、删除；
- 文件统计条。

## 4. 样式复用

`frontend/src/styles/app.css` 直接抽取自旧 `app.html` 的 `<style>`：

- 同一套 CSS 变量（`--foreground`、`--border`、`--green` 等）；
- 同一套字体（Instrument Sans / Instrument Serif / JetBrains Mono）；
- 同一套 `.nav`、`.tabs`、`.card`、`.khead/.krow`、`.chat-*`、`.dropzone` 类名；
- 只补了登录卡片和移动端少量响应式规则。

因此视觉不是重设计，而是原界面的组件化。

## 5. 构建与接入

```bash
cd frontend
pnpm install
pnpm build
```

- Vite `base=/app/`；
- `outDir=../static/app`；
- FastAPI 增加 `GET /app` 返回 `static/app/index.html`；
- `/app/` 由现有 `StaticFiles` 提供；
- 构建产物随仓库提交，没装 Node 也能直接跑；改源码后需要重新 build。

## 6. 验证

- `vue-tsc --noEmit` 通过；
- `vite build` 通过，输出 `static/app/index.html`、CSS、JS；
- `GET /app/`、`/app`、`/app/assets/*.css`、`/vendor/fonts.css` 均 200；
- 用 Edge headless 截图验证登录页、文档页、问答页、文件页；
- 文档页实际拉到真实文档数据，分页和 badge 正常；
- 问答页会话列表、文件页列表均正常渲染；
- `/health/ready` 仍为 `ok`。

## 7. 边界

- Vue 版上传走单文件 `/files/upload`；旧版的 5MB 分片、断点续传、秒传、共享弹窗仍在 `/app.html`；
- 没有引入 Vue Router，用 `?view=` 管理 tab，减少依赖；
- 没有做 SSR；
- 审核/共享等低频操作保持简单 prompt，不做复杂弹窗；
- 旧版 `app.html` 保留，方便回退。

## 8. 简历写法

> 把原有单文件工作台迁移到 Vue 3 + Vite + TypeScript，复用原 CSS 设计令牌和组件类名，拆出登录、文档、审核、问答、文件五个视图；用统一 API 层处理 JWT、401、分页和错误，构建产物由 FastAPI 静态托管。

## 9. 面试追问

**Q1：为什么保留旧 CSS？**
迁移目标是工程结构，不是换视觉。直接复用变量、字体和类名，能把风险控制在组件和状态层，也保证用户无感。

**Q2：为什么不用 Vue Router？**
工作台只有四个 tab，`?view=` 足够，少一个依赖；如果要做深链接/嵌套路由再引入。

**Q3：API 层做了什么？**
统一注入 Bearer token、处理 401、解析错误体、封装分页/上传/下载/聊天；组件不直接拼 headers。

**Q4：Vue 版和旧版差异？**
Vue 版覆盖核心工作流；分片续传、秒传、共享弹窗仍在旧版，报告里明确写成边界，不假装全量迁移。

**Q5：构建产物为什么提交？**
部署环境不一定装 Node；提交 `static/app` 保证直接可跑，源码和 lockfile 保证可重建。
