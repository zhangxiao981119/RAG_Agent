"""租户治理面（S7-5）：库白名单 / 场景分级 / 钉定 / 上限的读写入口。

路由只做三件事：鉴权（`require_admin`）、契约形状转换、异常映射。
所有校验与 SQL 都在 `services/tenant/governance.py` —— 路由层若也写一份校验，
就会出现「同一个值，PUT 能过、GET 解析不了」这类只在某个方向暴露的分叉。

★ 错误体统一：`{"detail": {code, message, field, context}}`。
  与 `api/scenarios.py` 同形态：客户端是按 `code` 写分支的，
  两个管理端接口给出不同形状会让客户端各写一套。
"""
from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import CurrentUser, get_db, require_admin
from app.config.settings import get_settings
from app.schemas.tenant_governance import (
    CapsOut,
    DbScopeIn,
    DbScopeOut,
    GovernanceUpdate,
    ScenarioGovernanceDetail,
    ScenarioGovernanceOut,
    ScenarioTierIn,
)
from app.services.tenant import governance
from app.services.tenant.governance import GovernanceError

router = APIRouter(prefix="/admin", tags=["admin"])


def _http_exception(error: GovernanceError) -> HTTPException:
    """`GovernanceError` → 契约形状的 `HTTPException`。

    ★ 映射表放在服务层（`governance_http_status`），路由只做搬运：
      「哪个码是 404」属于错误语义，不属于 HTTP 层。
    """
    return HTTPException(
        status_code=governance.governance_http_status(error.code),
        detail={
            "code": error.code.value,
            "message": error.message,
            "field": error.field,
            "context": error.context,
        },
    )


def _scenario_out(row: dict) -> ScenarioGovernanceOut:
    return ScenarioGovernanceOut(
        id=row["id"],
        scenario_key=row["scenario_key"],
        name=row["name"],
        status=row["status"],
        db_binding=row["db_binding"],
        tier=row["tier"],
        pinned_version=row["pinned_version"],
        caps=CapsOut(**row["caps"]),
    )


def _detail_out(row: dict) -> ScenarioGovernanceDetail:
    base = _scenario_out(row)
    return ScenarioGovernanceDetail(
        **base.model_dump(),
        governance_enabled=row["governance_enabled"],
        version=row["version"],
        version_source=row["version_source"],
        # `effective` 是 `dict[str, dict]`：交给 pydantic 逐项校验成 `EffectiveKeyOut`。
        effective=row["effective"],
    )


# ══════════════════════════════════════════════════════════════════════
# 库白名单
# ══════════════════════════════════════════════════════════════════════


@router.get("/tenant/db-scope", response_model=DbScopeOut)
async def read_db_scope(
    user: CurrentUser = Depends(require_admin),
    session: AsyncSession = Depends(get_db),
) -> DbScopeOut:
    """读租户库白名单（读库里的最新值，不走缓存）。"""
    allowed = await governance.get_db_scope(session, user.tenant_id)
    return DbScopeOut(
        allowed_db_keys=allowed,
        governance_enabled=bool(get_settings().tenant_governance_enabled),
    )


@router.put("/tenant/db-scope", response_model=DbScopeOut)
async def write_db_scope(
    payload: DbScopeIn,
    user: CurrentUser = Depends(require_admin),
    session: AsyncSession = Depends(get_db),
) -> DbScopeOut:
    """整体替换租户库白名单。`null` = 不限制，`[]` = 全部拒绝。"""
    try:
        allowed = await governance.update_db_scope(
            session,
            tenant_id=user.tenant_id,
            user_id=user.user_id,
            allowed_db_keys=payload.allowed_db_keys,
        )
    except GovernanceError as error:
        raise _http_exception(error) from error
    return DbScopeOut(
        allowed_db_keys=allowed,
        governance_enabled=bool(get_settings().tenant_governance_enabled),
    )


# ══════════════════════════════════════════════════════════════════════
# 场景治理
# ══════════════════════════════════════════════════════════════════════


@router.get("/scenario-governance", response_model=list[ScenarioGovernanceOut])
async def list_scenario_governance(
    user: CurrentUser = Depends(require_admin),
    session: AsyncSession = Depends(get_db),
) -> list[ScenarioGovernanceOut]:
    rows = await governance.list_scenario_governance(
        session, tenant_id=user.tenant_id
    )
    return [_scenario_out(row) for row in rows]


@router.get(
    "/scenario-governance/{scenario_id}", response_model=ScenarioGovernanceDetail
)
async def read_scenario_governance(
    scenario_id: uuid.UUID,
    user: CurrentUser = Depends(require_admin),
    session: AsyncSession = Depends(get_db),
) -> ScenarioGovernanceDetail:
    try:
        row = await governance.get_scenario_governance(
            session, tenant_id=user.tenant_id, scenario_id=scenario_id
        )
    except GovernanceError as error:
        raise _http_exception(error) from error
    return _detail_out(row)


@router.put(
    "/scenario-governance/{scenario_id}/tier",
    response_model=ScenarioGovernanceDetail,
)
async def write_scenario_tier(
    scenario_id: uuid.UUID,
    payload: ScenarioTierIn,
    user: CurrentUser = Depends(require_admin),
    session: AsyncSession = Depends(get_db),
) -> ScenarioGovernanceDetail:
    """搬库完成后的分级登记（tier 与 db_binding 同句写入）。

    ★ 本接口**不搬数据**：搬库仍按 Step 6 SOP 执行，这里只登记「数据已搬完」。
      `cold` 必须 `db_binding=null`；`warm` 的库位必须已注册且在本租户白名单内；
      `hot` 由服务层拒绝（`TIER_HOT_UNSUPPORTED`）。
    """
    try:
        await governance.update_scenario_tier(
            session,
            tenant_id=user.tenant_id,
            user_id=user.user_id,
            scenario_id=scenario_id,
            tier=payload.tier,
            db_binding=payload.db_binding,
        )
        # 写后响应必须与 GET 逐字段一致（复用读侧组装）。
        row = await governance.get_scenario_governance(
            session, tenant_id=user.tenant_id, scenario_id=scenario_id
        )
    except GovernanceError as error:
        raise _http_exception(error) from error
    return _detail_out(row)


@router.put(
    "/scenario-governance/{scenario_id}", response_model=ScenarioGovernanceDetail
)
async def write_scenario_governance(
    scenario_id: uuid.UUID,
    payload: GovernanceUpdate,
    user: CurrentUser = Depends(require_admin),
    session: AsyncSession = Depends(get_db),
) -> ScenarioGovernanceDetail:
    """按**显式传入**的键更新治理字段（稀疏更新）。

    ★ `model_fields_set` 而不是 `model_dump(exclude_none=True)`：
      后者会把「显式传 null（清除钉定 / 清除上限）」一并丢掉，
      于是「清除」这个操作在接口上不存在 —— 而它正是钉定功能的一半。
    """
    fields = {name: getattr(payload, name) for name in payload.model_fields_set}
    try:
        await governance.update_scenario_governance(
            session,
            tenant_id=user.tenant_id,
            user_id=user.user_id,
            scenario_id=scenario_id,
            fields=fields,
        )
        # 复用读侧组装详情：更新接口的响应与 GET 必须逐字段一致，
        # 否则客户端要按「哪个接口给的数据更全」写两套解析。
        row = await governance.get_scenario_governance(
            session, tenant_id=user.tenant_id, scenario_id=scenario_id
        )
    except GovernanceError as error:
        raise _http_exception(error) from error
    return _detail_out(row)
