"""主体解析与标签生成 —— 手册 §3.2.2 / §3.2.3 / §3.2.4 的可运行实现。

★ 本文件是 `acl_tags` 的**唯一**生成入口（手册 §4.2.8 硬约束 2）。

口径要点（改动前先读手册）：
  · 用户侧：`祖先 ∪ 自己 ∪ 子孙`（A1 + A2 双向展开）
  · 文档侧：**只取自己所属那一条路径**，MUST NOT 展开祖先（H1）
    —— 两侧刻意不对称；若都展开祖先，任意两个主体在根节点相交 → 全公司互通。

M3 接入：`load_principal_from_db` 把 SQLAlchemy 行映射为 UserRecord/DeptNode/
KnowledgeBaseRecord，复用上面的纯函数 `build_principal` 完成解析；带 Redis 缓存
（key 含 `tenant_acl_epoch`，§3.2.6）。Redis 不可用 MUST 降级直连 DB。
"""
from __future__ import annotations

import json
import logging
import uuid
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any

from redis.asyncio import Redis
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import decisions as D
from app.models import (
    Department,
    KnowledgeBase,
    KnowledgeBaseMember,
    Tenant,
    User,
    UserGroup,
)
from app.services.acl.errors import AclPathError, AclTagError

logger = logging.getLogger(__name__)

ACL_CACHE_KEY_FMT = "acl:{tenant_id}:{user_id}:{epoch}"

# ─────────────────────────────────────────────────────────────
# 数据模型（骨架版：内存结构。M3/M4 换成 SQLAlchemy 模型，
# 但下面的函数签名与判定顺序 MUST 保持不变）
# ─────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class DeptNode:
    """组织树节点。`path` 形如 `/总部/技术中心/后端组/`。"""

    path: str
    visible_to_parent: bool = True
    """§8 D-12：False ⇒ 祖先用户 MUST NOT 看到本部门及其子树。"""


@dataclass(frozen=True)
class UserRecord:
    """用户。`dept_paths` 支持兼岗（第一个视为主部门）。"""

    id: str
    tenant_id: str
    dept_paths: tuple[str, ...] = ()
    clearance: int = D.LEVEL_RANK_INTERNAL
    group_ids: tuple[str, ...] = ()
    role_names: tuple[str, ...] = ()
    region: str | None = None


@dataclass(frozen=True)
class KnowledgeBaseRecord:
    """知识库。`is_public=True` 时所有登录用户自动成为成员（§3.2.7）。"""

    id: str
    tenant_id: str
    is_public: bool = False
    members: tuple[str, ...] = ()


@dataclass(frozen=True)
class Principal:
    """解析后的用户上下文。放进缓存的就是它（§3.2.6）。"""

    user_id: str
    tenant_id: str
    subjects: frozenset[str]
    authorized_kb_ids: frozenset[str]
    clearance: int

    def has_write_intent(self) -> bool:  # pragma: no cover - 占位，M4 用
        return False


@dataclass
class ExpansionWarning:
    """展开规模告警（§3.2.7 / §7.2 核验 SQL 的运行时补充）。"""

    user_id: str
    subject_count: int
    limit: int
    warnings: list[str] = field(default_factory=list)


# ─────────────────────────────────────────────────────────────
# 部门路径工具
# ─────────────────────────────────────────────────────────────


def normalize_dept_path(path: str) -> str:
    """规整为 `/a/b/` 形式。文档与用户两侧 MUST 用同一个函数，否则标签对不上。"""
    if not path or not path.strip("/"):
        raise AclPathError(f"部门路径为空: {path!r}")
    parts = [p.strip() for p in path.split("/") if p.strip()]
    return "/" + "/".join(parts) + "/"


def dept_ancestors(path: str, *, include_self: bool = True) -> list[str]:
    """祖先链，从根到自身：`/总部/技术中心/后端组/` → [`/总部/`, `/总部/技术中心/`, 自身]。"""
    normalized = normalize_dept_path(path)
    parts = [p for p in normalized.split("/") if p]
    out = ["/" + "/".join(parts[: i + 1]) + "/" for i in range(len(parts))]
    return out if include_self else out[:-1]


def _visible_to_ancestor(
    descendant: str, ancestor: str, vtp: Mapping[str, bool]
) -> bool:
    """`descendant` 能否被 `ancestor` 看到（§8 D-12）。

    从 `ancestor` 往下的**每一级**都必须 `visible_to_parent=True`；
    只要有一级为 False，整棵子树对祖先不可见。
    """
    for node in dept_ancestors(descendant):
        if len(node) <= len(ancestor):
            continue  # 只检查严格位于 ancestor 之下的部分
        if not vtp.get(node, True):
            return False
    return True


# ─────────────────────────────────────────────────────────────
# 用户侧：主体解析
# ─────────────────────────────────────────────────────────────


