"""Step 4A 的**库层**约束补测（T4-C4 / C13 / C14 / C17）。

★ 为什么不把 C1–C17 全部重写一遍：其中 C5–C12（policy 键集/范围/类型、
  binding 一致性、kb_ids 元素类型、state 时间戳、checksum 格式）、C15（同场景两个
  published）、C16（FK RESTRICT）**已由 `test_0028_pg_constraints.py` 的 18 条用例
  逐条覆盖**，且每条都钉了约束名 + 带正向对照。再抄一遍不是"更严格"，
  而是**两个真源**：迁移改约束时只要漏改一处，就会出现「一份绿一份红」，
  最后被迫把红的那份放宽（那正是本项目已经踩过的退化路径）。
  这里只补 4 条 Step 4A 新引入、而 0028 那批**没有覆盖**的：

  | # | 约束 | 为什么 Step 4A 需要它 |
  |---|---|---|
  | C4 | `uq_scenarios_one_builtin` | W1 造非内置行 + 种子行的交互：把非内置改成内置会让租户有两个内置 |
  | C13 | `ck_scenario_versions_dna_required_keys` | `build_dna_declared()` 漏字段 ⇒ 审计镜像残缺 |
  | C14 | `ck_scenario_versions_dna_keyset` | 写侧多塞字段 ⇒ 声明镜像与迁移种子分叉 |
  | C17 | `scenario_versions.scenario_id` CASCADE | 删场景必须连带删版本（否则孤儿行累积且无人可查） |

★ 纪律与 0028 那批完全一致：**给全列 + 钉约束名 + 同用例内正向对照**（`pg_helpers`
  已经把这三条编码进 `insert_version` / `expect_violation`，所以这里是"用"纪律）。
"""
from __future__ import annotations

import json
from datetime import datetime, timezone

import asyncpg
import pytest

from tests.scenario.pg_helpers import (
    expect_violation,
    fresh,
    insert_version,
    new_scenario,
    new_tenant,
    requires_db,
)

# ★ 不用模块级 `pytestmark = pytest.mark.asyncio`：本文件里还有一条**同步**用例
#   （`test_dna_mirror_shape_matches_migration_seed`），模块级 mark 会让它报
#   `PytestWarning: marked asyncio but not async` —— 而那些警告在 `-q` 下会被
#   滚动条冲掉，于是「有一条用例其实没跑」这件事永远没人看见。


def _dna(*, drop: str | None = None, extra: dict | None = None) -> dict:
    """取迁移种子的 `dna_declared`，按需删一个键 / 加一个键。

    ★ 期望值取自**迁移种子**而不是 `build_dna_declared()`：
      若用后者做基准，那么「写侧生成错了」与「约束放宽了」会同时绿 —— 自证。
      迁移种子是这两个约束的原始依据（C13/C14 就是为了让它成立）。
    """
    from tests.scenario.conftest import load_migration

    dna = dict(load_migration()._SEED_DNA)
    if drop:
        dna.pop(drop)
    if extra:
        dna.update(extra)
    return dna


@requires_db
@pytest.mark.asyncio
async def test_c4_second_builtin_per_tenant_is_rejected(conn):
    """T4-C4：把某租户的非内置场景改成 `is_builtin=true`（该租户已有内置行）→ 被拒。

    ★ 这条与 0028 的 `test_one_builtin_per_tenant` 不重复：那条测的是「INSERT 两个内置」，
      这条测的是 **UPDATE 把存量行改成内置**。Step 4A 之后库里第一次有了大量非内置行，
      而「一键设成内置」这种运维误操作只会走 UPDATE 路径。
    ★ 正向对照：同一租户下把一个场景改成 `is_builtin=false` 必须成功 ——
      证明拒绝来自「第二个内置」，不是「UPDATE 本身被禁」。
    """
    tenant_id = await new_tenant(conn)
    await new_scenario(conn, tenant_id, is_builtin=True)  # 已有内置行
    plain = await new_scenario(conn, tenant_id, is_builtin=False)

    await expect_violation(
        conn,
        asyncpg.exceptions.UniqueViolationError,
        "uq_scenarios_one_builtin",
        lambda: conn.execute(
            "UPDATE scenarios SET is_builtin = true WHERE id = $1", plain
        ),
    )
    # 正向对照：反向 UPDATE 不被拦
    async with conn.transaction():
        await conn.execute(
            "UPDATE scenarios SET is_builtin = false WHERE id = $1", plain
        )


