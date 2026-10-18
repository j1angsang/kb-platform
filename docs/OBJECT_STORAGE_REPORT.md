# 对象存储改造报告（MinIO / S3）

## 1. 为什么改

原来的文件实体只存在本机 `storage/` 目录：数据库里是 `stored_path`，下载、解析、重排后的页图都直接拼本地路径。单机演示没问题，但有几个明显边界：

- 容器重建、换机器、磁盘故障，原始文件就丢了；
- 文件服务无法独立扩容，应用和文件绑死在一台机器；
- 面试里“文件存储”只能讲到分片和秒传，讲不到持久层与回源。

改造目标不是把本地磁盘全部删掉，而是把**原始文件**放到对象存储，本地磁盘降级为解析/OCR/图片渲染的工作缓存。这样解析链路不用重写，对象存储成为持久层。

## 2. 方案对比

| 方案 | 做法 | 问题 |
|---|---|---|
| 全量替换 | 所有读写都直接走 S3，本地不落盘 | 解析器、OCR、页图都按本地路径写，改动面大；每次解析都要走网络 |
| 只做备份 | 上传后异步复制一份到 S3 | 下载/入库仍依赖本地，本地丢了不能自动恢复 |
| **写穿 + 回源（采用）** | 上传/合并成功后同步到 S3；本地保留缓存；本地缺失时从 S3 拉回 | 多一次上传；派生文件仍是本地缓存 |

选择写穿 + 回源的原因：不改解析器的文件接口，故障时能回源，代码量可控，边界也能讲清楚。

## 3. 数据流

### 上传

```text
POST /files/upload
  -> save_upload_file 写本地 storage/<stored_path>
  -> create_file_atomic 拿 file_id / 判断是否秒传
  -> object_storage.upload(stored_path, local_path)
  -> DB commit
```

分片上传在 `/files/upload/complete` 合并完成后做同样的事；秒传命中已有文件时，会给已有 `stored_path` 补一次对象存储同步，避免历史文件只有本地。

### 下载

```text
GET /files/{id}/download
  -> 权限校验
  -> object_storage.ensure_local(stored_path)
       本地有 -> 直接返回
       本地没有 -> S3 fget_object 回源到本地缓存
  -> FileResponse
```

### 入库

```text
index_document / reindex_document
  -> object_storage.ensure_local(stored_path)
  -> parse_file(local_path)
  -> 切分 + embedding + upsert
```

本地文件被清理后，只要对象存储里有原始文件，重新入库仍然能跑。

### 删除

- 软删除：只改 `deleted_at`，对象保留，避免误删无法恢复；
- 硬清理：`delete_storage_artifacts` 先删本地原文件和页图目录，再删对象存储对象；对象删除失败会抛错，调用方不删数据库行，下一轮重试。

## 4. 配置

```dotenv
STORAGE_BACKEND=local          # local | s3
FILE_STORAGE_DIR=storage       # 本地工作缓存
S3_ENDPOINT=127.0.0.1:9000
S3_ACCESS_KEY=
S3_SECRET_KEY=
S3_BUCKET=kb-files
S3_SECURE=false
S3_KEY_PREFIX=kb
S3_AUTO_CREATE_BUCKET=true
S3_REGION=
```

- `local` 下所有 S3 方法都是 no-op，原有单机行为不变；
- `s3` 下缺 endpoint/ak/sk/bucket 会在启动时直接报错，不会带病运行；
- 对象 key 是 `<prefix>/<stored_path>`，`stored_path` 只允许单层文件名，禁止路径穿越；
- 本地 MinIO 首次使用自动建桶；生产可关掉自动建桶，预建后收紧权限。

## 5. 迁移

历史数据用 `scripts/backfill_object_storage.py`：

```bash
.venv\Scripts\python scripts\backfill_object_storage.py --dry-run
.venv\Scripts\python scripts\backfill_object_storage.py
```

脚本只上传本地仍存在的文件；本地已经丢失的历史文件不会凭空恢复。本次环境里 51 条历史记录中有 50 条本地实体已缺失，脚本如实报 `missing`，没有伪造成功。

## 6. Docker Compose

`docker-compose.yml` 起四个服务：

- `postgres`：`pgvector/pgvector:pg18`，初始化脚本建 `vector` / `citext` 扩展；
- `redis`：限流/缓存备用；
- `minio`：9000 API、9001 控制台，数据卷持久化；
- `app`：构建本仓库镜像，`STORAGE_BACKEND=s3`，启动前跑 `alembic upgrade head`。