def resolve_user_subjects(
    user: UserRecord,
    departments: Mapping[str, DeptNode] | Iterable[str] = (),
) -> frozenset[str]:
    """解析用户主体。含部门双向展开（A1 + A2）。

    `public` 对所有登录用户成立（含新账号、含外部人员）。
    """
    vtp: dict[str, bool] = {}
    all_paths: list[str] = []
    nodes = (
        departments.values() if isinstance(departments, Mapping) else departments
    )
    for node in nodes:
        if isinstance(node, str):
            all_paths.append(normalize_dept_path(node))
        else:
            path = normalize_dept_path(node.path)
            all_paths.append(path)
            vtp[path] = node.visible_to_parent

    subjects: set[str] = {f"user:{user.id}", "public"}
    for gid in user.group_ids:
        subjects.add(f"group:{gid}")
    for role in user.role_names:
        subjects.add(f"role:{role}")
    if user.region:
        subjects.add(f"region:{user.region}")

    for raw_path in user.dept_paths:
        own = normalize_dept_path(raw_path)

        # A1 · 祖先 ∪ 自己
        for ancestor in dept_ancestors(own):
            subjects.add(f"dept:{ancestor}")

        # A2 · 子孙（受 §8 D-12 的 visible_to_parent 约束）
        if D.USER_SUBJECT_DESCENDANT_EXPANSION:
            for candidate in all_paths:
                if candidate == own or not candidate.startswith(own):
                    continue
                if _visible_to_ancestor(candidate, own, vtp):
                    subjects.add(f"dept:{candidate}")

    return frozenset(subjects)


def resolve_authorized_kb_ids(
    user: UserRecord,
    subjects: frozenset[str],
    knowledge_bases: Iterable[KnowledgeBaseRecord],
) -> frozenset[str]:
    """G2 的右半边：用户有权限访问的知识库集合。

    ★ 公开库（`is_public`）对所有用户自动成立；其余靠 `kb_members` 交集。
    ★ 注意：这里是"库级"授权，**不是**文档可见性 —— 库内文档仍须过 G1/G3/G4。
    """
    out: set[str] = set()
    for kb in knowledge_bases:
        if kb.tenant_id != user.tenant_id:
            continue
        if kb.is_public and D.PUBLIC_KB_ENABLED:
            out.add(kb.id)
            continue
        if set(kb.members) & subjects:
            out.add(kb.id)
    return frozenset(out)


def build_principal(
    user: UserRecord,
    departments: Mapping[str, DeptNode] | Iterable[str] = (),
    knowledge_bases: Iterable[KnowledgeBaseRecord] = (),
) -> Principal:
    """一次解析出可直接放进缓存的 Principal（§3.2.6）。"""
    subjects = resolve_user_subjects(user, departments)
    return Principal(
        user_id=user.id,
        tenant_id=user.tenant_id,
        subjects=subjects,
        authorized_kb_ids=resolve_authorized_kb_ids(user, subjects, knowledge_bases),
        clearance=user.clearance,
    )


def check_expansion_scale(principal: Principal) -> ExpansionWarning | None:
    """主体数超过软上限时告警（不抛出 —— 告警不是拒绝服务）。"""
    count = len(principal.subjects)
    if count <= D.SUBJECT_EXPANSION_SOFT_LIMIT:
        return None
    return ExpansionWarning(
        user_id=principal.user_id,
        subject_count=count,
        limit=D.SUBJECT_EXPANSION_SOFT_LIMIT,
        warnings=["组织树过深，考虑预计算部门闭包表（手册 §7.2 核验 SQL）"],
    )


# ─────────────────────────────────────────────────────────────
# 文档侧：标签生成（唯一入口）
# ─────────────────────────────────────────────────────────────


def validate_acl_tags(tags: Iterable[str]) -> None:
    """写库前置校验（H2 / H3）。失败即拒绝入库。"""
    tag_set = {t.strip() for t in tags if t and t.strip()}
    if not tag_set:
        raise AclTagError("acl_tags MUST NOT 为空数组（须先规范化为 {public}）")
    if D.ACL_PUBLIC_MUTEX and "public" in tag_set and len(tag_set) != 1:
        raise AclTagError(
            f"public MUST 单独存在（H2 · §3.2.4）。收到: {sorted(tag_set)}；"
            "混标会让 G4 对所有用户成立，只剩 G2 在守"
        )
    if not D.ACL_LEVEL_TAGS_ALLOWED:
        bad = sorted(t for t in tag_set if t.startswith("level:"))
        if bad:
            raise AclTagError(
                f"acl_tags MUST NOT 含 level: 标签（H3 · §3.2.4）。收到: {bad}；"
                "放进来会让 G4 绕过 G1，密级形同虚设"
            )