@requires_db
@pytest.mark.asyncio
async def test_c13_dna_missing_required_key_is_rejected(conn):
    """T4-C13：`dna_declared` 缺 `chunker_version` → `ck_scenario_versions_dna_required_keys`。

    ★ 为什么必须拦：`dna_declared` 是**声明镜像**，它的用途是「事后比对这一版是
      按哪套口径写的」。缺一个键就等于有一条口径永远无法追溯，而它不会让任何请求失败 ——
      这类残缺只能靠库里拦。
    ★ 正向对照：`build_dna_declared()` 的真实产物必须能插进去 —— 否则就没法区分
      「约束拒了残缺」与「约束把正常数据也拒了」。
    """
    tenant_id, scenario_id = await fresh(conn)

    await expect_violation(
        conn,
        asyncpg.exceptions.CheckViolationError,
        "ck_scenario_versions_dna_required_keys",
        lambda: insert_version(
            conn, tenant_id, scenario_id, dna=_dna(drop="chunker_version")
        ),
    )
    async with conn.transaction():
        from app.services.scenario.dna_declare import build_dna_declared

        await insert_version(conn, tenant_id, scenario_id, version=2, dna=build_dna_declared())


@requires_db
@pytest.mark.asyncio
async def test_c14_dna_extra_key_is_rejected(conn):
    """T4-C14：`dna_declared` 多一个键 → `ck_scenario_versions_dna_keyset`。

    ★ 为什么必须拦「多」而不只是「少」：多出来的键会被写进审计内容，
      而 `resolver` 只认固定的 4+4 项 —— 于是那个键成为「写进去了、永远不会被读、
      也永远不会被发现」的孤儿字段。键集必须**双向封闭**。
    """
    tenant_id, scenario_id = await fresh(conn)

    await expect_violation(
        conn,
        asyncpg.exceptions.CheckViolationError,
        "ck_scenario_versions_dna_keyset",
        lambda: insert_version(
            conn, tenant_id, scenario_id, dna=_dna(extra={"extra_key": 1})
        ),
    )
    async with conn.transaction():
        from app.services.scenario.dna_declare import build_dna_declared

        await insert_version(conn, tenant_id, scenario_id, version=2, dna=build_dna_declared())


@requires_db
@pytest.mark.asyncio
async def test_c17_deleting_scenario_cascades_versions(conn):
    """T4-C17：删场景（无应用引用）→ **不报错**，且版本行数归零（CASCADE 生效）。

    ★ 与 C16（有引用时 RESTRICT 挡住）是两面：RESTRICT 管「有引用不许删」，
      CASCADE 管「删了必须带着版本一起走」。只测 RESTRICT 会让
      「版本行变成孤儿」这种数据腐坏无人发现 —— 孤儿行不报错、不拦截、
      只会让 `scenario_versions` 无限增长并且再也无法通过 API 查询。
    """
    tenant_id, scenario_id = await fresh(conn)
    await insert_version(conn, tenant_id, scenario_id, version=1)
    await insert_version(
        conn, tenant_id, scenario_id, version=2, state="validated",
        validated_at=datetime.now(timezone.utc),
    )

    async with conn.transaction():
        await conn.execute("DELETE FROM scenarios WHERE id = $1", scenario_id)

    remaining = await conn.fetchval(
        "SELECT count(*) FROM scenario_versions WHERE scenario_id = $1", scenario_id
    )
    assert int(remaining) == 0, f"删场景后仍有 {remaining} 条孤儿版本行（CASCADE 未生效）"
    # 正向对照：场景行本身确实没了（证明 DELETE 真的执行了，不是静默无操作）
    gone = await conn.fetchval("SELECT count(*) FROM scenarios WHERE id = $1", scenario_id)
    assert int(gone) == 0


def _dna(*, drop: str | None = None, extra: dict | None = None) -> dict:
    """同步版 dna 构造（`_dna` 的别名，避免在 lambda 里 await）。"""
    from tests.scenario.conftest import load_migration

    dna = dict(load_migration()._SEED_DNA)
    if drop:
        dna.pop(drop)
    if extra:
        dna.update(extra)
    return dna


def test_dna_mirror_shape_matches_migration_seed():
    """静态对照：`build_dna_declared()` 与迁移 `_SEED_DNA` **同构**（键集合逐字相等）。

    ★ 这条不连库，却守住 C13/C14 的真正目的：两条 CHECK 只保证「形状合法」，
      而「写侧生成的形状 == 种子形状」只能在这里证明。两者一旦分叉，
      种子行与 API 写出的行在审计上不可比 —— 而审计的全部意义就是可比。
    """
    from app.services.scenario.dna_declare import build_dna_declared
    from tests.scenario.conftest import load_migration

    assert set(build_dna_declared()) == set(load_migration()._SEED_DNA)
    assert json.dumps(build_dna_declared(), sort_keys=True) == json.dumps(
        dict(load_migration()._SEED_DNA), sort_keys=True
    ), "写侧的 dna 镜像与种子行不等 —— 两者都是「声明」，必须逐字相同"
