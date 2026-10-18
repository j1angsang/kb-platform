# 对象存储代码讲解

## 1. 文件地图

| 文件 | 作用 |
|---|---|
| `services/object_storage.py` | local/S3 适配层，MinIO SDK，上传/回源/删除/健康检查 |
| `services/storage.py` | 本地路径、派生目录、删除本地 + 对象 |
| `routers/files.py` | 上传、分片合并、下载接对象存储 |
| `rag/indexer.py` | 入库前确保本地有文件 |
| `agents/review_graph.py` | 审核预检时确保文件可读 |
| `agents/graph.py` | 图片问答读取页图/原图前确保本地存在 |
| `seed_enterprise_data.py` | 种子文件写入本地后同步 S3 |
| `main.py` + `routers/health.py` | 启动检查与 readiness |
| `scripts/backfill_object_storage.py` | 历史文件回填 |
| `docker-compose.yml` / `Dockerfile` | PostgreSQL + Redis + MinIO + App |

## 2. `services/object_storage.py`

### 2.1 配置

模块导入时读取环境变量：

- `STORAGE_BACKEND`：只允许 `local` / `s3`，非法值直接抛错；
- `S3_ENDPOINT`：去掉 `http://` / `https://`，MinIO SDK 只接受 `host:port`；
- `S3_SECURE`：显式配置或 endpoint 是 https；
- `S3_KEY_PREFIX`：默认 `kb`，最终 key 是 `kb/<stored_path>`；
- `S3_AUTO_CREATE_BUCKET`：本地 MinIO 方便，生产建议关闭；
- s3 模式缺 AK/SK/bucket 时，启动阶段直接 `RuntimeError`，不把错误拖到第一次上传。

### 2.2 关键方法

```python
class ObjectStorage:
    @property
    def enabled(self) -> bool: ...
    def _key(self, stored_path: str) -> str: ...
    def _ensure_bucket(self) -> None: ...
    def upload(self, stored_path, local_path) -> None: ...
    def ensure_local(self, stored_path) -> Path: ...
    def delete(self, stored_path) -> None: ...
    def healthcheck(self) -> str: ...
```

**`_key`**

```python
name = Path(stored_path).name
if not name or name in (".", "..") or name != stored_path:
    raise ValueError(...)
return f"{S3_KEY_PREFIX}/{name}" if S3_KEY_PREFIX else name
```

只接受单层文件名，拒绝 `../x`、绝对路径、空名。数据库里的 `stored_path` 是服务端生成的 uuid + 后缀，正常不会命中异常分支；这里仍然做防御，是因为回填脚本和运维脚本也会传这个值。

**`upload`**

```python
if not self.enabled:
    return
self._ensure_bucket()
self._get_client().fput_object(bucket, key, str(local_path), ...)
```

local 模式是 no-op，保证老部署不需要改任何调用。S3 模式首次上传时懒建 bucket；客户端也是懒加载，避免导入模块就建连接。

**`ensure_local`**

```python
path = _local_path(stored_path)
if path.exists() and path.stat().st_size > 0:
    return path
if not self.enabled:
    raise FileNotFoundError(...)
self._ensure_bucket()
path.parent.mkdir(...)
self._get_client().fget_object(bucket, key, str(path))
return path
```

先看本地缓存，零字节文件也当作缺失。回源失败统一转成 `FileNotFoundError`，上层下载返回 404、入库标记 failed，不会把 MinIO 的 `S3Error` 泄漏给用户。

**`delete`**

```python
try:
    self._get_client().remove_object(bucket, key)
except S3Error as exc:
    if exc.code not in ("NoSuchKey", "NoSuchBucket"):
        raise
```

对象不存在视为幂等成功；其他错误往上抛，让硬清理任务保留数据库行并重试。

### 2.3 异步包装

MinIO SDK 是同步的。路由和索引器是 async，所以提供：

```python
async def upload_async(...):
    await asyncio.to_thread(self.upload, ...)

async def ensure_local_async(...):
    return await asyncio.to_thread(self.ensure_local, ...)
```

避免在事件循环里直接做网络/磁盘 IO。`healthcheck` 也通过 `asyncio.to_thread` 调用。

## 3. 上传链路

### 3.1 单文件 `routers/files.py::upload_file`

```python
record, inserted = await file_crud.create_file_atomic(...)
if inserted:
    await object_storage.upload_async(stored_name, dest)
    await db.commit()
```

顺序是“先 flush 拿 file_id → 同步对象 → commit”。对象上传失败：

```python
await db.rollback()
dest.unlink(missing_ok=True)
raise HTTPException(500, "对象存储写入失败")
```

这样不会留下“数据库有记录、对象没有”的半成功。

秒传命中（`inserted=False`）时，数据库里已经有旧的 `stored_path`。代码会：

