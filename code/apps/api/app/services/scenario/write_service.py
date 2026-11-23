"""场景写接口的**事务实现** —— 唯一 impure 写入口。

分层与依赖方向（设计 §B.1）：

    api/scenarios.py  →  write_service  →  {step4_check, dna_declare, policy_spec,
                                             snapshot, resolver, audit}

★ `write_service` 与 `resolver` **互不 import**：一个只读、一个只写，共享的只有
  `snapshot.compute_checksum` 与 `dna_declare`。这样「写接口把读路径搞坏」在结构上
  不可能（它们没有边）。唯一的例外是 `resolver.invalidate`（缓存失效必须由写侧
  在**提交后**调用，`resolver.py:111-117`）。

★ 锁纪律（全局唯一顺序，不允许任何用例反过来）：
      ① `scenarios` 行 FOR UPDATE  →  ② `scenario_versions` 行 FOR UPDATE
  两个写用例（发布 v3 与归档 v2）会同时碰这两张表；顺序不一致就是死锁的教科书条件。
  所有用例都先拿同一把「场景锁」，因此**同一个场景的写天然串行**，包括版本号分配。
"""
from __future__ import annotations

import json
import logging
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any

import sqlalchemy as sa
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.config.settings import get_settings
from app.infra import metrics
from app.models import IntegrationApplication, KnowledgeBase, Scenario, ScenarioVersion
from app.services import audit
from app.services.scenario import (
    needs_reindex,
    policy_spec,
    resolver,
    snapshot,
    step4_check,
)
from app.services.scenario.dna_declare import build_dna_declared
from app.services.scenario.policy_spec import PolicyValidationError
from app.services.scenario.snapshot import BUILTIN_SCENARIO_KEY
from app.services.scenario.write_errors import WriteCode, WriteError, constraint_name

logger = logging.getLogger(__name__)


# ══════════════════════════════════════════════════════════════════════
# SQL 常量（每条 MUST 带 tenant_id —— 由 test_step4_sql_tenant_guard 文本守卫）
# ══════════════════════════════════════════════════════════════════════

_SQL_LOCK_SCENARIO = """
SELECT id, is_builtin, status
FROM scenarios
WHERE id = :scenario_id AND tenant_id = :tenant_id
FOR UPDATE
"""
"""所有写用例的第一条语句（唯一的锁）。

缺 `tenant_id` 条件 = 跨租户外键被锁住（`resolver.py:100` 的同一条纪律）。
"""

_SQL_LOCK_VERSION = """
SELECT v.version, v.state, v.checksum, v.policy, v.kb_ids, v.binding_mode,
       v.dna_declared, v.change_note, v.created_at, v.validated_at,
       v.published_at, v.archived_at
FROM scenario_versions v
WHERE v.scenario_id = :scenario_id
  AND v.tenant_id   = :tenant_id
  AND v.version     = :version
FOR UPDATE
"""
"""版本行锁。`v.tenant_id` 是**加严**：租户不一致视为查不到（能读到但锁不住别人的行）。"""

_SQL_NEXT_VERSION = """
SELECT COALESCE(MAX(version), 0) + 1 AS next_version
FROM scenario_versions
WHERE scenario_id = :scenario_id AND tenant_id = :tenant_id
"""

_SQL_ARCHIVE_PUBLISHED = """
UPDATE scenario_versions
SET state = 'archived', archived_at = now()
WHERE scenario_id = :scenario_id
  AND tenant_id   = :tenant_id
  AND state       = 'published'
  AND version    <> :keep_version
"""
"""发布前先归档旧 published。★ 必须在发布 UPDATE 之前执行：
`uq_scenario_versions_one_published` 是**立即生效**的部分唯一索引（非 DEFERRABLE），
先发布会让它在该语句执行的瞬间报 UniqueViolation，与「并发发布」这个合法场景
混成同一个错误。先归档后发布让索引在整个事务里恒满足（1 行 → 0 行 → 1 行）。"""

_SQL_PUBLISH = """
UPDATE scenario_versions
SET state = 'published',
    published_at = COALESCE(published_at, now())
WHERE scenario_id = :scenario_id
  AND tenant_id   = :tenant_id
  AND version     = :version
  AND state       = 'validated'
"""
"""带 `AND state='validated'`：即使将来的重构把上面的 Python 判定改错，
UPDATE 也不会把 archived 拉回 published（rowcount=0 → STATE_CONFLICT）。"""

_SQL_COUNT_APPLICATIONS = """
SELECT count(*) AS application_count
FROM integration_applications
WHERE tenant_id = :tenant_id AND scenario_id = :scenario_id
"""


