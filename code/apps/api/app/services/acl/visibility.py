"""可见性判定 —— 手册 §3.2.1 的四闸门 + §3.3.2 的下推过滤。

判定式（**MUST 逐字实现，MUST NOT 简化/重排为 OR**）：

    visible(chunk, user) :=
          chunk.deleted_at IS NULL
      AND chunk.is_latest = true
      AND chunk.tenant_id = user.tenant_id
      AND chunk.level_rank <= user.clearance          -- G1
      AND chunk.kb_id IN user.authorized_kb_ids       -- G2（必要条件）
      AND NOT (chunk.deny_subjects ∩ user.subjects)   -- G3（优先）
      AND (chunk.acl_tags ∩ user.subjects ≠ ∅)        -- G4

⚠ 本文件是**内存版参考实现**，用于单元测试与本地验证。
   生产检索 MUST 把同样的条件写进 SQL（见 PUSHDOWN_WHERE_SQL），
   MUST NOT 先取出再在应用层过滤（H7）。
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from app.services.acl.subjects import Principal

# ── 检索下推 SQL（唯一真源）─────────────────────────────────
# 业务代码 MUST 从这里取，MUST NOT 各写一份。
PUSHDOWN_WHERE_SQL = """WHERE tenant_id      = :tenant_id
  AND deleted_at     IS NULL
  AND is_latest      = true
  AND level_rank    <= :clearance              -- G1
  AND kb_id          = ANY(:authorized_kb_ids) -- G2
  AND NOT (deny_subjects && :user_subjects)    -- G3
  AND acl_tags       && :user_subjects         -- G4"""


@dataclass(frozen=True)
class Chunk:
    """检索与权限的最终载体（手册 §4.2.8）。"""

    id: str
    tenant_id: str
    kb_id: str
    level_rank: int
    acl_tags: frozenset[str]
    deny_subjects: frozenset[str] = frozenset()
    deleted_at: datetime | None = None
    is_latest: bool = True


def is_visible(chunk: Chunk, principal: Principal) -> bool:
    """四闸门判定。任何一个不成立 → 不可见（AND 语义）。"""
    # 0 · 软删 / 历史版本 / 租户（判定式的前三行）
    if chunk.deleted_at is not None:
        return False
    if not chunk.is_latest:
        return False
    if chunk.tenant_id != principal.tenant_id:
        return False

    # G1 · 密级
    if chunk.level_rank > principal.clearance:
        return False

    # G2 · 知识库（必要条件；公开库也要走这一条）
    if chunk.kb_id not in principal.authorized_kb_ids:
        return False

    # G3 · 显式拒绝（优先级最高，命中即不可见）
    if chunk.deny_subjects & principal.subjects:
        return False

    # G4 · 标签匹配
    return bool(chunk.acl_tags & principal.subjects)


def filter_visible(chunks: list[Chunk], principal: Principal) -> list[Chunk]:
    """内存版过滤。生产环境 MUST 改为 SQL 下推（PUSHDOWN_WHERE_SQL）。"""
    return [c for c in chunks if is_visible(c, principal)]


def pushdown_params(principal: Principal) -> dict[str, object]:
    """构造下推参数。★ MUST 从 Principal 取，MUST NOT 从前端入参取。"""
    return {
        "tenant_id": principal.tenant_id,
        "clearance": principal.clearance,
        "authorized_kb_ids": sorted(principal.authorized_kb_ids),
        "user_subjects": sorted(principal.subjects),
    }