def compute_doc_acl_tags(
    *,
    dept_path: str | None = None,
    extra_tags: Iterable[str] = (),
) -> frozenset[str]:
    """由文档属性算出 `chunk.acl_tags`。**这是唯一入口**（手册 §4.2.8）。

    · `dept_path` 只写入文档**自己**那一条路径 → MUST NOT 展开祖先（H1）
    · 结果为空 → 规范化为 `{public}`（MUST NOT 留空数组）
    · 结果 MUST 过 `validate_acl_tags`
    """
    tags: set[str] = {t.strip() for t in extra_tags if t and t.strip()}

    if dept_path:
        own = normalize_dept_path(dept_path)
        if D.DOC_TAG_ANCESTOR_EXPANSION:
            # ★ 只为回归测试保留：证明"打开它会全公司互通"（tests/acl/test_visibility.py::test_10）。
            #   生产路径 MUST NOT 走到这里 —— self_check() 会拦住这个取值。
            for ancestor in dept_ancestors(own):
                tags.add(f"dept:{ancestor}")
        else:
            tags.add(f"dept:{own}")

    if not tags:
        tags = {"public"}

    validate_acl_tags(tags)
    return frozenset(tags)


# ─────────────────────────────────────────────────────────────
# M3 DB 接入：从 SQLAlchemy 行解析 Principal，带 Redis 缓存（§3.2.6）
# ─────────────────────────────────────────────────────────────


def _principal_to_cache_value(principal: Principal, dept_path: str) -> str:
    """Principal + dept_path → JSON 字符串，便于 Redis 存储。frozenset 转 sorted list。"""
    return json.dumps(
        {
            "user_id": principal.user_id,
            "tenant_id": principal.tenant_id,
            "subjects": sorted(principal.subjects),
            "authorized_kb_ids": sorted(principal.authorized_kb_ids),
            "clearance": principal.clearance,
            "dept_path": dept_path,
        },
        ensure_ascii=False,
    )


def _principal_from_cache_value(raw: str) -> tuple[Principal, str]:
    """JSON 字符串 → (Principal, dept_path)。list 转回 frozenset。"""
    data: dict[str, Any] = json.loads(raw)
    principal = Principal(
        user_id=data["user_id"],
        tenant_id=data["tenant_id"],
        subjects=frozenset(data["subjects"]),
        authorized_kb_ids=frozenset(data["authorized_kb_ids"]),
        clearance=data["clearance"],
    )
    return principal, data.get("dept_path", "")


async def _resolve_principal_from_db(
    session: AsyncSession,
    user_id: uuid.UUID,
    tenant_id: uuid.UUID,
) -> tuple[Principal, str]:
    """从 DB 加载并解析 (Principal, dept_path)。无缓存逻辑，纯数据加载 + 调 build_principal。

    加载范围：用户行、tenant 全部 departments、用户参与的 user_groups、
    tenant 全部 knowledge_bases + kb_members。tenant 级组织树通常 < 200 节点，
    全量加载 + 原 A1+A2 展开逻辑足够；§7.2 SQL 核验留待 M4 写下推时再做。
    """
    user = await session.get(User, user_id)
    if user is None or user.tenant_id != tenant_id:
        raise ValueError(f"user {user_id} 不属于 tenant {tenant_id}")
    if user.status != "active":
        raise ValueError(f"user {user_id} 已禁用")

    # b. tenant 全部 departments（path + visible_to_parent）→ Dict[path, DeptNode]
    dept_rows = (
        await session.execute(
            select(Department).where(Department.tenant_id == tenant_id)
        )
    ).scalars().all()
    departments: dict[str, DeptNode] = {
        d.path: DeptNode(path=d.path, visible_to_parent=d.visible_to_parent)
        for d in dept_rows
    }

    # c. 用户参与的 user_groups → group_ids（字符串）
    user_group_rows = (
        await session.execute(
            select(UserGroup.group_id).where(
                UserGroup.tenant_id == tenant_id,
                UserGroup.user_id == user_id,
            )
        )
    ).scalars().all()
    group_ids = tuple(str(gid) for gid in user_group_rows)

    # d. role_names 在 User 数组字段，直接用
    role_names = tuple(user.role_names or [])

    # e. 用户主部门路径（如有）；同时作为展示用 dept_path 返回
    dept_paths: tuple[str, ...] = ()
    dept_path = ""
    if user.dept_id is not None:
        dept = await session.get(Department, user.dept_id)
        if dept is not None and dept.tenant_id == tenant_id:
            dept_paths = (dept.path,)
            dept_path = dept.path

    # f. tenant 全部 knowledge_bases + kb_members
    kb_rows = (
        await session.execute(
            select(KnowledgeBase).where(KnowledgeBase.tenant_id == tenant_id)
        )
    ).scalars().all()

    member_rows = (
        await session.execute(
            select(KnowledgeBaseMember).where(
                KnowledgeBaseMember.tenant_id == tenant_id
            )
        )
    ).scalars().all()

    # 按 kb_id 分组，拼成 "subject_type:subject_id" 形式喂给 KnowledgeBaseRecord.members
    members_by_kb: dict[uuid.UUID, list[str]] = {}
    for m in member_rows:
        members_by_kb.setdefault(m.kb_id, []).append(f"{m.subject_type}:{m.subject_id}")

    kbs = tuple(
        KnowledgeBaseRecord(
            id=str(kb.id),
            tenant_id=str(kb.tenant_id),
            is_public=kb.is_public,
            members=tuple(members_by_kb.get(kb.id, ())),
        )
        for kb in kb_rows
    )

    # 构造 UserRecord + 解析（复用纯函数 build_principal）
    user_record = UserRecord(
        id=str(user.id),
        tenant_id=str(user.tenant_id),
        dept_paths=dept_paths,
        clearance=user.clearance,
        group_ids=group_ids,
        role_names=role_names,
        region=None,  # M3 暂不支持 region 主体
    )
    return build_principal(user_record, departments, kbs), dept_path


