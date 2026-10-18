# Knowledge Base Platform

企业知识库平台：自研文件存储底座 + 知识文档审核流 + 多模态混合检索 + 对话式企业问答。

技术栈：FastAPI + SQLAlchemy async + PostgreSQL 18 + pgvector + JWT + LangChain/LangGraph。
支持分片上传/断点续传/秒传、下载防盗链、知识文档审核流、后台向量入库、向量 + BM25 混合检索、
基于 Agent 的企业问答。

## 功能概览

- **文件存储**：分片上传、断点续传、秒传、下载防盗链、孤儿分片清理。
- **知识库**：文档上传 → 解析 PDF/Word/Excel/图片 → 结构感知切分 → 审核 → 向量入库。
- **检索**：向量 + BM25 倒数排名融合（RRF），相对掉档截断，可选 cross-encoder 重排与相关性门控；`SEARCH_FUSION=weighted` 可切回原加权融合。
- **对话问答**：LangChain `create_agent` 编排，检索增强 + 工具调用 + 多模态图片通道；检索内容按不可信数据处理。
- **记忆**：短期会话用 LangGraph checkpointer，长期记忆是用户偏好，两者都按用户隔离。
- **权限**：部门维度集中校验，员工只读本部门 + 共享，领导/管理员可跨部门。

## 快速开始

### 1. 环境要求

- Python 3.13
- PostgreSQL 18 + pgvector，本机 Docker 可用 `pgvector/pgvector:pg18`
- uv，依赖清单见 `pyproject.toml` 与 `uv.lock`

### 2. 依赖安装

```bash
uv sync
```

### 3. 配置

复制 `.env.example` 为 `.env` 并填写。主要变量：

| 变量 | 必填 | 说明 |
|---|---|---|
| `DB_HOST` / `DB_PORT` / `DB_USER` / `DB_PASSWORD` | 是 | PostgreSQL 连接 |
| `DB_NAME` | 否 | 默认 `file_storage_db` |
| `SECRET_KEY` | 是 | JWT 签名密钥 |
| `LLM_MODEL` / `LLM_BASE_URL` / `LLM_API_KEY` | 是 | 对话问答使用 |
| `EMBEDDING_MODEL` / `EMBEDDING_API_KEY` | 是 | 向量入库/检索使用 |
| `EMBEDDING_DIM` | 是 | 与 embedding 模型实际输出维度一致，pgvector 硬约束 |
| `LLM_SUPPORTS_VISION` | 否 | 纯文本模型须显式设为 `false`，否则启动报错 |
| `STORAGE_BACKEND` | 否 | `local`（默认）或 `s3`；s3 还需配 `S3_*` |
| `S3_ENDPOINT` / `S3_ACCESS_KEY` / `S3_SECRET_KEY` / `S3_BUCKET` | s3 时是 | MinIO/S3 连接信息，见 `.env.example` |

完整变量与默认值见 `.env.example`。启动时 `config/settings.py` 一次性校验必填项，缺项会一次列全。

生成随机密钥：

```bash
python -c "import secrets; print(secrets.token_hex(32))"
```

### 4. 建库与迁移

```bash
# 启动 PostgreSQL，数据卷持久化
docker run -d --name pg-local -e POSTGRES_PASSWORD=postgres -p 5432:5432 \
  -v pg_local_data:/var/lib/postgresql pgvector/pgvector:pg18

# 建库 + 扩展
docker exec -it pg-local psql -U postgres -c "CREATE DATABASE file_storage_db;"
docker exec -it pg-local psql -U postgres -d file_storage_db \
  -c "CREATE EXTENSION IF NOT EXISTS vector; CREATE EXTENSION IF NOT EXISTS citext;"

# schema 由 Alembic 管理
.venv\Scripts\alembic.exe upgrade head
```

### 5. 灌种子数据

```bash
.venv\Scripts\python seed_employees.py
.venv\Scripts\python seed_enterprise_data.py
```

### 6. 启动

```bash
.venv\Scripts\alembic.exe upgrade head
.venv\Scripts\python -m uvicorn main:app --reload --port 8005
```

Windows 下可直接双击 `start.bat`，已内置迁移 + 启动。浏览器打开 `http://127.0.0.1:8005/`。

### 7. 冒烟验证

