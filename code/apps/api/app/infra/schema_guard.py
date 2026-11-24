"""启动期 schema 版本闸门（T1）。

★ 为什么它是 T1（拒绝启动）而不是 warning：
  `TENANT_GOVERNANCE_ENABLED=true` 时，治理查询会读 0031 新增的列
  （`tenants.allowed_db_keys`、`scenarios.pinned_version` 等）。若库仍停在 0030，
  这些查询会在**请求期**抛 UndefinedColumn → 500，而且只对走到治理分支的请求出现。
  代码与库的版本错位属于「部署动作没做完」，必须在启动时暴露，不能等用户提问才炸。

★ 为什么关闭态保持旧行为（只 warning、不阻断）：
  治理关闭时一条新列都不读，旧库仍能正常服务；此时拒绝启动会把「只是没跑最新迁移」
  升级成「整个服务不可用」，与既有发布流程（迁移由编排里的 migrate 一次性服务保证）
  相比是过度收紧。

★ 单一真源是 alembic 版本，不复制迁移列清单：
  这里只比较「库的 `alembic_version`」与「代码 heads」，不查 `information_schema`。
  列清单一旦复制一份，迁移与闸门就会各自漂移，而漂移的第一个后果是闸门绿着、
  查询却因缺列 500 —— 正是本闸门要消灭的那种信号。
"""
from __future__ import annotations

import logging
from pathlib import Path

from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import text

from app.config.settings import get_settings

logger = logging.getLogger(__name__)

# `app/infra/schema_guard.py` → parents[2] = `apps/api`（alembic.ini 所在目录）
_ALEMBIC_INI = Path(__file__).resolve().parents[2] / "alembic.ini"

_WARNING_HINT = (
    "执行 docker compose -p kagent -f docker-compose.prod.yml "
    "run --rm migrate alembic upgrade head"
)


def _code_heads() -> set[str]:
    """读代码侧迁移 heads（alembic.ini 缺失或损坏时由调用方统一处置）。"""
    return set(ScriptDirectory.from_config(Config(str(_ALEMBIC_INI))).get_heads())


async def assert_schema_matches_code(*, context: str) -> None:
    """校验数据库 schema 版本与代码 heads 一致；治理开启时不匹配即拒绝启动。

    `context` 只用于日志与异常文案（"api" / "worker"），不参与判定。

    判定与后果：
      · 一致 → INFO 并返回；
      · 不一致且治理开启 → RuntimeError（消息含 db_revision / code_heads / 升级提示）；
      · 不一致且治理关闭 → WARNING，不阻断（与改动前一致）；
      · 校验本身失败（alembic.ini 缺失 / 表不存在 / 连接失败等）：
        治理开启 → RuntimeError，且用 `from exc` 保留根因；
        治理关闭 → WARNING 后返回。
    """
    governed = get_settings().tenant_governance_enabled

    try:
        code_heads = _code_heads()
        # 函数内 import：避免 `app.database` 与 `app.main` 在模块加载期形成循环。
        from app.database import SessionLocal

        async with SessionLocal() as session:
            db_rev = (
                await session.execute(text("SELECT version_num FROM alembic_version"))
            ).scalar_one_or_none()
    except Exception as exc:
        if governed:
            raise RuntimeError("无法校验 schema 版本，拒绝在治理开启时启动") from exc
        logger.warning("schema 版本启动检查跳过", exc_info=True, extra={"app": context})
        return

    if db_rev in code_heads:
        logger.info(
            "启动检查：schema 版本一致",
            extra={"app": context, "revision": db_rev},
        )
        return

    if governed:
        raise RuntimeError(
            f"启动检查失败（{context}）：数据库 schema 版本与代码不一致。"
            f"db_revision={db_rev!r}，code_heads={sorted(code_heads)}。"
            "治理已开启（TENANT_GOVERNANCE_ENABLED=true），"
            "先执行 alembic upgrade head（治理至少需要 0031）再启动。"
        )

    logger.warning(
        "启动检查：数据库 schema 版本与代码不一致",
        extra={
            "db_revision": db_rev,
            "code_heads": sorted(code_heads),
            "hint": _WARNING_HINT,
            "app": context,
        },
    )
