from __future__ import annotations

import asyncio

import bcrypt
from sqlalchemy import select

from app.config.settings import get_settings
from app.database import SessionLocal
from app.models import (
    Department,
    Group,
    KnowledgeBase,
    Role,
    Tenant,
    User,
    UserGroup,
    UserRole,
)

DEPARTMENTS = (
    ("总部", "/总部/", None, True),
    ("技术中心", "/总部/技术中心/", "/总部/", True),
    ("后端组", "/总部/技术中心/后端组/", "/总部/技术中心/", True),
    ("前端组", "/总部/技术中心/前端组/", "/总部/技术中心/", True),
    ("财务部", "/总部/财务部/", "/总部/", True),
    ("人力资源部", "/总部/人力资源部/", "/总部/", True),
    ("薪酬组", "/总部/人力资源部/薪酬组/", "/总部/人力资源部/", False),
    ("市场部", "/总部/市场部/", "/总部/", True),
)


async def seed() -> None:
    settings = get_settings()
    password_hash = bcrypt.hashpw(b"ChangeMe123!", bcrypt.gensalt()).decode()
    async with SessionLocal() as session:
        tenant = await session.scalar(select(Tenant).where(Tenant.code == settings.tenant_code))
        if tenant is None:
            tenant = Tenant(name="默认租户", code=settings.tenant_code)
            session.add(tenant)
            await session.flush()

        department_by_path: dict[str, Department] = {}
        for index, (name, path, parent_path, visible) in enumerate(DEPARTMENTS):
            department = await session.scalar(
                select(Department).where(Department.tenant_id == tenant.id, Department.path == path)
            )
            if department is None:
                department = Department(
                    tenant_id=tenant.id,
                    parent_id=department_by_path[parent_path].id if parent_path else None,
                    name=name,
                    path=path,
                    depth=path.strip("/").count("/"),  # /总部/ -> 0, /总部/技术中心/ -> 1
                    sort_order=index,
                    visible_to_parent=visible,
                )
                session.add(department)
                await session.flush()
            department_by_path[path] = department

        admin_role = await session.scalar(select(Role).where(Role.tenant_id == tenant.id, Role.name == "admin"))
        if admin_role is None:
            admin_role = Role(tenant_id=tenant.id, name="admin")
            session.add(admin_role)

        external_group = await session.scalar(
            select(Group).where(Group.tenant_id == tenant.id, Group.name == "external_partners")
        )
        if external_group is None:
            external_group = Group(tenant_id=tenant.id, name="external_partners", kind="external")
            session.add(external_group)
            await session.flush()

        dept_paths = [item[1] for item in DEPARTMENTS[2:]]
        users: dict[str, User] = {}
        for index in range(30):
            username = "admin" if index == 0 else "external" if index == 29 else f"user{index:02d}"
            user = await session.scalar(
                select(User).where(User.tenant_id == tenant.id, User.username == username)
            )
            if user is None:
                is_external = index == 29
                user = User(
                    tenant_id=tenant.id,
                    username=username,
                    email=f"{username}@example.com",
                    password_hash=password_hash,
                    display_name="系统管理员" if index == 0 else "外部人员" if is_external else f"用户{index:02d}",
                    dept_id=None if is_external else department_by_path[dept_paths[index % len(dept_paths)]].id,
                    clearance=10 if is_external else 40 if index == 0 else 20,
                    role_names=["admin"] if index == 0 else [],
                )
                session.add(user)
                await session.flush()
            users[username] = user

        admin_membership = await session.scalar(
            select(UserRole).where(
                UserRole.tenant_id == tenant.id,
                UserRole.user_id == users["admin"].id,
                UserRole.role_id == admin_role.id,
            )
        )
        if admin_membership is None:
            session.add(UserRole(tenant_id=tenant.id, user_id=users["admin"].id, role_id=admin_role.id))

        external_membership = await session.scalar(
            select(UserGroup).where(
                UserGroup.tenant_id == tenant.id,
                UserGroup.user_id == users["external"].id,
                UserGroup.group_id == external_group.id,
            )
        )
        if external_membership is None:
            session.add(UserGroup(tenant_id=tenant.id, user_id=users["external"].id, group_id=external_group.id))

        kb_specs = (
            ("公共知识库", "全员可见的公共资料", "public", True),
            ("技术知识库", "技术团队资料", "restricted", False),
            ("管理知识库", "企业管理资料", "restricted", False),
        )
        for name, description, visibility, is_public in kb_specs:
            exists = await session.scalar(
                select(KnowledgeBase).where(KnowledgeBase.tenant_id == tenant.id, KnowledgeBase.name == name)
            )
            if exists is None:
                session.add(
                    KnowledgeBase(
                        tenant_id=tenant.id,
                        name=name,
                        description=description,
                        visibility=visibility,
                        is_public=is_public,
                        owner_id=users["admin"].id,
                    )
                )
        await session.commit()


if __name__ == "__main__":
    asyncio.run(seed())