```bash
cp .env.example .env
# 填 LLM / embedding 必填项
docker compose up -d --build
```

`app` 启动时会对 S3 做最多 10 次、每次间隔 2 秒的健康检查；MinIO 比应用晚就绪不会直接拖死启动，但 `/health/ready` 会返回 `503`，上传会失败直到对象存储恢复。

## 7. 验证记录

本机 Docker 的镜像拉取被网络/代理拦截；MinIO 官方社区二进制已归档不再提供，新的 AIStor 二进制约运行时提示需要 license。因此用 S3 兼容服务（moto）跑协议级验证，代码仍使用 MinIO SDK。Compose 配置用 `docker compose config --quiet` 校验通过。

| 验证项 | 结果 |
|---|---|
| 适配层上传 + healthcheck | 通过，`backend=s3`，bucket 可用 |
| 删除本地缓存后 `ensure_local` | 通过，内容从 S3 回源 |
| 删除对象 + 删除本地后回源 | 正确抛 `FileNotFoundError` |
| API `POST /files/upload` | 201，对象 `kb/<stored_path>` 存在 |
| API 分片上传 init/chunk/complete | 20 片全部 200，合并后对象存在，下载内容一致 |
| API 下载 | 200，内容一致 |
| 删除本地文件后再次下载 | 200，自动回源，本地缓存恢复 |
| 上传 md 后删本地，再提交入库 | 文档 `index_status=ready`，`chunk_count=1` |
| 回填脚本 dry-run / 实跑 | 正常；本次环境 1 条有本地实体，上传成功；50 条 missing 被列出 |
| `docker compose config --quiet` | 通过 |

未完成的环境项：MinIO 容器实跑（Docker daemon 直连 Docker Hub 超时，宿主代理 7890 没有被 daemon 继承；社区二进制已归档，AIStor 二进制需要 license）。这是本机环境/上游分发变化，不是适配层代码问题；Compose 和 SDK 路径都已就绪。

## 8. 边界与后续

- 分片上传的临时分片仍在本地 `storage/chunks`，多实例下需要共享分片存储；
- PDF 页图等派生文件没有上传 S3，属于可重建缓存；本地清空后需要重新解析；
- 当前按单实例设计，没有做多实例并发合并的分布式锁；
- 软删除对象保留 30 天，硬清理任务负责最终删除；
- 生产建议：预建 bucket、最小权限 AK/SK、服务端加密、生命周期规则、跨实例共享缓存或直接把派生文件也对象化。

## 9. 简历写法

工作流版：

> 参与内部知识库文件存储改造，把原始文件从单机磁盘迁到 MinIO/S3，本地保留解析缓存；上传/合并后同步对象，下载和入库在本地缺失时自动回源，并用迁移脚本回填历史文件。

技术版：

> 设计 local/S3 双后端存储适配，写穿上传 + 本地缓存回源；对象 key 做前缀隔离和路径校验，删除采用软删 + 硬清理，补充回填脚本与 Compose 编排。

## 10. 面试追问

**Q1：为什么不全量走 S3？**
解析器、OCR、页图都按本地路径写，全量替换要改所有文件接口，而且每次解析都走网络。写穿 + 回源保留了本地工作缓存，故障时能回源，改动面更小。

**Q2：上传到 S3 失败怎么办？**
新文件上传会在 DB commit 前同步对象；失败就 rollback、删本地临时文件并返回 500，不会出现“数据库有记录、对象没有”的半成功。秒传命中已有文件时，会给已有 `stored_path` 补同步。

**Q3：本地缓存和对象不一致怎么处理？**
读取以本地缓存优先；本地缺失才回源。删除先删本地再删对象，对象删除失败会让硬清理重试，不删数据库行。

**Q4：对象 key 怎么防路径穿越？**
`stored_path` 必须是单层文件名，`Path(name).name == stored_path`，再加固定前缀；不接受用户原始文件名直接拼 key。

**Q5：多实例怎么办？**
当前分片和派生文件是本地缓存，多实例需要共享分片存储/对象化页图，并给合并加分布式锁；这是已知边界，不假装已支持。

**Q6：为什么软删除不立刻删对象？**
误删可恢复；硬清理任务按保留期统一删本地和对象，避免软删除期间对象被提前清掉。

