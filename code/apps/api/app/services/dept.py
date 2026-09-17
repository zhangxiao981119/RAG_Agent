"""M3 任务 7+8 部门管理服务 —— 树形构建 / CRUD / 移动 / 子树 path 级联重写。

口径（手册 §3.2.2 + §8 D-12）：
  · path 全程以 `/` 开头并以 `/` 结尾，例如 `/总部/技术中心/后端组/`
  · depth 从 0 开始（根节点），每深一层 +1
  · visible_to_parent=False 表示对祖先不可见
  · 部门改名 / 移动时，子树所有 path 前缀替换 + depth 重算
  · 所有写操作后调 invalidate_tenant_acl，使权限缓存按 epoch 失效（§3.2.6）

事务边界：所有写方法不主动 commit；调用方负责 commit。
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass, field

from redis.asyncio import Redis
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Department, Tenant, User
from app.services.acl import invalidate_tenant_acl


@dataclass
class DeptNode:
    """部门树内存节点（用于 build_tree 返回）。"""

    id: uuid.UUID
    name: str
    path: str
    depth: int
    sort_order: int
    visible_to_parent: bool
    user_count: int
    children: list["DeptNode"] = field(default_factory=list)


def _build_tree_from_flat(rows: list[Department], user_count_map: dict[uuid.UUID, int]) -> list[DeptNode]:
    """扁平行列表 → 树。按 sort_order + name 排序子节点。"""
    by_id: dict[uuid.UUID, DeptNode] = {}
    for r in rows:
        by_id[r.id] = DeptNode(
            id=r.id,
            name=r.name,
            path=r.path,
            depth=r.depth,
            sort_order=r.sort_order,
            visible_to_parent=r.visible_to_parent,
            user_count=user_count_map.get(r.id, 0),
        )

    roots: list[DeptNode] = []
    for r in rows:
        node = by_id[r.id]
        if r.parent_id is None:
            roots.append(node)
        else:
            parent = by_id.get(r.parent_id)
            if parent is None:
                roots.append(node)  # 父找不到按根处理（防御）
            else:
                parent.children.append(node)

    # 子节点排序：sort_order 优先，name 次之
    def sort_key(n: DeptNode) -> tuple:
        return (n.sort_order, n.name)

    def sort_subtree(n: DeptNode) -> None:
        n.children.sort(key=sort_key)
        for c in n.children:
            sort_subtree(c)

    for r in roots:
        sort_subtree(r)
    roots.sort(key=sort_key)
    return roots


async def build_tree(session: AsyncSession, tenant_id: uuid.UUID) -> list[DeptNode]:
    """构建完整部门树（含每节点 user_count）。"""
    rows = (await session.execute(
        select(Department).where(Department.tenant_id == tenant_id).order_by(Department.depth, Department.sort_order, Department.name)
    )).scalars().all()

    # 一次性查所有部门的 user_count
    user_count_rows = (await session.execute(
        select(User.dept_id, text("count(*)"))
        .where(User.tenant_id == tenant_id, User.dept_id.is_not(None))
        .group_by(User.dept_id)
    )).all()
    user_count_map = {row[0]: row[1] for row in user_count_rows}

    return _build_tree_from_flat(list(rows), user_count_map)


async def _bump_acl_epoch(session: AsyncSession, redis: Redis, tenant_id: uuid.UUID) -> int:
    """触发 tenant_acl_epoch+1 + 清旧缓存。返回新 epoch。"""
    return await invalidate_tenant_acl(session, redis, tenant_id)


async def create_dept(
    session: AsyncSession,
    redis: Redis,
    tenant_id: uuid.UUID,
    name: str,
    parent_id: uuid.UUID | None,
    sort_order: int = 0,
    visible_to_parent: bool = True,
) -> Department:
    """新建部门。parent_id=None 表示根节点。"""
    if parent_id is None:
        # 根节点 path = /name/
        path = f"/{name}/"
        depth = 0
    else:
        parent = await session.get(Department, parent_id)
        if parent is None or parent.tenant_id != tenant_id:
            raise ValueError("父部门不存在")
        path = f"{parent.path}{name}/"
        depth = parent.depth + 1

    # 校验 path 唯一（uq_departments_tenant_path 也会兜底，但提前报更友好）
    existing = (await session.execute(
        select(Department).where(Department.tenant_id == tenant_id, Department.path == path)
    )).first()
    if existing is not None:
        raise ValueError(f"部门路径已存在：{path}")

    dept = Department(
        tenant_id=tenant_id,
        parent_id=parent_id,
        name=name,
        path=path,
        depth=depth,
        sort_order=sort_order,
        visible_to_parent=visible_to_parent,
    )
    session.add(dept)
    await session.flush()  # 拿 id

    # 部门树变化 → acl_epoch+1（subjects 含 dept:<path>，path 列表变了）
    await _bump_acl_epoch(session, redis, tenant_id)
    return dept


async def _recascade_subtree(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    dept_id: uuid.UUID,
    old_path: str,
    new_path: str,
    new_base_depth: int,
) -> None:
    """子树 path 级联重写：把 old_path 前缀全部替换为 new_path，并把 depth 重新算。

    调用前提：dept_id 这一行自身的 path / depth 已经更新过；本函数处理子孙。
    """
    # 1. 子孙行：path 替换前缀
    #    path LIKE 'old_path%' 且 path != new_path（排除自己）
    # 用 raw SQL 更高效
    sql = text(
        """
        UPDATE departments
        SET path = :new_prefix || substring(path, :old_len + 1),
            depth = :new_base_depth + (depth - :old_depth_self)
        WHERE tenant_id = :tid
          AND path LIKE :pattern
          AND path <> :new_path
        """
    )
    old_depth_self = (await session.execute(
        select(Department.depth).where(Department.id == dept_id)
    )).scalar_one()
    await session.execute(sql, {
        "tid": tenant_id,
        "new_prefix": new_path,
        "old_len": len(old_path),
        "new_base_depth": new_base_depth,
        "old_depth_self": old_depth_self,
        "pattern": f"{old_path}%",
        "new_path": new_path,
    })


async def rename_dept(
    session: AsyncSession,
    redis: Redis,
    tenant_id: uuid.UUID,
    dept_id: uuid.UUID,
    new_name: str,
) -> Department:
    """重命名部门：自身 path 替换 + 子树 path 前缀替换 + acl_epoch+1。"""
    dept = await session.get(Department, dept_id)
    if dept is None or dept.tenant_id != tenant_id:
        raise ValueError("部门不存在")

    old_path = dept.path
    # 新 path：把 old_path 倒数第二段替换为 new_name
    # 例：/总部/技术中心/后端组/ → /总部/技术中心/{new_name}/
    # 算法：old_path[:-1].rsplit('/', 1)[0] + '/' + new_name + '/'
    parent_path = old_path[:-1].rsplit("/", 1)[0]  # 去尾 / 再切最后一段
    new_path = f"{parent_path}/{new_name}/"
    if new_path == old_path:
        return dept  # 名字相同，无变化

    # 校验新 path 不冲突
    existing = (await session.execute(
        select(Department).where(
            Department.tenant_id == tenant_id,
            Department.path == new_path,
            Department.id != dept_id,
        )
    )).first()
    if existing is not None:
        raise ValueError(f"目标路径已存在：{new_path}")

    dept.name = new_name
    dept.path = new_path
    await session.flush()

    # 子树 path 前缀替换 + depth 不变（重命名不改层级）
    # 但仍调 _recascade_subtree 来更新子孙 path
    sql = text(
        """
        UPDATE departments
        SET path = :new_prefix || substring(path, :old_len + 1)
        WHERE tenant_id = :tid
          AND path LIKE :pattern
          AND path <> :new_path
        """
    )
    await session.execute(sql, {
        "tid": tenant_id,
        "new_prefix": new_path,
        "old_len": len(old_path),
        "pattern": f"{old_path}%",
        "new_path": new_path,
    })

    await _bump_acl_epoch(session, redis, tenant_id)
    return dept


async def move_dept(
    session: AsyncSession,
    redis: Redis,
    tenant_id: uuid.UUID,
    dept_id: uuid.UUID,
    new_parent_id: uuid.UUID | None,
) -> Department:
    """移动部门到新父下：自身 path/depth/parent_id 更新 + 子树 path+depth 级联重写 + acl_epoch+1。"""
    dept = await session.get(Department, dept_id)
    if dept is None or dept.tenant_id != tenant_id:
        raise ValueError("部门不存在")

    # 防止移动到自身或自己的子树（造成循环）
    if new_parent_id is not None:
        if new_parent_id == dept_id:
            raise ValueError("不能移动到自身")
        new_parent = await session.get(Department, new_parent_id)
        if new_parent is None or new_parent.tenant_id != tenant_id:
            raise ValueError("目标父部门不存在")
        # 新父不能是当前部门的子孙（即新父的 path 不能以 dept.path 为前缀）
        if new_parent.path.startswith(dept.path):
            raise ValueError("不能移动到自己的子部门下")

        new_path = f"{new_parent.path}{dept.name}/"
        new_depth = new_parent.depth + 1
    else:
        # 移到根
        new_path = f"/{dept.name}/"
        new_depth = 0

    if new_path == dept.path:
        return dept  # 没变化

    # 校验新 path 不冲突
    existing = (await session.execute(
        select(Department).where(
            Department.tenant_id == tenant_id,
            Department.path == new_path,
            Department.id != dept_id,
        )
    )).first()
    if existing is not None:
        raise ValueError(f"目标路径已存在：{new_path}")

    old_path = dept.path
    old_depth = dept.depth

    # 更新自身
    dept.parent_id = new_parent_id
    dept.path = new_path
    dept.depth = new_depth
    await session.flush()

    # 子树 path + depth 级联重写
    sql = text(
        """
        UPDATE departments
        SET path = :new_prefix || substring(path, :old_len + 1),
            depth = :new_base_depth + (depth - :old_depth_self)
        WHERE tenant_id = :tid
          AND path LIKE :pattern
          AND path <> :new_path
        """
    )
    await session.execute(sql, {
        "tid": tenant_id,
        "new_prefix": new_path,
        "old_len": len(old_path),
        "new_base_depth": new_depth,
        "old_depth_self": old_depth,
        "pattern": f"{old_path}%",
        "new_path": new_path,
    })

    await _bump_acl_epoch(session, redis, tenant_id)
    return dept


async def update_dept_attrs(
    session: AsyncSession,
    redis: Redis,
    tenant_id: uuid.UUID,
    dept_id: uuid.UUID,
    *,
    sort_order: int | None = None,
    visible_to_parent: bool | None = None,
) -> Department:
    """修改部门属性（不影响 path）。

    visible_to_parent 变化影响祖先对子孙的可见性，必须触发 acl_epoch+1。
    sort_order 不影响权限，但顺带一起做。
    """
    dept = await session.get(Department, dept_id)
    if dept is None or dept.tenant_id != tenant_id:
        raise ValueError("部门不存在")

    changed = False
    if sort_order is not None and sort_order != dept.sort_order:
        dept.sort_order = sort_order
        changed = True
    if visible_to_parent is not None and visible_to_parent != dept.visible_to_parent:
        dept.visible_to_parent = visible_to_parent
        changed = True

    if changed:
        await session.flush()
        # visible_to_parent 变化影响主体解析（A1 祖先看到子孙的范围）
        await _bump_acl_epoch(session, redis, tenant_id)
    return dept


async def delete_dept(
    session: AsyncSession,
    redis: Redis,
    tenant_id: uuid.UUID,
    dept_id: uuid.UUID,
) -> None:
    """删除部门。约束：必须无子部门 + 无用户。"""
    dept = await session.get(Department, dept_id)
    if dept is None or dept.tenant_id != tenant_id:
        raise ValueError("部门不存在")

    # 校验无子部门
    child_count = (await session.execute(
        select(Department).where(Department.tenant_id == tenant_id, Department.parent_id == dept_id)
    )).all()
    if child_count:
        raise ValueError("部门下还有子部门，不能删除")

    # 校验无用户
    user_count = (await session.execute(
        select(User).where(User.tenant_id == tenant_id, User.dept_id == dept_id)
    )).first()
    if user_count is not None:
        raise ValueError("部门下还有用户，不能删除")

    await session.delete(dept)
    await session.flush()
    await _bump_acl_epoch(session, redis, tenant_id)


__all__ = [
    "DeptNode",
    "build_tree",
    "create_dept",
    "rename_dept",
    "move_dept",
    "update_dept_attrs",
    "delete_dept",
]