# ══════════════════════════════════════════════════════════════════════
# 行快照（锁的返回值）
# ══════════════════════════════════════════════════════════════════════


@dataclass(frozen=True)
class ScenarioLock:
    scenario_id: uuid.UUID
    is_builtin: bool
    status: str


@dataclass(frozen=True)
class VersionRow:
    version: int
    state: str
    checksum: str
    policy: dict[str, Any]
    kb_ids: list[str]
    binding_mode: str
    dna_declared: dict[str, Any]
    change_note: str
    created_at: datetime
    validated_at: datetime | None
    published_at: datetime | None
    archived_at: datetime | None


async def _lock_scenario(
    session: AsyncSession, *, tenant_id: uuid.UUID, scenario_id: uuid.UUID
) -> ScenarioLock:
    row = (
        await session.execute(
            sa.text(_SQL_LOCK_SCENARIO).bindparams(
                scenario_id=scenario_id, tenant_id=tenant_id
            )
        )
    ).first()
    if row is None:
        # 跨租户与不存在**同一个码** —— 403/404 的差异本身就是存在性探针
        raise WriteError(WriteCode.NOT_FOUND)
    return ScenarioLock(
        scenario_id=scenario_id, is_builtin=bool(row.is_builtin), status=row.status
    )


async def _lock_version(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    scenario_id: uuid.UUID,
    version: int,
) -> VersionRow:
    row = (
        await session.execute(
            sa.text(_SQL_LOCK_VERSION).bindparams(
                scenario_id=scenario_id, tenant_id=tenant_id, version=version
            )
        )
    ).first()
    if row is None:
        raise WriteError(WriteCode.NOT_FOUND)
    return VersionRow(
        version=row.version,
        state=row.state,
        checksum=row.checksum,
        policy=dict(row.policy or {}),
        kb_ids=list(row.kb_ids or []),
        binding_mode=row.binding_mode,
        dna_declared=dict(row.dna_declared or {}),
        change_note=row.change_note,
        created_at=row.created_at,
        validated_at=row.validated_at,
        published_at=row.published_at,
        archived_at=row.archived_at,
    )


def _require_if_match(if_match: str | None, checksum: str) -> None:
    """`If-Match` 判定：缺头 → 428；不匹配 → 409（只回显前 6 位）。

    ★ 顺序：调用方 MUST 先做存在性/状态判定再调本函数 —— 否则 404 场景会返回 428，
      等于泄露「这个版本曾经存在过」。
    """
    if if_match is None or not if_match.strip():
        raise WriteError(WriteCode.PRECONDITION_REQUIRED, field="If-Match")
    # ★ 登记为 U4-12（本步只注释、不改行为）：`strip('W/')` 是**字符集剥离**，不是前缀剥离 ——
    #   它会把两端任意个 `W` / `/` 一次剥掉，而 RFC 7232 的 `W/` 是**前缀**且大小写敏感。
    #   当前安全是因为 `checksum` 是纯 hex（不含 `W` 与 `/`）；`checksum` 格式一旦变更
    #   （base64url / 引入 `W`），这里 MUST 改成前缀判断 `re.sub(r"^W/", "", v)`。
    candidate = if_match.strip().strip('"').strip("W/").strip('"')
    if candidate != checksum:
        raise WriteError(
            WriteCode.CHECKSUM_MISMATCH,
            field="If-Match",
            context={"expected_prefix": checksum[:6], "got_prefix": candidate[:6]},
        )


async def _require_kb_ids(
    session: AsyncSession, *, tenant_id: uuid.UUID, ids: Sequence[uuid.UUID]
) -> list[str]:
    """知识库存在性 + 归属本租户。失败 → 400 `WRITE_INVALID`（`field="kb_ids"`）。

    ★ 与本项目既有口径的差异（设计 §A.3 W2）：`integrations.py:109-118` 用 404
      「部分知识库不存在」，因为那查的是**路径资源**；这里是**请求体字段**错误，
      404 会让客户端以为场景不存在。`context` 只给缺失计数，不回显 id 列表。
    """
    unique = list(dict.fromkeys(str(kb_id) for kb_id in ids))
    if not unique:
        raise WriteError(
            WriteCode.WRITE_INVALID, field="kb_ids", context={"missing": 0}
        )
    rows = (
        await session.execute(
            sa.select(KnowledgeBase.id).where(
                KnowledgeBase.id.in_([uuid.UUID(k) for k in unique]),
                KnowledgeBase.tenant_id == tenant_id,
            )
        )
    ).scalars().all()
    if len(rows) != len(unique):
        raise WriteError(
            WriteCode.WRITE_INVALID,
            field="kb_ids",
            context={"missing": len(unique) - len(rows)},
        )
    return unique


