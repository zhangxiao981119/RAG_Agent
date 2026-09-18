# M3 · GET /api/me + acl/subjects.py 数据库接入

## Context（为什么做这件事）

M2 真 RAG 已完成，M3 登录侧也已完成（JWT/IP 限流/账户锁定/RSA 加密/多 worker 一致）。但 [deps.py L83-L84](file:///c:/Users/86186/Desktop/AI%20Agent%E5%AD%A6%E4%B9%A0/file_rag_agent/code/apps/api/app/api/deps.py#L83-L84) 当前仍是骨架：

```python
subjects=["public"],  # M3 暂无权限版本
authorized_kb_ids=[],  # M3 不限，chat 直接信任前端 kb_ids
```

而 [acl/subjects.py](file:///c:/Users/86186/Desktop/AI%20Agent%E5%AD%A6%E4%B9%A0/file_rag_agent/code/apps/api/app/services/acl/subjects.py) 仍是内存骨架版（dataclass + 纯函数），注释明写「M3/M4 换成 SQLAlchemy 模型」。

这是 M3 的任务 2 + 任务 3（手册 L1296-L1297），也是 M4 检索下推过滤的前置：没有真实的 `subjects` 和 `authorized_kb_ids`，M4 的 SQL 过滤条件无东西可下推。本次只完成**主体解析的 DB 接入 + GET /api/me 端点**，不触碰用户/部门管理界面（任务 4-8）和 path 级联重写（任务 8）。

## 范围边界

**本次做**：
- M3 任务 2：`GET /api/me` 返回 `subjects`（含 §3.2.3 双向展开）/ `clearance` / `dept_path` / `authorized_kb_ids`
- M3 任务 3：主体解析服务 + Redis 缓存（key 带 `tenant_acl_epoch`）

**本次不做**（留待 M3 后续或 M4）：
- `POST /api/auth/refresh` + 登出
- 用户/部门/角色管理界面（任务 4-7）
- 部门树 path 级联重写 + `tenant_acl_epoch += 1` 的触发点（任务 8）
- `kb_members` / `is_public` 变更时主动 `tenant_acl_epoch += 1`（M4 工作，但函数先写好）
- 检索 SQL 下推过滤（M4 工作）
- 单元测试（按用户偏好不写）

## 实现步骤

### 1. 数据库迁移 · Tenant 加 acl_epoch 字段

**文件**：`code/apps/api/alembic/versions/0003_add_tenant_acl_epoch.py`（新建）

**改动**：`tenants` 表加 `acl_epoch BIGINT NOT NULL DEFAULT 0`。

**理由**：§3.2.6 缓存 key `acl:{tenant_id}:{user_id}:{tenant_acl_epoch}`。任何权限变更都让 epoch +1，整个 tenant 的旧缓存自然失效（key 对不上）。比精确失效简单且不易写错。

**Model 同步**：[entities.py Tenant](file:///c:/Users/86186/Desktop/AI%20Agent%E5%AD%A6%E4%B9%A0/file_rag_agent/code/apps/api/app/models/entities.py#L28-L37) 加 `acl_epoch: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default=text("0"))`。

### 2. acl/subjects.py · DB 加载器 + 缓存

**文件**：[code/apps/api/app/services/acl/subjects.py](file:///c:/Users/86186/Desktop/AI%20Agent%E5%AD%A6%E4%B9%A0/file_rag_agent/code/apps/api/app/services/acl/subjects.py)（追加，不动现有纯函数）

**新增函数**：

```python
async def load_principal_from_db(
    session: AsyncSession,
    redis: Redis | None,
    user_id: uuid.UUID,
    tenant_id: uuid.UUID,
) -> Principal:
    """从 DB 解析 Principal，带 Redis 缓存（§3.2.6）。

    流程：
      1. 读 Tenant.acl_epoch
      2. 查 Redis key `acl:{tid}:{uid}:{epoch}`
         命中 → 反序列化为 Principal 返回
      3. 未命中 →
         a. 读 User（含 dept_id、clearance、role_names）
         b. 读该 tenant 全部 departments（path + visible_to_parent）→ Dict[str, DeptNode]
         c. 读 user_groups → group_ids
         d. role_names 已在 User 数组字段，直接用
         e. 读该 tenant 全部 knowledge_bases + kb_members → List[KnowledgeBaseRecord]
         f. 构造 UserRecord，调 build_principal（复用现有纯函数）
         g. 写回 Redis（TTL=ACL_CACHE_TTL_SECONDS=300s）
      4. Redis 异常 → 降级直连 DB，不抛（§3.2.6：「缓存不可用 MUST 降级直连 DB」）
    """
```

```python
async def invalidate_tenant_acl(redis, tenant_id: uuid.UUID) -> None:
    """使整个 tenant 的权限缓存失效。INCR tenant.acl_epoch。

    M4 在以下变更点调用（§3.2.6 表）：
      - kb_members 增删改
      - kb.visibility / is_public 变更
      - 用户增删改 / 换部门 / 换角色
      - user_group 成员增删改
      - 部门树结构变更（新增/移动/删除）
    本函数只负责 INCR + 写回 DB；调用方负责 commit。
    """
```

**关键设计点**：

- **复用现有纯函数**：`build_principal(user, departments, kbs)` 不变，DB 加载器只是把 SQLAlchemy 行映射成 `UserRecord` / `DeptNode` / `KnowledgeBaseRecord` 后喂进去。
- **dept 双向展开**：直接把 tenant 全部 departments 一次性加载到内存 `Dict[path, DeptNode]`。租户级组织树通常 < 200 节点，全量加载 + 原有 A1+A2 展开逻辑足够。§7.2 的 SQL 核验在 M4 写下推时再做。
- **kb_members 加载**：把每条 kb 的成员按 `subject_type:subject_id` 拼成元组塞进 `KnowledgeBaseRecord.members`，复用 `resolve_authorized_kb_ids` 的 `set(kb.members) & subjects` 逻辑。
- **缓存序列化**：Principal 的 `frozenset[str]` 序列化为 `sorted(list)`；`frozenset[uuid]` 序列化为 `sorted(list[str])`；反序列化时转回 frozenset。
- **失败回退**：`try/except redis Exception` → 走 DB 加载分支但不写回；日志 warning。

**导出**：`acl/__init__.py` 加 `load_principal_from_db`、`invalidate_tenant_acl`。

### 3. deps.py · get_current_user 用真实 Principal

**文件**：[code/apps/api/app/api/deps.py](file:///c:/Users/86186/Desktop/AI%20Agent%E5%AD%A6%E4%B9%A0/file_rag_agent/code/apps/api/app/api/deps.py)

**改动**：把 L83-L84 的硬编码替换为 `load_principal_from_db(session, redis, user.user_id, tenant_id)` 调用。Redis 客户端从 settings.redis_url 构造（与 auth.py 一致），在 finally 里 aclose。

**注意**：`CurrentUser.subjects` 字段类型当前是 `list[str]`，Principal 是 `frozenset[str]`。把 frozenset 转 list 存入 CurrentUser（保持 API 层兼容）。`authorized_kb_ids` 同理。

### 4. schemas/auth.py · 加 MeResponse

**文件**：[code/apps/api/app/schemas/auth.py](file:///c:/Users/86186/Desktop/AI%20Agent%E5%AD%A6%E4%B9%A0/file_rag_agent/code/apps/api/app/schemas/auth.py)

**新增**：

```python
class MeResponse(BaseModel):
    """GET /api/me 响应：当前用户主体解析结果。"""
    user_id: uuid.UUID
    username: str
    display_name: str
    dept_path: str
    clearance: int
    subjects: list[str]            # 双向展开后的全部主体
    authorized_kb_ids: list[uuid.UUID]   # G2 用：用户可访问的知识库
    acl_epoch: int                 # 调试用：当前缓存版本号
```

### 5. api/me.py · 新增 GET /api/me 端点

**文件**：`code/apps/api/app/api/me.py`（新建）

```python
@router.get("/me", response_model=MeResponse)
async def get_me(user: CurrentUser = Depends(get_current_user)) -> MeResponse:
    """返回当前登录用户的主体解析结果（手册 §5.1 M3 任务 2）。

    已由 get_current_user 完成主体解析（含 Redis 缓存），这里只是格式化输出。
    """
    return MeResponse(
        user_id=user.user_id,
        username=user.username,
        display_name=user.display_name,
        dept_path=user.dept_path,
        clearance=user.clearance,
        subjects=sorted(user.subjects),
        authorized_kb_ids=sorted(user.authorized_kb_ids),
        acl_epoch=...,  # 从 CurrentUser 暴露，或直接查 Tenant
    )
```

**acl_epoch 取值**：方案 A 是给 `CurrentUser` 加 `acl_epoch: int` 字段（load_principal_from_db 顺便返回）；方案 B 是 me 端点单独查 Tenant。**选 A**，因为 deps 已经要读 Tenant.acl_epoch 用于缓存 key，顺手带回来更省一次查询。

→ `CurrentUser` dataclass 加 `acl_epoch: int` 字段。

### 6. main.py · 注册 me_router

**文件**：[code/apps/api/app/main.py](file:///c:/Users/86186/Desktop/AI%20Agent%E5%AD%A6%E4%B9%A0/file_rag_agent/code/apps/api/app/main.py)

加 `from app.api.me import router as me_router` + `app.include_router(me_router, prefix="/api")`。

## 涉及文件汇总

| # | 文件 | 操作 |
|---|---|---|
| 1 | `apps/api/alembic/versions/0003_add_tenant_acl_epoch.py` | 新建迁移 |
| 2 | `apps/api/app/models/entities.py` | Tenant 加 acl_epoch 字段 |
| 3 | `apps/api/app/services/acl/subjects.py` | 追加 load_principal_from_db + invalidate_tenant_acl |
| 4 | `apps/api/app/services/acl/__init__.py` | 导出新函数 |
| 5 | `apps/api/app/api/deps.py` | get_current_user 调真实加载器；CurrentUser 加 acl_epoch |
| 6 | `apps/api/app/schemas/auth.py` | 加 MeResponse |
| 7 | `apps/api/app/api/me.py` | 新建 GET /api/me |
| 8 | `apps/api/app/main.py` | 注册 me_router |

## 口径对照（确保不偏）

| 手册条款 | 实现 |
|---|---|
| §3.2.2 主体 6 种 | `user:` `group:` `dept:` `role:` `region:` `public` 全覆盖 |
| §3.2.3 用户侧 A1+A2 双向展开 | 复用 `resolve_user_subjects`，不变 |
| §3.2.3 文档侧不展开祖先 | 本次不碰文档侧（M4 才接 compute_doc_acl_tags 到入库） |
| §3.2.3 D-12 visible_to_parent | 复用 `_visible_to_ancestor`，加载时把 `departments.visible_to_parent` 传入 |
| §3.2.4 写库断言 A1/A2 | 本次不碰写库路径 |
| §3.2.6 缓存 key 格式 | `acl:{tenant_id}:{user_id}:{acl_epoch}` |
| §3.2.6 TTL=300s | 用 `ACL_CACHE_TTL_SECONDS=300` |
| §3.2.6 缓存不可用降级直连 | try/except redis，不抛 |
| §3.2.6 失效点表 | `invalidate_tenant_acl` 函数先写好，M4 接入触发点 |
| §3.2.7 公开库自动成员 | 复用 `resolve_authorized_kb_ids` 的 `kb.is_public` 分支 |
| §4.2.8 acl_tags 唯一入口 | 本次不碰 compute_doc_acl_tags |

## 验证方案

1. **跑迁移**：`docker compose exec api alembic upgrade head` 应无报错；`\d tenants` 应看到 acl_epoch 列。
2. **种子数据**：`docker compose exec api python scripts/seed.py` 幂等成功（不动现有种子，新增字段默认 0 即可）。
3. **GET /api/me 验收 D1**（手册 L1308）：
   ```bash
   TOKEN=...  # 用 admin 登录拿 JWT
   curl -H "Authorization: Bearer $TOKEN" http://localhost:8000/api/me
   ```
   - admin 在 `/总部/`，返回 `subjects` 应包含 `dept:/总部/`、`dept:/总部/技术中心/`、`dept:/总部/技术中心/后端组/`（含子孙）、`public`、`user:<admin_id>`
   - 不应包含 `/总部/人力资源部/薪酬组/`（visible_to_parent=False）
   - `authorized_kb_ids` 应含公开库 id（admin 是用户，自动成员）
4. **Redis 缓存命中验证**：第二次 curl 同一 token → api 日志显示「cache hit」；`docker compose exec redis redis-cli KEYS 'acl:*'` 应有对应 key。
5. **降级验证**：临时 stop redis 容器 → curl /api/me 应仍返回 200（直连 DB 路径生效），日志有 warning。
6. **chat/ask 不退化**：curl POST /api/chat/ask 走通——deps 现在返回非空 authorized_kb_ids 和真实 subjects，但 chat 没用这俩字段（M2 信任前端 kb_ids），所以行为不变；M4 才接 subjects 到检索过滤。
7. **修改 admin 部门后缓存失效**（手动模拟 M4 触发）：在 DB 直接 UPDATE tenants SET acl_epoch = acl_epoch + 1 → 第二次 curl /api/me 应重新解析（cache key 对不上）。