```bash
# 存活 + 依赖就绪
curl -fsS http://127.0.0.1:8005/health
curl -fsS http://127.0.0.1:8005/health/ready

# 注册并登录
curl -s -X POST http://127.0.0.1:8005/auth/register \
  -F username=smoke_user -F password=123456 -F department=技术部
curl -s -X POST http://127.0.0.1:8005/auth/login \
  -F username=smoke_user -F password=123456

# 上传文件，把 <TOKEN> 换成上一步 access_token
echo "hello kb" > smoke.txt
curl -s -X POST http://127.0.0.1:8005/files/upload \
  -H "Authorization: Bearer <TOKEN>" \
  -F "file=@smoke.txt;type=text/plain"
```

## 关键设计

- **配置 fail-fast**：`DB_PASSWORD` / `SECRET_KEY` 缺失直接启动失败，不允许带病启动。
- **写后立读**：写接口在响应前显式 `commit()`，避免下一个请求读到旧值。
- **后台入库可见**：BackgroundTasks 日志带 doc_id / file_id / chunk / 耗时 / request_id，失败落 `failed` + 堆栈。
- **文档锁**：`rag/indexer.py` 用 `WeakValueDictionary[int, asyncio.Lock]` 按 doc_id 互斥，避免重复入库。
- **结构感知切分**：`rag/parser.py` 先按标题分节，长段按句子切，表格行尽量不拆；标题路径写入 chunk 前缀，`CHUNK_SIZE` / `CHUNK_OVERLAP` 可配。
- **Prompt injection 分层防护**：检索内容包进 `<knowledge><document>` 并标为不可信，规则命中只告警不拦截；真正的边界仍是权限过滤与只读工具。
- **相对掉档截断**：检索保留 `SEARCH_MIN_SCORE=0.80`、`SEARCH_MAX_DROP=0.015`；RRF 模式下这两个阈值作用在 `_relevance_score`（沿用原加权分）上，不是 RRF 分数——排序与置信度分开。
- **上传硬上限**：流式写盘阶段即拦截，超限先删半截文件，防止磁盘打爆。
- **权限集中化**：`services/permissions.py` 统一部门维度校验，不散落在路由里。
- **限流**：登录/注册、下载、`/chat/ask`、`/knowledge/search` 均有限流，429 带 Retry-After；单进程内存版，多 worker 可切 Redis。
- **安全响应头**：`nosniff` / `DENY` / `no-referrer`。
- **可观测性**：`request_id` 串联 HTTP → Agent → 工具 → 后台任务。

### 检索融合：RRF（默认）

`SEARCH_FUSION=rrf` 时，文本候选按两路名次做倒数排名融合：

`rrf(d) = 0.6 / (60 + rank_vector) + 0.4 / (60 + rank_bm25)`

- **RRF 只负责排序**：输出 `score` 归一化到 0~1，仅用于展示；不要拿它套按余弦分标定的阈值；
- **门控仍用旧阈值**：`SEARCH_MIN_SCORE` / `SEARCH_MAX_DROP` 作用在 `_relevance_score`（沿用原加权分）上，避免把 0.80/0.015 硬套到 0.01~0.03 量级的 RRF 分数；
- **重排优先**：开启 `RERANK_ENABLED` 时，`rerank_score` 仍是最终门控，RRF 只决定进入重排的候选顺序；
- **可回滚**：`SEARCH_FUSION=weighted` 恢复原加权融合，便于 A/B 与线上回退。

## 可选能力

以下能力默认关闭，开启方式见 `.env.example`，关闭时行为与关闭前一致。

### 扫描件 OCR 文本通道：`PDF_OCR_TEXT_ENABLED`

无文字层的扫描件 PDF 默认只整页渲染为图片块走视觉通道，对"600 元""内部聚餐"这类字面查询
区分度不足。开启后，在保留图片块的同时额外用 RapidOCR onnxruntime CPU 版 OCR 出文本，作为
`type=text(source=ocr)` 块入库，与图片块并存、按 page 关联。

- **成本**：每页 embedding 调用由 1 次增至 2 次；CPU OCR 约 1~3s/页。
- **失败降级**：OCR 异常/超时/模型缺失 → WARNING 并跳过，图片块保留，绝不把文件标成 failed。
- **质量闸门**：去符号后不足 `PDF_OCR_TEXT_MIN_CHARS` 的噪声不入库；整页置信度低于阈值打标
  `source=ocr_lowconf`。

### 重排与相关性门控：`RERANK_ENABLED`

双塔 embedding 只能判"主题像不像"，相关与无关查询的分数区间常重叠，仅靠阈值容易漏拒答。
开启后，`store.search` 把融合序前 `RERANK_TOP_N` 条文本候选送 cross-encoder 重排，
分数写入独立字段 `rerank_score`，不覆盖 `score`，图片通道阈值与前端展示不受影响；
`select_relevant_hits` 用 `rerank_score >= RERANK_MIN_SCORE` 门控，低于阈值即视为无命中，
由 `rag_hook` 注入明确拒答指令。

