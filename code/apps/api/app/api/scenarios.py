"""场景管理接口：3 读（R1–R3）+ 6 写（W1–W6）。

★ 为什么必须有 R1–R3：`If-Match` 的输入是 `checksum`，而 `checksum` 只存在于库里。
  没有读接口，W3–W6 在契约上不可用（客户端拿不到前置条件）。这不是「顺手加的
  CRUD」，是并发机制的组成部分。

★ 错误体统一：`{"detail": {code, message, field, context}}`。`code` 是稳定机器键。
  `PolicyValidationError` / `WriteError` **必须**在 handler 内转成契约形状：
  `main.py` 的全局 `ValueError` 处理器会把它变成 `{"detail": "<原文>"}`，
  丢掉 `field` —— 这是一处真实的陷阱（`main.py:362-371`）。
"""
from __future__ import annotations

import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Header, HTTPException, Response, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import CurrentUser, get_db, require_admin
from app.schemas.needs_reindex import DeclarationOut, KbReadabilityOut, PreflightOut
from app.schemas.scenario import (
    ScenarioCreate,
    ScenarioDetail,
    ScenarioOut,
    VersionActionOut,
    VersionContent,
    VersionOut,
)
from app.services.scenario import write_service
from app.services.scenario.write_errors import WriteError, http_status

router = APIRouter(prefix="/scenarios", tags=["scenarios"])

IfMatch = Annotated[str | None, Header(alias="If-Match")]
"""`If-Match` 取 `None` 时由 service 抛 428（不在路由层挡）。

让「缺头」与「值不匹配」在同一个地方判，避免两处各判一半。

★ 默认值 MUST 写在参数处的 `= None`，**不能**写进 `Header(default=None)`：
  FastAPI 在 `analyze_param` 里对「Annotated 里带默认值」直接 `AssertionError`
  （不是警告，是 import 期崩溃 —— 整个应用起不来）。
"""


def to_http_exception(error: WriteError) -> HTTPException:
    """`WriteError` → 契约形状的 `HTTPException`。

    ★ 必须是**公开**函数：`integrations.py` 的 W7 路由也要用同一份映射。
      复制一份到那边就是第二个真源 —— 两边一旦分叉，同一类失败在两个接口上
      会给出不同的 `code`，而客户端是按 `code` 写分支的。
    """
    return HTTPException(
        status_code=http_status(error.code),
        detail={
            "code": error.code.value,
            "message": error.message,
            "field": error.field,
            "context": error.context,
        },
    )


def _published_brief(row: dict[str, Any]) -> dict[str, Any] | None:
    if row.get("published_version") is None:
        return None
    return {
        "version": row["published_version"],
        "checksum": row["published_checksum"],
        "binding_mode": row["published_binding_mode"],
        "kb_ids": [uuid.UUID(kb) for kb in (row.get("published_kb_ids") or [])],
    }