# ══════════════════════════════════════════════════════════════════════
# W1 创建场景
# ══════════════════════════════════════════════════════════════════════


async def create_scenario(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    user_id: uuid.UUID,
    scenario_key: str,
    name: str,
    description: str = "",
) -> Scenario:
    """W1：创建场景（`is_builtin=false`，`status='active'`）。

    ★ `default` key 必须**显式判等**（设计 §A.5.2）：种子行是**按租户**插的，
      若某租户的内置行缺失（`builtin_fallback` 路径存在且有测试），W1 用 `default`
      会插成功 —— 于是该租户出现一个「叫 default 但不是内置」的场景，
      场景列表多出一项同名条目，运维与审计都会被带偏。`uq_scenarios_tenant_key`
      在「内置行缺失」时挡不住它。
    """
    if scenario_key == BUILTIN_SCENARIO_KEY:
        raise WriteError(WriteCode.KEY_RESERVED, field="scenario_key")

    scenario = Scenario(
        tenant_id=tenant_id,
        scenario_key=scenario_key,
        name=name,
        description=description,
        is_builtin=False,
        status="active",
    )
    session.add(scenario)
    try:
        await session.flush()
    except IntegrityError as exc:
        await session.rollback()
        name_ = constraint_name(exc)
        if name_ == "uq_scenarios_tenant_key":
            raise WriteError(
                WriteCode.KEY_EXISTS, field="scenario_key", context={"key": scenario_key}
            ) from exc
        raise

    await audit.record_with_session(
        session,
        tenant_id,
        user_id,
        "scenario.create",
        object_type="scenario",
        object_id=str(scenario.id),
        detail={"scenario_key": scenario_key, "name": name},
    )
    await session.commit()
    return scenario


# ══════════════════════════════════════════════════════════════════════
# W2 创建 draft 版本
# ══════════════════════════════════════════════════════════════════════


def _validated_policy(policy: Mapping[str, object] | None, *, field_prefix: str = "") -> dict:
    """`validate_policy` 的失败映射成 400 `WRITE_INVALID`（保留 `field`）。"""
    try:
        return policy_spec.validate_policy(policy)
    except PolicyValidationError as exc:
        raise WriteError(
            WriteCode.WRITE_INVALID, field=exc.field or "policy", message_override=str(exc)
        ) from exc


async def create_version(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    user_id: uuid.UUID,
    scenario_id: uuid.UUID,
    policy: Mapping[str, object] | None,
    kb_ids: Sequence[uuid.UUID],
    change_note: str,
) -> ScenarioVersion:
    """W2：在场景下建一个 draft 版本（version 自增）。"""
    scenario = await _lock_scenario(session, tenant_id=tenant_id, scenario_id=scenario_id)
    if scenario.is_builtin:
        # 内置场景不能建版本：S-8 要求内置的已发布 policy 为空，而任何新版本
        # 按定义都可能违反它 —— 与其让 S-8 在启动时喊，不如不让它发生
        raise WriteError(WriteCode.BUILTIN_IMMUTABLE)

    checked = _validated_policy(policy)
    normalized_kb = await _require_kb_ids(session, tenant_id=tenant_id, ids=kb_ids)
    dna = build_dna_declared()
    binding_mode = "explicit"  # kb_ids 必非空 ⇒ ck_..._binding_consistent 恒成立
    checksum = snapshot.compute_checksum(
        policy=checked, kb_ids=normalized_kb, binding_mode=binding_mode, dna_declared=dna
    )

    next_version = (
        await session.execute(
            sa.text(_SQL_NEXT_VERSION).bindparams(
                scenario_id=scenario_id, tenant_id=tenant_id
            )
        )
    ).scalar_one()

    row = ScenarioVersion(
        tenant_id=tenant_id,
        scenario_id=scenario_id,
        version=next_version,
        state="draft",
        policy=checked,
        kb_ids=normalized_kb,
        binding_mode=binding_mode,
        dna_declared=dna,
        checksum=checksum,
        change_note=change_note,
    )
    session.add(row)
    try:
        await session.flush()
    except IntegrityError as exc:
        await session.rollback()
        name_ = constraint_name(exc)
        if name_ == "uq_scenario_versions_scenario_version":
            raise WriteError(
                WriteCode.VERSION_CONFLICT, field="version", context={"retry": True}
            ) from exc
        raise

    await audit.record_with_session(
        session,
        tenant_id,
        user_id,
        "scenario.version.create",
        object_type="scenario_version",
        object_id=f"{scenario_id}:{next_version}",
        detail={
            "version": next_version,
            "checksum": checksum,
            "policy_keys": sorted(checked),
            "kb_count": len(normalized_kb),
            "change_note": change_note,
        },
    )
    await session.commit()
    return row


