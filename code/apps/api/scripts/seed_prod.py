"""生产环境初始化种子（M5 任务 6/7）—— 幂等。

与开发用 seed.py 的区别：
  - 只建生产运行必需的最小数据：默认租户 + admin 角色 + 管理员账号 + 唯一公开知识库
  - 不建组织树、测试用户、业务知识库、评估用例
  - 管理员密码从环境变量 ADMIN_PASSWORD 读取（.env.prod.example 中有占位）

首次启动引导（手册 M5 任务 7）：
  创建管理员 → 建公开库 → 第一个业务知识库由管理员登录后在界面创建。
"""
from __future__ import annotations

import asyncio

import bcrypt
from sqlalchemy import select

from app.config.settings import get_settings
from app.database import SessionLocal
from app.models import KnowledgeBase, Role, Tenant, User, UserRole


async def seed_prod() -> None:
    settings = get_settings()
    password_hash = bcrypt.hashpw(settings.admin_password.encode(), bcrypt.gensalt()).decode()

    async with SessionLocal() as session:
        # 1. 默认租户（私有化部署恒为 default）
        tenant = await session.scalar(select(Tenant).where(Tenant.code == settings.tenant_code))
        if tenant is None:
            tenant = Tenant(name="默认租户", code=settings.tenant_code)
            session.add(tenant)
            await session.flush()
            print(f"[seed_prod] 创建租户: {tenant.code}")

        # 2. admin 角色
        admin_role = await session.scalar(
            select(Role).where(Role.tenant_id == tenant.id, Role.name == "admin")
        )
        if admin_role is None:
            admin_role = Role(tenant_id=tenant.id, name="admin")
            session.add(admin_role)
            await session.flush()
            print("[seed_prod] 创建角色: admin")

        # 3. 管理员账号
        admin = await session.scalar(
            select(User).where(User.tenant_id == tenant.id, User.username == "admin")
        )
        if admin is None:
            admin = User(
                tenant_id=tenant.id,
                username="admin",
                email="admin@example.com",
                password_hash=password_hash,
                display_name="系统管理员",
                dept_id=None,
                clearance=40,  # 最高密级（SECRET）
                role_names=["admin"],
            )
            session.add(admin)
            await session.flush()
            print("[seed_prod] 创建管理员: admin（密码取自环境变量 ADMIN_PASSWORD）")

        # 4. 管理员-角色关联
        membership = await session.scalar(
            select(UserRole).where(
                UserRole.tenant_id == tenant.id,
                UserRole.user_id == admin.id,
                UserRole.role_id == admin_role.id,
            )
        )
        if membership is None:
            session.add(UserRole(tenant_id=tenant.id, user_id=admin.id, role_id=admin_role.id))

        # 5. 唯一公开知识库（手册口径：全租户只能有 1 个 is_public 的库）
        public_kb = await session.scalar(
            select(KnowledgeBase).where(
                KnowledgeBase.tenant_id == tenant.id, KnowledgeBase.is_public.is_(True)
            )
        )
        if public_kb is None:
            session.add(
                KnowledgeBase(
                    tenant_id=tenant.id,
                    name="公共知识库",
                    description="全员可见的公共资料",
                    visibility="public",
                    is_public=True,
                    owner_id=admin.id,
                )
            )
            print("[seed_prod] 创建公开知识库: 公共知识库")

        await session.commit()
        print("[seed_prod] 完成（幂等，重复执行不会产生重复数据）")


if __name__ == "__main__":
    asyncio.run(seed_prod())