def _scenario_out(row: dict[str, Any]) -> ScenarioOut:
    return ScenarioOut(
        id=row["id"],
        scenario_key=row["scenario_key"],
        name=row["name"],
        status=row["status"],
        is_builtin=row["is_builtin"],
        db_binding=row["db_binding"],
        description=row["description"],
        published=_published_brief(row),
        draft_version=row.get("draft_version"),
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def _version_out(row: dict[str, Any]) -> VersionOut:
    return VersionOut(
        version=row["version"],
        state=row["state"],
        checksum=row["checksum"],
        policy=dict(row.get("policy") or {}),
        kb_ids=[uuid.UUID(kb) for kb in (row.get("kb_ids") or [])],
        binding_mode=row["binding_mode"],
        dna_declared=dict(row.get("dna_declared") or {}),
        change_note=row["change_note"],
        created_at=row["created_at"],
        validated_at=row["validated_at"],
        published_at=row["published_at"],
        archived_at=row["archived_at"],
    )


# ══════════════════════════════════════════════════════════════════════
# R1–R3 读
# ══════════════════════════════════════════════════════════════════════


@router.get("", response_model=list[ScenarioOut])
async def list_scenarios(
    user: CurrentUser = Depends(require_admin),
    session: AsyncSession = Depends(get_db),
) -> list[ScenarioOut]:
    rows = await write_service.list_scenarios(session, tenant_id=user.tenant_id)
    return [_scenario_out(row) for row in rows]


@router.get("/{scenario_id}", response_model=ScenarioDetail)
async def get_scenario(
    scenario_id: uuid.UUID,
    user: CurrentUser = Depends(require_admin),
    session: AsyncSession = Depends(get_db),
) -> ScenarioDetail:
    try:
        target, versions = await write_service.get_scenario_detail(
            session, tenant_id=user.tenant_id, scenario_id=scenario_id
        )
    except WriteError as error:
        raise to_http_exception(error) from error
    base = _scenario_out(target)
    return ScenarioDetail(**base.model_dump(), versions=versions)


@router.get("/{scenario_id}/versions/{version}", response_model=VersionOut)
async def get_version(
    scenario_id: uuid.UUID,
    version: int,
    response: Response,
    user: CurrentUser = Depends(require_admin),
    session: AsyncSession = Depends(get_db),
) -> VersionOut:
    try:
        row = await write_service.get_version(
            session, tenant_id=user.tenant_id, scenario_id=scenario_id, version=version
        )
    except WriteError as error:
        raise to_http_exception(error) from error
    # ETag 语义**仅**借用 RFC 的比较语义：`checksum` 不含 version / state，
    # 因此两个不同版本可以同 checksum（见 U4-4 与 API 文档）。
    response.headers["ETag"] = f'"{row["checksum"]}"'
    return _version_out(row)


# ══════════════════════════════════════════════════════════════════════
# W1–W6 写
# ══════════════════════════════════════════════════════════════════════


@router.post("", response_model=ScenarioOut, status_code=status.HTTP_201_CREATED)
async def create_scenario(
    payload: ScenarioCreate,
    response: Response,
    user: CurrentUser = Depends(require_admin),
    session: AsyncSession = Depends(get_db),
) -> ScenarioOut:
    """W1。不做 upsert：重放创建 → 409，而不是把「两次点提交」与「两人同 key」

    压成同一结果（那会掩盖真实冲突）。
    """
    try:
        scenario = await write_service.create_scenario(
            session,
            tenant_id=user.tenant_id,
            user_id=user.user_id,
            scenario_key=payload.scenario_key,
            name=payload.name,
            description=payload.description,
        )
    except WriteError as error:
        raise to_http_exception(error) from error
    response.headers["Location"] = f"/api/scenarios/{scenario.id}"
    rows = await write_service.list_scenarios(session, tenant_id=user.tenant_id)
    row = next(item for item in rows if item["id"] == scenario.id)
    return _scenario_out(row)


@router.post(
    "/{scenario_id}/versions", response_model=VersionOut, status_code=status.HTTP_201_CREATED
)
async def create_version(
    scenario_id: uuid.UUID,
    payload: VersionContent,
    user: CurrentUser = Depends(require_admin),
    session: AsyncSession = Depends(get_db),
) -> VersionOut:
    try:
        row = await write_service.create_version(
            session,
            tenant_id=user.tenant_id,
            user_id=user.user_id,
            scenario_id=scenario_id,
            policy=payload.policy,
            kb_ids=payload.kb_ids,
            change_note=payload.change_note,
        )
    except WriteError as error:
        raise to_http_exception(error) from error
    return _version_out(
        {
            "version": row.version,
            "state": row.state,
            "checksum": row.checksum,
            "policy": row.policy,
            "kb_ids": row.kb_ids,
            "binding_mode": row.binding_mode,
            "dna_declared": row.dna_declared,
            "change_note": row.change_note,
            "created_at": row.created_at,
            "validated_at": None,
            "published_at": None,
            "archived_at": None,
        }
    )


@router.put("/{scenario_id}/versions/{version}", response_model=VersionOut)
async def replace_version_content(
    scenario_id: uuid.UUID,
    version: int,
    payload: VersionContent,
    response: Response,
    if_match: IfMatch = None,
    user: CurrentUser = Depends(require_admin),
    session: AsyncSession = Depends(get_db),
) -> VersionOut:
    try:
        row = await write_service.replace_content(
            session,
            tenant_id=user.tenant_id,
            user_id=user.user_id,
            scenario_id=scenario_id,
            version=version,
            policy=payload.policy,
            kb_ids=payload.kb_ids,
            change_note=payload.change_note,
            if_match=if_match,
        )
    except WriteError as error:
        raise to_http_exception(error) from error
    response.headers["ETag"] = f'"{row.checksum}"'
    return _version_out(
        {
            "version": row.version,
            "state": row.state,
            "checksum": row.checksum,
            "policy": row.policy,
            "kb_ids": row.kb_ids,
            "binding_mode": row.binding_mode,
            "dna_declared": row.dna_declared,
            "change_note": row.change_note,
            "created_at": row.created_at,
            "validated_at": row.validated_at,
            "published_at": row.published_at,
            "archived_at": row.archived_at,
        }
    )


@router.post("/{scenario_id}/versions/{version}/validate", response_model=VersionActionOut)
async def validate_version(
    scenario_id: uuid.UUID,
    version: int,
    if_match: IfMatch = None,
    user: CurrentUser = Depends(require_admin),
    session: AsyncSession = Depends(get_db),
) -> VersionActionOut:
    try:
        row, changed = await write_service.validate_version(
            session,
            tenant_id=user.tenant_id,
            user_id=user.user_id,
            scenario_id=scenario_id,
            version=version,
            if_match=if_match,
        )
    except WriteError as error:
        raise to_http_exception(error) from error
    return VersionActionOut(
        version=row.version,
        state="validated" if changed else row.state,
        checksum=row.checksum,
        changed=changed,
    )


@router.post("/{scenario_id}/versions/{version}/publish", response_model=VersionActionOut)
async def publish_version(
    scenario_id: uuid.UUID,
    version: int,
    if_match: IfMatch = None,
    user: CurrentUser = Depends(require_admin),
    session: AsyncSession = Depends(get_db),
) -> VersionActionOut:
    # ★ 4B-1 的口径快照由 service 在**同一事务内**算（必须在归档之前，
    #   否则阻断时旧 published 已被归档）。service 返回元组的形状是 4A 的契约
    #   且被多处用例按三元素解包，故用「调用方传进来的列表」把它带出来。
    caliber_out: list[dict[str, object]] = []
    try:
        row, changed, archived = await write_service.publish(
            session,
            tenant_id=user.tenant_id,
            user_id=user.user_id,
            scenario_id=scenario_id,
            version=version,
            if_match=if_match,
            caliber_out=caliber_out,
        )
    except WriteError as error:
        raise to_http_exception(error) from error
    return VersionActionOut(
        version=row.version,
        state="published" if changed else row.state,
        checksum=row.checksum,
        changed=changed,
        archived_previous_version=archived,
        # 幂等重放（changed=false）不产生快照：4A 的语义是"不重放副作用"，
        # 重算一次快照会给出"这次发布当时的语料状态"，而那已经过去了。
        dna_snapshot=caliber_out[0] if caliber_out else None,
    )


@router.post("/{scenario_id}/versions/{version}/archive", response_model=VersionActionOut)
async def archive_version(
    scenario_id: uuid.UUID,
    version: int,
    if_match: IfMatch = None,
    user: CurrentUser = Depends(require_admin),
    session: AsyncSession = Depends(get_db),
) -> VersionActionOut:
    try:
        row, changed = await write_service.archive(
            session,
            tenant_id=user.tenant_id,
            user_id=user.user_id,
            scenario_id=scenario_id,
            version=version,
            if_match=if_match,
        )
    except WriteError as error:
        raise to_http_exception(error) from error
    return VersionActionOut(
        version=row.version,
        state="archived" if changed else row.state,
        checksum=row.checksum,
        changed=changed,
    )


# ★ W7（绑定 / 解绑场景）的路由挂在 `integrations.py`：
#   `/integrations/applications/{id}/scenario` 是**集成应用**的子资源，
#   路径前缀必须一致，否则同一个资源会有两个前缀。
#   实现体是 `write_service.bind_application`，本模块不重复包一层
#   （多一层就多一处会与 service 分叉的错误映射）。


# ══════════════════════════════════════════════════════════════════════
# R-4B-2 preflight（只读，不做任何判定冻结）
# ══════════════════════════════════════════════════════════════════════


@router.get(
    "/{scenario_id}/versions/{version}/preflight",
    response_model=PreflightOut,
)
async def preflight_version(
    scenario_id: uuid.UUID,
    version: int,
    user: CurrentUser = Depends(require_admin),
    session: AsyncSession = Depends(get_db),
) -> PreflightOut:
    """在**点发布之前**回答"如果现在点发布会发生什么"，且**不冻结**结论。

    ★ 为什么需要它（它是 DQ4B-2 选「校验放 W5」的补偿）：把 DNA 校验放 W5 是对的
      （发布期的外部条件不该在校验期冻结），但那样一来"发布前无法预知"。这个端点
      把信息**提前**给出来，同时明确"结论随时可变"—— 与 W4 的 `validated` 语义
      解耦（W4 一行未改）。
    ★ 只读、无锁、无事务副作用：**不调** `_lock_scenario`。那是写路径的锁，
      读端点拿它会与写互斥，把一个查询变成阻塞点。
    ★ 跨租户 / 不存在 → **404**（`get_version` 已按 `tenant_id` 过滤，写侧口径同源）。
    ★ 未发布（draft）也能问：这正是它的用途 —— 发布**前**预知。
      唯一例外是"已完成 T4 校验"这一项由 `ensure_publishable` 现算，不看 `state`。
    """
    from app.config.settings import get_settings
    from app.services.scenario import needs_reindex, step4_check
    from app.services.scenario.policy_spec import PolicyValidationError

    try:
        row = await write_service.get_version(
            session, tenant_id=user.tenant_id, scenario_id=scenario_id, version=version
        )
    except WriteError as error:
        raise to_http_exception(error) from error

    # ① 内容是否可发布（与 W5 同一份判据，不复制）
    publishable = True
    try:
        step4_check.ensure_publishable(
            policy=dict(row.get("policy") or {}),
            binding_mode=row["binding_mode"],
            is_builtin=bool(row.get("is_builtin", False)),
            platform_rerank_enabled=bool(get_settings().rerank_enabled),
        )
    except PolicyValidationError:
        publishable = False

    # ② 声明漂移（只比有值的四项）
    drift = needs_reindex.declaration_drift(row.get("dna_declared"))

    # ③ 该场景 kb 集合的口径可检程度（与所有其它消费方同一个函数）
    kb_ids = [uuid.UUID(str(kb)) for kb in (row.get("kb_ids") or [])]
    readability = await needs_reindex.scenario_kb_readability(
        session, tenant_id=user.tenant_id, kb_ids=kb_ids
    )
    decision = needs_reindex.judge_publish_snapshot(
        readability, declared_stale_keys=drift.stale_keys
    )
    blockers = [decision.blocker] if decision.blocker else []
    warnings = list(decision.warnings)
    if not publishable:
        # 内容不可发布与"语料可检程度"是两类事：前者是 blocker，后者最多是 warning。
        # 这里把它并入 blockers 而不是再造一个字段 —— 客户端要的是"能不能发"。
        blockers.insert(0, "NOT_PUBLISHABLE")

    return PreflightOut(
        version=row["version"],
        state=row["state"],
        checksum=row["checksum"],
        policy_publishable=publishable,
        declaration=DeclarationOut(
            stale_keys=list(drift.stale_keys),
            declared=dict(drift.declared),
            current=dict(drift.current),
        ),
        kb_readability=KbReadabilityOut(
            kb_count=readability.kb_count,
            chunks_total=readability.chunks_total,
            # 「向量路可检」= 分母 - 硬过期。它**不含**授权维度（发布时没有具体
            # 请求者，用假的 clearance 算 ACL 会让数字变成另一个口径的谎言）。
            chunks_vector_readable=max(
                readability.chunks_total - readability.chunks_hard_stale, 0
            ),
            chunks_hard_stale=readability.chunks_hard_stale,
            documents_indexed=readability.documents_indexed,
        ),
        publish_blockers=blockers,
        publish_warnings=warnings,
    )


__all__ = ["router", "to_http_exception"]