# ══════════════════════════════════════════════════════════════════════
# W3 整体替换内容（仅 draft）
# ══════════════════════════════════════════════════════════════════════


async def replace_content(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    user_id: uuid.UUID,
    scenario_id: uuid.UUID,
    version: int,
    policy: Mapping[str, object] | None,
    kb_ids: Sequence[uuid.UUID],
    change_note: str,
    if_match: str | None,
) -> VersionRow:
    """W3：`PUT` 整体替换 draft 内容，返回替换后的行。

    `validated` 之后内容冻结（对 validated/published/archived 一律 409）：要改内容
    必须 W2 新建 draft —— 这条让「校验过的内容」与「发布出去的内容」之间没有中间窗口。
    """
    await _lock_scenario(session, tenant_id=tenant_id, scenario_id=scenario_id)
    row = await _lock_version(
        session, tenant_id=tenant_id, scenario_id=scenario_id, version=version
    )
    if row.state != "draft":
        raise WriteError(
            WriteCode.STATE_CONFLICT,
            field="state",
            context={"state": row.state, "expected": "draft"},
        )
    _require_if_match(if_match, row.checksum)

    checked = _validated_policy(policy)
    normalized_kb = await _require_kb_ids(session, tenant_id=tenant_id, ids=kb_ids)
    dna = build_dna_declared()
    checksum = snapshot.compute_checksum(
        policy=checked, kb_ids=normalized_kb, binding_mode="explicit", dna_declared=dna
    )
    changed_keys = sorted(set(checked) ^ set(row.policy))

    await session.execute(
        sa.text(
            """
            UPDATE scenario_versions
            SET policy = CAST(:policy AS jsonb),
                kb_ids = CAST(:kb_ids AS jsonb),
                binding_mode = 'explicit',
                dna_declared = CAST(:dna AS jsonb),
                checksum = :checksum,
                change_note = :change_note
            WHERE scenario_id = :scenario_id
              AND tenant_id   = :tenant_id
              AND version     = :version
              AND state       = 'draft'
            """
        ).bindparams(
            policy=json.dumps(checked, ensure_ascii=False, separators=(",", ":")),
            kb_ids=json.dumps(normalized_kb, ensure_ascii=False, separators=(",", ":")),
            dna=json.dumps(dna, ensure_ascii=False, separators=(",", ":")),
            checksum=checksum,
            change_note=change_note,
            scenario_id=scenario_id,
            tenant_id=tenant_id,
            version=version,
        )
    )
    await audit.record_with_session(
        session,
        tenant_id,
        user_id,
        "scenario.version.update",
        object_type="scenario_version",
        object_id=f"{scenario_id}:{version}",
        detail={
            "version": version,
            "from_checksum": row.checksum,
            "to_checksum": checksum,
            "changed_keys": changed_keys,
            "change_note": change_note,
        },
    )
    await session.commit()

    return await _lock_version(
        session, tenant_id=tenant_id, scenario_id=scenario_id, version=version
    )


# ══════════════════════════════════════════════════════════════════════
# W4 校验
# ══════════════════════════════════════════════════════════════════════


async def validate_version(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    user_id: uuid.UUID,
    scenario_id: uuid.UUID,
    version: int,
    if_match: str | None,
) -> tuple[VersionRow, bool]:
    """W4：`draft → validated`。返回 (行, 是否真的发生了变化)。

    ★ W4 **不跑 T4**：校验期只做「内容自洽」，发布期的外部条件（平台重排是否开启）
      不该在校验期冻结 —— 否则「今天校验、明天平台关掉重排、后天发布」会发布一个与
      校验结论相反的东西。判据放在发布那一刻（§B.5）。
    """
    await _lock_scenario(session, tenant_id=tenant_id, scenario_id=scenario_id)
    row = await _lock_version(
        session, tenant_id=tenant_id, scenario_id=scenario_id, version=version
    )
    if row.state == "validated":
        _require_if_match(if_match, row.checksum)
        return row, False
    if row.state != "draft":
        raise WriteError(
            WriteCode.STATE_CONFLICT,
            field="state",
            context={"state": row.state, "expected": "draft"},
        )
    _require_if_match(if_match, row.checksum)

    # 校验动作必须复算一次内容自洽（内容可能来自 W2 之外的路径）
    _validated_policy(row.policy)

    await session.execute(
        sa.text(
            """
            UPDATE scenario_versions
            SET state = 'validated', validated_at = now()
            WHERE scenario_id = :scenario_id
              AND tenant_id   = :tenant_id
              AND version     = :version
              AND state       = 'draft'
            """
        ).bindparams(scenario_id=scenario_id, tenant_id=tenant_id, version=version)
    )
    await audit.record_with_session(
        session,
        tenant_id,
        user_id,
        "scenario.version.validate",
        object_type="scenario_version",
        object_id=f"{scenario_id}:{version}",
        detail={"version": version, "checksum": row.checksum},
    )
    await session.commit()
    return row, True


