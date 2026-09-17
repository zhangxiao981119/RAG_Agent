"""M4 任务 1 知识库成员管理服务 —— 四种主体（user / group / dept / role）。

口径（手册 §4.2.6 + §3.2.2）：
  · subject_id 存储格式：user/group → UUID 字符串；dept → 规整后的部门路径；
    role → 角色名（users.role_names 数组里的名字）
  · G2 判定：kb_members 拼成 "subject_type:subject_id" 与用户 subjects 求交集
    （subjects.py::_resolve_principal_from_db），所以这里的存储格式必须与之一致
  · PUT 全量替换语义（手册 §5.1：设置成员）；成员集合实际变化才 acl_epoch+1
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass

from redis.asyncio import Redis
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import (
    Department,
    Group,
    KnowledgeBase,
    KnowledgeBaseMember,
    Role,
    User,
)
from app.services.acl import invalidate_tenant_acl, normalize_dept_path

# 合法主体类型（与 models.KnowledgeBaseMember 的 CHECK 约束一致）
VALID_SUBJECT_TYPES = ("user", "group", "dept", "role")


@dataclass
class KbMemberRow:
    """成员行（含展示用 label）。"""

    subject_type: str
    subject_id: str
    label: str


def _parse_uuid(raw: str, kind: str) -> uuid.UUID:
    """user/group 的 subject_id 必须是 UUID。"""
    try:
        return uuid.UUID(raw)
    except ValueError as exc:
        raise ValueError(f"{kind} 的 subject_id 必须是 UUID：{raw}") from exc


async def list_members(
    session: AsyncSession, tenant_id: uuid.UUID, kb_id: uuid.UUID
) -> list[KbMemberRow]:
    """列出知识库成员，label 用于前端展示。

    label 规则：user → 显示名；group → 组名；dept → 部门路径；role → 角色名本身。
    主体实体已被删除时（kb_members 无外键，可能残留孤儿行）label 回退为原始 id。
    """
    rows = (await session.execute(
        select(KnowledgeBaseMember)
        .where(
            KnowledgeBaseMember.tenant_id == tenant_id,
            KnowledgeBaseMember.kb_id == kb_id,
        )
        .order_by(KnowledgeBaseMember.subject_type, KnowledgeBaseMember.created_at)
    )).scalars().all()

    # 按类型收集，用于批量反查展示名
    user_ids: list[uuid.UUID] = []
    group_ids: list[uuid.UUID] = []
    dept_paths: list[str] = []
    for m in rows:
        if m.subject_type == "user":
            user_ids.append(_parse_uuid(m.subject_id, "user"))
        elif m.subject_type == "group":
            group_ids.append(_parse_uuid(m.subject_id, "group"))
        elif m.subject_type == "dept":
            dept_paths.append(m.subject_id)

    user_names: dict[uuid.UUID, str] = {}
    if user_ids:
        user_names = dict((await session.execute(
            select(User.id, User.display_name).where(
                User.tenant_id == tenant_id, User.id.in_(user_ids)
            )
        )).all())
    group_names: dict[uuid.UUID, str] = {}
    if group_ids:
        group_names = dict((await session.execute(
            select(Group.id, Group.name).where(
                Group.tenant_id == tenant_id, Group.id.in_(group_ids)
            )
        )).all())
    dept_names: dict[str, str] = {}
    if dept_paths:
        dept_names = dict((await session.execute(
            select(Department.path, Department.name).where(
                Department.tenant_id == tenant_id, Department.path.in_(dept_paths)
            )
        )).all())

    out: list[KbMemberRow] = []
    for m in rows:
        if m.subject_type == "user":
            label = user_names.get(_parse_uuid(m.subject_id, "user"), m.subject_id)
        elif m.subject_type == "group":
            label = group_names.get(_parse_uuid(m.subject_id, "group"), m.subject_id)
        elif m.subject_type == "dept":
            # 部门路径本身已含层级信息，直接展示；能反查到则补注部门名
            name = dept_names.get(m.subject_id)
            label = f"{name}（{m.subject_id}）" if name else m.subject_id
        else:  # role：subject_id 就是角色名
            label = m.subject_id
        out.append(KbMemberRow(
            subject_type=m.subject_type, subject_id=m.subject_id, label=label
        ))
    return out


async def _validate_subjects_exist(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    members: list[tuple[str, str]],
) -> None:
    """校验每个主体都真实存在，否则拒绝写入（避免存进永远匹配不上的死数据）。"""
    user_ids: list[uuid.UUID] = []
    group_ids: list[uuid.UUID] = []
    dept_paths: list[str] = []
    role_names: list[str] = []
    for subject_type, subject_id in members:
        if subject_type == "user":
            user_ids.append(_parse_uuid(subject_id, "user"))
        elif subject_type == "group":
            group_ids.append(_parse_uuid(subject_id, "group"))
        elif subject_type == "dept":
            dept_paths.append(subject_id)
        else:
            role_names.append(subject_id)

    problems: list[str] = []
    if user_ids:
        found = set((await session.execute(
            select(User.id).where(User.tenant_id == tenant_id, User.id.in_(user_ids))
        )).scalars().all())
        problems += [f"用户不存在：{uid}" for uid in user_ids if uid not in found]
    if group_ids:
        found = set((await session.execute(
            select(Group.id).where(Group.tenant_id == tenant_id, Group.id.in_(group_ids))
        )).scalars().all())
        problems += [f"用户组不存在：{gid}" for gid in group_ids if gid not in found]
    if dept_paths:
        found = set((await session.execute(
            select(Department.path).where(
                Department.tenant_id == tenant_id, Department.path.in_(dept_paths)
            )
        )).scalars().all())
        problems += [f"部门路径不存在：{p}" for p in dept_paths if p not in found]
    if role_names:
        found = set((await session.execute(
            select(Role.name).where(Role.tenant_id == tenant_id, Role.name.in_(role_names))
        )).scalars().all())
        problems += [f"角色不存在：{r}" for r in role_names if r not in found]

    if problems:
        raise ValueError("；".join(problems))


async def set_members(
    session: AsyncSession,
    redis: Redis,
    tenant_id: uuid.UUID,
    kb_id: uuid.UUID,
    members: list[tuple[str, str]],
) -> tuple[list[KbMemberRow], bool]:
    """全量设置成员（PUT 语义）。返回 (设置后的成员列表, 是否发生实际变更)。

    · dept 的 subject_id 先经 normalize_dept_path 规整为 /a/b/ 形式
    · 请求内去重（唯一约束 uq_kb_members_subject）
    · 与现有集合一致 → 不动库、不动 acl_epoch（避免无意义的缓存失效）
    · 有变化 → 全删 + 全插 + tenant_acl_epoch+1（§3.2.6 失效表）
    """
    # 规整 + 去重（保持传入顺序）
    normalized: list[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for subject_type, raw_subject_id in members:
        if subject_type not in VALID_SUBJECT_TYPES:
            raise ValueError(f"不支持的主体类型：{subject_type}")
        if subject_type == "dept":
            subject_id = normalize_dept_path(raw_subject_id)
        else:
            subject_id = raw_subject_id.strip()
            if not subject_id:
                raise ValueError("subject_id 不能为空")
        key = (subject_type, subject_id)
        if key not in seen:
            seen.add(key)
            normalized.append(key)

    await _validate_subjects_exist(session, tenant_id, normalized)

    existing = set((await session.execute(
        select(KnowledgeBaseMember.subject_type, KnowledgeBaseMember.subject_id).where(
            KnowledgeBaseMember.tenant_id == tenant_id,
            KnowledgeBaseMember.kb_id == kb_id,
        )
    )).all())

    if existing == seen:
        return await list_members(session, tenant_id, kb_id), False

    await session.execute(
        delete(KnowledgeBaseMember).where(
            KnowledgeBaseMember.tenant_id == tenant_id,
            KnowledgeBaseMember.kb_id == kb_id,
        )
    )
    for subject_type, subject_id in normalized:
        session.add(KnowledgeBaseMember(
            tenant_id=tenant_id,
            kb_id=kb_id,
            subject_type=subject_type,
            subject_id=subject_id,
        ))
    await session.flush()
    await invalidate_tenant_acl(session, redis, tenant_id)
    return await list_members(session, tenant_id, kb_id), True


__all__ = [
    "VALID_SUBJECT_TYPES",
    "KbMemberRow",
    "list_members",
    "set_members",
]