async def load_principal_from_db(
    session: AsyncSession,
    redis: Redis | None,
    user_id: uuid.UUID,
    tenant_id: uuid.UUID,
) -> tuple[Principal, int, str]:
    """从 DB 解析 Principal，带 Redis 缓存（§3.2.6）。

    返回 (Principal, acl_epoch, dept_path)。
    acl_epoch 给调用方回填到 CurrentUser；dept_path 给前端展示用。

    流程：
      1. 读 Tenant.acl_epoch
      2. 查 Redis key `acl:{tid}:{uid}:{epoch}`
         命中 → 反序列化为 (Principal, dept_path) 返回
      3. 未命中 → _resolve_principal_from_db 解析 → 写回 Redis
      4. Redis 异常 → 降级直连 DB，MUST NOT 抛（§3.2.6：「缓存不可用 MUST 降级直连 DB」）
    """
    tenant = await session.get(Tenant, tenant_id)
    if tenant is None:
        raise ValueError(f"tenant {tenant_id} 不存在")
    acl_epoch = tenant.acl_epoch

    cache_key = ACL_CACHE_KEY_FMT.format(
        tenant_id=str(tenant_id), user_id=str(user_id), epoch=acl_epoch
    )

    # 2. 尝试 Redis 缓存
    if redis is not None:
        try:
            raw = await redis.get(cache_key)
            if raw:
                logger.debug("acl cache hit: %s", cache_key)
                principal, dept_path = _principal_from_cache_value(raw)
                return principal, acl_epoch, dept_path
        except Exception:
            logger.warning("acl cache 读失败，降级直连 DB", exc_info=True)

    # 3. DB 加载
    principal, dept_path = await _resolve_principal_from_db(session, user_id, tenant_id)

    # 4. 写回 Redis（即使 redis 为 None 也不抛）
    if redis is not None:
        try:
            await redis.setex(
                cache_key,
                D.ACL_CACHE_TTL_SECONDS,
                _principal_to_cache_value(principal, dept_path),
            )
        except Exception:
            logger.warning("acl cache 写失败，本次不缓存", exc_info=True)

    return principal, acl_epoch, dept_path


async def invalidate_tenant_acl(
    session: AsyncSession,
    redis: Redis | None,
    tenant_id: uuid.UUID,
) -> int:
    """使整个 tenant 的权限缓存失效：INCR tenant.acl_epoch。

    M4 在以下变更点调用（§3.2.6 表）：
      - kb_members 增删改
      - kb.visibility / is_public 变更
      - 用户增删改 / 换部门 / 换角色
      - user_group 成员增删改
      - 部门树结构变更（新增/移动/删除）

    本函数只负责 INCR + 写回 DB；调用方负责 commit。返回新 acl_epoch。
    Redis 端靠 epoch 不匹配自然失效，不强制 DEL（减少一次扫描）。
    """
    stmt = (
        update(Tenant)
        .where(Tenant.id == tenant_id)
        .values(acl_epoch=Tenant.acl_epoch + 1)
        .returning(Tenant.acl_epoch)
    )
    result = await session.execute(stmt)
    new_epoch = result.scalar_one()

    # 可选：批量 DEL acl:{tid}:* 减少内存（非必要，靠 epoch 失效即可）
    if redis is not None:
        try:
            pattern = f"acl:{tenant_id}:*"
            async for key in redis.scan_iter(match=pattern, count=100):
                await redis.delete(key)
        except Exception:
            logger.warning(
                "acl cache 批量删除失败，靠 epoch 失效即可",
                exc_info=True,
            )

    return new_epoch