# ══════════════════════════════════════════════════════════════════════
# W5 发布（T4 的唯一调用点）
# ══════════════════════════════════════════════════════════════════════


async def publish(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    user_id: uuid.UUID,
    scenario_id: uuid.UUID,
    version: int,
    if_match: str | None,
    caliber_out: list[dict[str, object]] | None = None,
) -> tuple[VersionRow, bool, int | None]:
    """W5：`validated → published`，并自动归档该场景的原 published。

    返回 `(行, changed, archived_previous_version)`。

    三道闸的顺序（设计 §B.5.3，每道拦的是不同的事）：
      ① 场景行 FOR UPDATE —— 并发发布串行化；
      ② 应用层状态机 + If-Match + T4 —— 业务规则（可解释、可自助修）；
      ③ DB 部分唯一索引 —— 应用层漏判时兜底（`PUBLISH_CONFLICT`）。

    `caliber_out`（Step 4B-1）：一个**只写列表**。快照在本函数内算（必须在同一
    事务、且在归档之前），但函数返回元组的形状是 4A 的契约、被多处用例按三元素
    解包 —— 加第四个返回值会改动那些用例。所以用「调用方传进来的列表」把结果
    带出去：不传 = 完全不产生快照成本，传了 = 发一次快照。
    """
    scenario = await _lock_scenario(session, tenant_id=tenant_id, scenario_id=scenario_id)
    row = await _lock_version(
        session, tenant_id=tenant_id, scenario_id=scenario_id, version=version
    )

    if row.state == "published":
        # 幂等重放仍要证明「你看到的就是当前」
        _require_if_match(if_match, row.checksum)
        return row, False, None
    if row.state != "validated":
        raise WriteError(
            WriteCode.STATE_CONFLICT,
            field="state",
            context={"state": row.state, "expected": "validated"},
        )
    _require_if_match(if_match, row.checksum)

    # ── T4：唯一能阻止坏配置进入系统的位置 ──────────────────────────
    # 位置 MUST 在 If-Match 之后（否则 T4 的结论不是针对「我看到的那份内容」）、
    # 归档之前（T4 失败而旧 published 已归档会让场景进入「没有 published」的中间态）。
    try:
        checked = step4_check.ensure_publishable(
            policy=row.policy,
            binding_mode=row.binding_mode,
            is_builtin=scenario.is_builtin,
            platform_rerank_enabled=bool(get_settings().rerank_enabled),
        )
    except PolicyValidationError as exc:
        raise WriteError(
            WriteCode.NOT_PUBLISHABLE,
            field=exc.field or "policy",
            message_override=str(exc),
        ) from exc

    # T4 返回的归一化 policy 必须与库里逐键相等：不等 = 库里内容不合法（直写 SQL）
    if checked != dict(row.policy):
        raise WriteError(
            WriteCode.NOT_PUBLISHABLE,
            field="policy",
            message_override="库中策略与校验结果不一致，拒绝发布",
        )
    # 顺带复核 checksum 与库内容一致（发现「内容被改但 checksum 没跟上」）
    if (
        snapshot.compute_checksum(
            policy=checked,
            kb_ids=list(row.kb_ids),
            binding_mode=row.binding_mode,
            dna_declared=dict(row.dna_declared),
        )
        != row.checksum
    ):
        raise WriteError(WriteCode.CHECKSUM_MISMATCH, field="checksum")

    # ── 4B-1 口径快照（只读旁路；位置三条理由，一行都不能挪）────────────
    # ① 必须在 If-Match 之后：快照要针对"我看到的那份内容"；
    # ② 必须在**归档之前**：若快照里的判定要阻断（唯一的 409 档），阻断时旧
    #    published 不能被归档 —— "会失败的判定放在最前面"是零成本的正确做法；
    # ③ 必须在 commit 之前：快照要写进**同一条审计**，不能另开事务
    #    （否则又出现"主事务回滚、审计已提交"）。
    # ★ 它**不写** scenario_versions、不进 checksum、不触发 invalidate（I-4B-5）：
    #   把观测塞进 checksum 会让同一个 If-Match 因为语料变化而失效 —— 那是把写
    #   接口的并发机制绑到语料状态上。
    drift = needs_reindex.declaration_drift(row.dna_declared)
    readability = await needs_reindex.scenario_kb_readability(
        session,
        tenant_id=tenant_id,
        kb_ids=[uuid.UUID(str(kb)) for kb in (row.kb_ids or [])],
    )
    caliber_verdict = needs_reindex.judge_publish_snapshot(
        readability, declared_stale_keys=drift.stale_keys
    )
    if caliber_verdict.blocker is not None:
        # 唯一阻断档：确定性数据损坏（有 status='indexed' 文档却 0 条 chunk）。
        # `context` 只放三个计数，不回显 kb id 列表（id 已在版本行里）。
        raise WriteError(
            WriteCode.KB_EMPTY_BUT_INDEXED,
            field="kb_ids",
            context={
                "kb_count": readability.kb_count,
                "documents_indexed": readability.documents_indexed,
                "chunks_total": readability.chunks_total,
            },
        )
    caliber_block: dict[str, object] = {
        "declaration": {
            "stale_keys": list(drift.stale_keys),
            "declared": dict(drift.declared),
            "current": dict(drift.current),
        },
        "kb_readability": {
            "kb_count": readability.kb_count,
            "chunks_total": readability.chunks_total,
            "chunks_hard_stale": readability.chunks_hard_stale,
            "documents_indexed": readability.documents_indexed,
        },
        "warnings": list(caliber_verdict.warnings),
        "note": (
            "kb_readability 的「可检」只指**口径维度**，不含授权维度；"
            "chunks_hard_stale>0 表示向量路降级为仅关键词（预期行为，不阻断）；"
            "chunker_only 是软 DNA，不在此块内。"
        ),
    }
    if caliber_verdict.warnings:
        # 只 WARN 不阻断：拒绝会**销毁它想保护的信息**（拒绝的处置是重建 draft
        # 以刷新声明，而旧声明正是"这个场景是在换模型之前设计的"这条审计事实）。
        metrics.incr("rag.caliber.publish_with_stale_dna")
    if caliber_out is not None:
        caliber_out.append(caliber_block)

    # ── 顺序不可调换：先归档旧 published，再发布目标（§C.3）──────────
    try:
        archived = (
            await session.execute(
                sa.text(_SQL_ARCHIVE_PUBLISHED).bindparams(
                    scenario_id=scenario_id, tenant_id=tenant_id, keep_version=version
                )
            )
        ).rowcount
        result = await session.execute(
            sa.text(_SQL_PUBLISH).bindparams(
                scenario_id=scenario_id, tenant_id=tenant_id, version=version
            )
        )
        if result.rowcount != 1:
            raise WriteError(
                WriteCode.STATE_CONFLICT,
                field="state",
                context={"state": row.state, "expected": "validated"},
            )
    except IntegrityError as exc:
        await session.rollback()
        name_ = constraint_name(exc)
        if name_ == "uq_scenario_versions_one_published":
            raise WriteError(WriteCode.PUBLISH_CONFLICT, context={"retry": True}) from exc
        raise

    await audit.record_with_session(
        session,
        tenant_id,
        user_id,
        "scenario.version.publish",
        object_type="scenario_version",
        object_id=f"{scenario_id}:{version}",
        detail={
            "version": version,
            "checksum": row.checksum,
            "archived_version": archived or None,
            # 半年后要能回答："这个场景发布时是不是在降级状态"（4A §B.7 的审计价值判据）。
            # 与响应体同一份内容 —— 两处都写是为了"当场可见"与"事后可查"各自独立成立。
            "dna_snapshot": caliber_block,
            "dna_warnings": list(caliber_verdict.warnings),
        },
    )
    await session.commit()
    # ★ 事务**提交后**才失效缓存（resolver.py:111-117）：事务内调用会在回滚时
    #   留下「缓存空、库里旧」的错位。
    resolver.invalidate(tenant_id, scenario_id)
    return row, True, archived or None