- **`RERANK_TOP_N` 是常量不是旋钮**：`relevance_score` 是请求内相对分，候选数变化会抬高无关查询的
  分数分布，改它必须重新标定。
- **失败/超时**：退回原融合排序，无重试。

### Query 改写：`QUERY_REWRITE_ENABLED`

多轮指代如"那二类城市呢？"，或口语化提问，会让检索 query 信息量不足。开启后，`rag_hook`
在检索前用 LLM 把最新问题补全为自足的检索 query。

- **改写结果只用于检索**：不当答案、不进注入内容、不写进 sources。
- **fail-open**：超时/异常/输出异常一律回退原 query，检索不因改写失败而中断。

## 深入阅读

- [检索融合改造报告：Weighted → RRF](docs/RRF_CHANGE_REPORT.md)
- [RRF 代码逐行讲解](docs/RRF_CODE_WALKTHROUGH.md)
- [切分与 Prompt injection 防护](docs/CHUNKING_AND_INJECTION.md)
- [对象存储改造报告](docs/OBJECT_STORAGE_REPORT.md)
- [对象存储代码讲解](docs/OBJECT_STORAGE_CODE_WALKTHROUGH.md)
- [Agent 架构](agents/ARCHITECTURE.md)

## 评测

检索评测题集与脚本见 [eval/README.md](eval/README.md)，不调用 LLM：

```bash
.venv\Scripts\python.exe eval\run_retrieval.py --fusion both --rerank both
```

## 对象存储（MinIO/S3）

默认 `STORAGE_BACKEND=local`，本地文件就是唯一实体。切到 `s3` 后：

- 单文件上传、分片合并成功后，原始文件同步到对象存储；
- 本地磁盘保留为解析、OCR、图片渲染的工作缓存；
- 下载、后台入库时本地缺失，会自动从对象存储回源；
- 软删除不立刻删对象，硬清理时本地和对象一起删。

配置示例：

```dotenv
STORAGE_BACKEND=s3
S3_ENDPOINT=127.0.0.1:9000
S3_ACCESS_KEY=<minio-access-key>
S3_SECRET_KEY=<minio-secret-key>
S3_BUCKET=kb-files
S3_SECURE=false
S3_KEY_PREFIX=kb
S3_AUTO_CREATE_BUCKET=true
```

历史文件迁移（只在切换前本地文件还在时有效）：

```bash
.venv\Scripts\python scripts\backfill_object_storage.py --dry-run
.venv\Scripts\python scripts\backfill_object_storage.py
```

Docker Compose 一键起 PostgreSQL + Redis + MinIO + App：

```bash
cp .env.example .env
# 先填 LLM / embedding 等必填项
docker compose up -d --build
# MinIO 控制台 http://127.0.0.1:9001
```

边界：分片上传的临时分片、PDF 页图等派生文件仍落在本地缓存；本实现面向单实例，多实例部署需要把分片和派生文件也迁到共享存储。

## 测试

完整版使用 pytest + 独立测试库；本展示仓库为控制体积，未包含 `tests/`。

## 目录结构

| 路径 | 职责 |
|---|---|
| main.py | App 组装、startup、配置校验、健康检查路由 |
| config/ | DB / LLM / 日志 / 配置校验 / 限流 / 安全头 |
| routers/ | auth / files / knowledge / chat / health |
| services/ | 上传落盘、存储路径、权限集中化；`object_storage.py` 是 local/S3 适配 |
| scripts/ | 运维脚本，`backfill_object_storage.py` 回填历史文件到对象存储 |
| docker-compose.yml / Dockerfile | PostgreSQL + Redis + MinIO + App 一键启动 |
| rag/ | parser / embeddings / fusion / store / indexer / bm25 |
| docs/ | 检索融合、切分与注入防护、对象存储的改造报告和代码讲解 |
| eval/ | 检索评测题集、指标与可复跑脚本 |
| agents/ | create_agent + 钩子，企业问答编排，见 [agents/ARCHITECTURE.md](agents/ARCHITECTURE.md) |
| tools/ | 知识检索、文档列表、员工查询工具 |
| crud/ | 文件 / 知识库数据库操作 |
| models/ | SQLAlchemy ORM 模型 |
| schemas/ | Pydantic 请求/响应模型 |
| migrations/ | Alembic 迁移 |
| static/ | 文件存储前端 |
| seed_*.py | 一次性种子脚本 |

## 许可证

MIT
