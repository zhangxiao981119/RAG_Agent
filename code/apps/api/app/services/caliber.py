"""答案口径快照 —— 记录「这条答案按什么口径产生」。

用于合规申诉与问题回溯：给定一条答案，能回答「它是哪个 contract 版本 / 哪个 LLM 模型 /
哪个 embedding 模型 / 哪个提示词版本 / 哪些阈值与参数下产出的」。

两个入口：
  · build_caliber_snapshot()          纯代码/配置口径（无 IO），同步可用，便于测试
  · snapshot_for_message(tenant_id)   在代码口径之上，合并该租户当前生效的提示词版本号

★ 永久记忆约束：快照 MUST NOT 含任何**用户内容**（问题 / 答案 / 画像 / 记忆项）——
  它只描述「系统按什么口径运行」，是口径而非数据。故本模块不接收、不读取任何用户输入。
"""
from __future__ import annotations

import uuid
from datetime import UTC, datetime

from app.config import decisions
from app.config.config_profile import config_fingerprint
from app.config.index_profile import current_index_profile
from app.config.settings import get_settings
from app.services import prompt_registry


def build_caliber_snapshot() -> dict:
    """纯代码/配置口径快照（不含 DB 中的提示词版本 —— 那部分见 snapshot_for_message）。

    原实现位于 services/memory.py（作为「永久记忆」的一环），现上提为独立的
    「答案口径」模块 —— 同一份口径既用于永久记忆，也写入每条 assistant 消息的 meta。
    """
    settings = get_settings()
    profile = current_index_profile()
    return {
        "contract_version": decisions.CONTRACT_VERSION,
        # ── 运行时配置指纹（项3：配置版本）──
        # 部署可变项（模型端点 / 超时 / 批量 / 预算…）的脱敏指纹：同一指纹 = 同一套运行配置。
        # 详细配置见 config/config_profile.runtime_config_snapshot()。
        "config_fingerprint": config_fingerprint(),
        # ── 模型版本 ──
        "llm_model": settings.llm_model,
        "embedding_model": settings.embedding_model,
        "rerank_model": settings.rerank_model,
        # ── 索引口径（方案一）──
        "embedding_text_version": profile.embedding_text_version,
        "chunker_version": profile.chunker_version,
        # ── 检索 / 生成口径 ──
        "relevance_threshold_vector": decisions.RELEVANCE_THRESHOLD_VECTOR,
        "relevance_threshold_rerank": decisions.RELEVANCE_THRESHOLD_RERANK,
        "rrf_k": decisions.RRF_K,
        "top_k_recall": decisions.TOP_K_RECALL,
        "top_k_rerank": decisions.TOP_K_RERANK,
        "chunk_target_tokens": decisions.CHUNK_TARGET_TOKENS,
        "cot_mode": decisions.COT_MODE,
        "grounding_check_enabled": decisions.GROUNDING_CHECK_ENABLED,
        "snapshot_at": datetime.now(UTC).isoformat(),
    }


async def snapshot_for_message(
    tenant_id: uuid.UUID | None, chain_mode: str | None = None
) -> dict:
    """消息级口径快照 = 代码口径 + 该租户当前生效的提示词版本号 + 本次检索模式。

    tenant_id 为 None（无租户上下文）时，提示词版本退回内置默认（version 0）。
    chain_mode 是**本次请求**的检索侧链式推理模式（off / single / multi:N）——
    它随问题复杂度信号变化，故不能像 cot_mode 那样进代码级静态快照，
    改由调用方（聊天持久化单点）传入；未知（未跑检索的路径）时为 None。
    """
    snapshot = build_caliber_snapshot()
    snapshot["prompt_versions"] = await prompt_registry.resolve_versions(tenant_id)
    if chain_mode is not None:
        snapshot["chain_mode"] = chain_mode
    return snapshot