# ══════════════════════════════════════════════════════════════════════
# W6 归档
# ══════════════════════════════════════════════════════════════════════


async def archive(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    user_id: uuid.UUID,
    scenario_id: uuid.UUID,
    version: int,
    if_match: str | None,
) -> tuple[VersionRow, bool]:
    """W6：`published → archived`。

    ★ 前置校验是本接口的核心（§C.4）：归档是 UPDATE，`fk_integration_app_scenario`
      的 RESTRICT 完全不参与 —— 归档一个有应用引用的场景，等于让这批应用的所有
      `/ask` 与 `/search` 立刻 500（`resolver.py:88` 的 `v.state='published'` 查不到行
      → `snapshot.py:384-392` R1 → UNRESOLVABLE）。前置校验把「必然 500」变成「明确拒绝」。
    """
    scenario = await _lock_scenario(session, tenant_id=tenant_id, scenario_id=scenario_id)
    row = await _lock_version(
        session, tenant_id=tenant_id, scenario_id=scenario_id, version=version
    )

    if scenario.is_builtin:
        # 归档内置的已发布版本会让该租户全部走 builtin_fallback，且一直打
        # rag.scenario.builtin_row_missing 这个「该补种」的告警，把真告警淹掉
        raise WriteError(WriteCode.BUILTIN_IMMUTABLE)
    if row.state == "archived":
        _require_if_match(if_match, row.checksum)
        return row, False
    if row.state != "published":
        raise WriteError(
            WriteCode.STATE_CONFLICT,
            field="state",
            context={"state": row.state, "expected": "published"},
        )
    _require_if_match(if_match, row.checksum)

    application_count = (
        await session.execute(
            sa.text(_SQL_COUNT_APPLICATIONS).bindparams(
                tenant_id=tenant_id, scenario_id=scenario_id
            )
        )
    ).scalar_one()
    if application_count:
        raise WriteError(
            WriteCode.IN_USE,
            field="scenario_id",
            context={"application_count": int(application_count)},
        )

    await session.execute(
        sa.text(
            """
            UPDATE scenario_versions
            SET state = 'archived', archived_at = now()
            WHERE scenario_id = :scenario_id
              AND tenant_id   = :tenant_id
              AND version     = :version
              AND state       = 'published'
            """
        ).bindparams(scenario_id=scenario_id, tenant_id=tenant_id, version=version)
    )
    await audit.record_with_session(
        session,
        tenant_id,
        user_id,
        "scenario.version.archive",
        object_type="scenario_version",
        object_id=f"{scenario_id}:{version}",
        detail={
            "version": version,
            "checksum": row.checksum,
            "application_count": int(application_count),
        },
    )
    await session.commit()
    resolver.invalidate(tenant_id, scenario_id)
    return row, True