- 旧文件在本地：删掉这次上传的临时文件；
- 旧文件不在本地：把临时文件挪到旧路径；
- 对旧 `stored_path` 再同步一次对象存储，修复历史文件只有本地的情况。

### 3.2 分片合并 `complete_upload`

合并、md5 校验、大小校验通过后，先 `create_file_atomic`，再决定上传哪个 `stored_path`：

- 新文件：上传新 `stored_name`；
- 秒传：上传已有 `existing.stored_path`；
- 上传失败：rollback，保留分片和 upload task，用户可以重试合并；
- 上传成功后才删分片、删 task、commit。

这个顺序是为了避免“对象没传上去，分片先被删了，用户无法重试”。

## 4. 下载链路

```python
try:
    path = await object_storage.ensure_local_async(record.stored_path)
except FileNotFoundError:
    raise HTTPException(404, "文件已丢失...")
```

权限校验在回源之前，避免无权限用户通过下载接口触发对象读取。`FileResponse` 仍用数据库里的文件名和 content-type。

## 5. 入库与审核

### `rag/indexer.py`

```python
path = await object_storage.ensure_local_async(file.stored_path)
chunks = parse_file(path)
```

`_index_document_inner` 的异常会走原来的失败落库逻辑，`error_message` 记录“对象存储回源失败”之类信息；`_reindex_document_inner` 在回源失败时直接跳过并记 warning，不把旧索引删掉。

### `agents/review_graph.py`

审核预检原来只看本地 `STORAGE_DIR / stored_path`，现在调用 `ensure_local_async`：本地没有就从对象存储回源；回源失败才把“磁盘实体丢失”加入 notes。这样换机器后审核不会误判。

### `agents/graph.py`

图片问答读取页图前检查本地：

```python
if not abs_path.exists():
    abs_path = await object_storage.ensure_local_async(rel_path)
```

原图（用户上传的图片）可以从 S3 回源；PDF 页图是派生文件，不在 S3，回源失败就跳过该图并记 warning。

## 6. 删除

`services/storage.py::delete_storage_artifacts`：

1. 校验目标严格在 `STORAGE_DIR` 内；
2. 删原文件、删 `<stem>_pages` 派生目录；
3. 调 `object_storage.delete(name)`。

对象删除失败会抛错，`cleanup_soft_deleted_files` 捕获后 `continue`，不删数据库行，下一轮重试。软删除本身不动对象，保留误删恢复窗口。

## 7. 启动与健康检查

`main.py`：

```python
if object_storage.enabled:
    for attempt in range(1, 11):
        try:
            await asyncio.to_thread(object_storage.healthcheck)
            break
        except Exception:
            await asyncio.sleep(2)
```

不因为 MinIO 晚启动就让整个应用起不来；但会明确记 error。

`routers/health.py` 的 `/health/ready` 增加 `object_storage` 检查，S3 不可用时整体返回 503。

## 8. 回填脚本

`scripts/backfill_object_storage.py`：

- 查全部 `FileInfo`（包含软删除，保留期内对象也要在）；
- 本地文件存在且非空才上传；
- `--dry-run` 只打印 would_upload；
- 缺失文件单独计数，不伪造成功；
- 有失败返回非 0，方便 CI/运维判断。

## 9. 本地验证

### 9.1 适配层

```bash
set STORAGE_BACKEND=s3
set S3_ENDPOINT=127.0.0.1:9000
set S3_ACCESS_KEY=<minio-access-key>
set S3_SECRET_KEY=<minio-secret-key>
set S3_BUCKET=kb-files
set S3_REGION=us-east-1
python -c "from services.object_storage import object_storage; print(object_storage.healthcheck())"
```

本次用 S3 兼容服务验证，没有把 moto 加进项目依赖。

### 9.2 接口

1. 登录拿 token；
2. `POST /files/upload`；
3. 用 MinIO SDK 列出 `kb/<stored_path>`；
4. `GET /files/{id}/download`；
5. 删本地文件，再下载一次，确认回源；
6. 上传 md → 删本地 → `POST /knowledge/submit` → 查 `knowledge_docs.index_status=ready`。

### 9.3 Compose

```bash
docker compose config --quiet
docker compose up -d --build
```

`depends_on` 里 MinIO 用 `service_started`，应用自己重试 S3；Postgres/Redis 用 healthcheck。

## 10. 已知边界

- 分片临时目录仍在本机，多实例要共享；
- PDF 页图、OCR 中间产物是本地缓存，丢了可以重新解析；
- 没有做多实例合并的分布式锁；
- 当前环境 Docker 拉不到 MinIO 镜像，Compose 只做了配置校验；MinIO 社区二进制已归档，AIStor 二进制需要 license。S3 协议层用兼容服务验证。