# ══════════════════════════════════════════════════════════════════════
# W7 绑定 / 解绑应用
# ══════════════════════════════════════════════════════════════════════


async def bind_application(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    user_id: uuid.UUID,
    application_id: uuid.UUID,
    scenario_id: uuid.UUID | None,
) -> IntegrationApplication:
    """W7：`scenario_id=None` = 解绑回内置；非 null 时必须「有 published 且非内置」。

    ★ 「有 published」是关键：否则绑定的那一刻就制造了一个必 500 的应用
      （与归档侧是同一枚硬币的两面，§C.4.2）。
    """
    application = (
        await session.execute(
            sa.select(IntegrationApplication).where(
                IntegrationApplication.id == application_id,
                IntegrationApplication.tenant_id == tenant_id,
            )
        )
    ).scalar_one_or_none()
    if application is None:
        raise WriteError(WriteCode.NOT_FOUND, message_override="应用不存在")

    from_scenario_id = application.scenario_id

    if scenario_id is not None:
        scenario = (
            await session.execute(
                sa.select(Scenario).where(
                    Scenario.id == scenario_id, Scenario.tenant_id == tenant_id
                )
            )
        ).scalar_one_or_none()
        if scenario is None:
            raise WriteError(WriteCode.NOT_FOUND)
        if scenario.is_builtin:
            # 内置 ≡ 平台默认，不需要绑定；允许绑定会制造「绑了但等于没绑」的困惑
            raise WriteError(
                WriteCode.WRITE_INVALID, field="scenario_id",
                message_override="内置场景等价于「不绑定」，请直接设 null",
            )
        if scenario.status != "active":
            raise WriteError(
                WriteCode.STATE_CONFLICT,
                field="status",
                context={"status": scenario.status, "expected": "active"},
            )
        published = (
            await session.execute(
                sa.text(
                    """
                    SELECT count(*) AS n
                    FROM scenario_versions
                    WHERE scenario_id = :scenario_id
                      AND tenant_id   = :tenant_id
                      AND state       = 'published'
                    """
                ).bindparams(scenario_id=scenario_id, tenant_id=tenant_id)
            )
        ).scalar_one()
        if not published:
            raise WriteError(
                WriteCode.NOT_PUBLISHABLE,
                field="scenario_id",
                message_override="该场景还没有已发布版本，绑定后集成请求会全部失败",
                context={"published": 0},
            )

    application.scenario_id = scenario_id
    await session.flush()
    await audit.record_with_session(
        session,
        tenant_id,
        user_id,
        "integration.application.bind_scenario",
        object_type="integration_application",
        object_id=str(application_id),
        detail={
            "application_id": str(application_id),
            "from_scenario_id": str(from_scenario_id) if from_scenario_id else None,
            "to_scenario_id": str(scenario_id) if scenario_id else None,
        },
    )
    await session.commit()
    return application


# ══════════════════════════════════════════════════════════════════════
# 读接口（R1–R3）：只读，无锁、无事务副作用
# ══════════════════════════════════════════════════════════════════════

_SQL_LIST_SCENARIOS = """
SELECT s.id, s.scenario_key, s.name, s.status, s.is_builtin, s.db_binding,
       s.description, s.created_at, s.updated_at,
       p.version AS published_version, p.checksum AS published_checksum,
       p.binding_mode AS published_binding_mode, p.kb_ids AS published_kb_ids,
       d.version AS draft_version
FROM scenarios s
LEFT JOIN scenario_versions p
  ON p.scenario_id = s.id AND p.tenant_id = s.tenant_id AND p.state = 'published'
LEFT JOIN LATERAL (
    SELECT max(v.version) AS version
    FROM scenario_versions v
    WHERE v.scenario_id = s.id AND v.tenant_id = s.tenant_id
      AND v.state IN ('draft', 'validated')
) d ON true
WHERE s.tenant_id = :tenant_id
ORDER BY s.is_builtin DESC, s.scenario_key
"""

_SQL_GET_VERSIONS = """
SELECT version, state, checksum, change_note, created_at,
       validated_at, published_at, archived_at
FROM scenario_versions
WHERE scenario_id = :scenario_id AND tenant_id = :tenant_id
ORDER BY version DESC
LIMIT 50
"""

_SQL_GET_VERSION = """
SELECT version, state, checksum, policy, kb_ids, binding_mode, dna_declared,
       change_note, created_at, validated_at, published_at, archived_at
FROM scenario_versions
WHERE scenario_id = :scenario_id AND tenant_id = :tenant_id AND version = :version
"""


async def list_scenarios(session: AsyncSession, *, tenant_id: uuid.UUID) -> list[dict]:
    rows = (
        await session.execute(
            sa.text(_SQL_LIST_SCENARIOS).bindparams(tenant_id=tenant_id)
        )
    ).mappings().all()
    return [dict(row) for row in rows]


async def get_scenario_detail(
    session: AsyncSession, *, tenant_id: uuid.UUID, scenario_id: uuid.UUID
) -> tuple[dict, list[dict]]:
    """场景详情 + 版本摘要（version 倒序，最多 50 条）。"""
    rows = await list_scenarios(session, tenant_id=tenant_id)
    target = next((row for row in rows if row["id"] == scenario_id), None)
    if target is None:
        raise WriteError(WriteCode.NOT_FOUND)
    versions = (
        await session.execute(
            sa.text(_SQL_GET_VERSIONS).bindparams(
                scenario_id=scenario_id, tenant_id=tenant_id
            )
        )
    ).mappings().all()
    return target, [dict(row) for row in versions]


async def get_version(
    session: AsyncSession, *, tenant_id: uuid.UUID, scenario_id: uuid.UUID, version: int
) -> dict:
    row = (
        await session.execute(
            sa.text(_SQL_GET_VERSION).bindparams(
                scenario_id=scenario_id, tenant_id=tenant_id, version=version
            )
        )
    ).mappings().first()
    if row is None:
        raise WriteError(WriteCode.NOT_FOUND)
    return dict(row)


def publishable_now(*, policy: Mapping[str, object]) -> bool:
    """辅助：当前平台配置下该 policy 是否可发布（供测试与运维自查，不参与写事务）。"""
    try:
        step4_check.ensure_publishable(
            policy=policy,
            binding_mode="explicit",
            is_builtin=False,
            platform_rerank_enabled=bool(get_settings().rerank_enabled),
        )
    except PolicyValidationError:
        return False
    return True


__all__ = [
    "ScenarioLock",
    "VersionRow",
    "archive",
    "bind_application",
    "create_scenario",
    "create_version",
    "get_scenario_detail",
    "get_version",
    "list_scenarios",
    "publish",
    "publishable_now",
    "replace_content",
    "validate_version",
]
