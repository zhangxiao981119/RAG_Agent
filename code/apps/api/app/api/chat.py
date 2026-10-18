"""Chat SSE 接口 —— 手册 §5.2 POST /api/chat/ask。

SSE 事件顺序：
  meta → stage(retrieving) → [refused | citations → delta... → done]

MUST 遵守（手册 §5.2 约束）：
  1. kb_ids 与用户 authorized_kb_ids 求交集（M2 信任前端，M4 补）
  2. citations MUST 在 delta 之前
  3. 拒答 MUST 发 refused 事件，MUST NOT 用 delta 发拒答文案
  4. delta 是增量文本，前端累加
"""
from __future__ import annotations

import asyncio
import json
import logging
import uuid
from typing import AsyncIterator

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import StreamingResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import CurrentUser, get_current_user, get_db
from app.api.rate_limit import check_chat_rate_limit
from app.config import decisions
from app.config.settings import get_settings
from app.database import SessionLocal
from app.infra import trace
from app.models import Conversation, KnowledgeBase, Message, User
from app.schemas.chat import ChatAskRequest, ImageRef
from app.services import audit
from app.services import feature_flag as flag_service
from app.services import quota as quota_service
from app.services import sensitive as sensitive_service
from app.services.caliber import snapshot_for_message
from app.services.generate import build_system_prompt, get_stream_generation_service
from app.services.guard import injection as injection_guard
from app.services.mask import mask_pii
from app.services.memory import (
    build_memory_prompt,
    ensure_context_within_limit,
    estimate_context_tokens,
    extract_facts,
)
from app.services.multimodal import (
    VisionError,
    VisionNoContentError,
    extract_text_from_image,
)
from app.services.query_rewrite import rewrite_query
from app.services.retrieve import get_retrieval_service
from app.services.router.modality import Modality, detect

logger = logging.getLogger(__name__)
router = APIRouter(tags=["chat"])

# 单轮提问允许携带的图片上限（M7 规范 §3.2：超限直接 400，MUST NOT 静默截断）
MAX_IMAGES_PER_ASK = 3


def _validate_images(images: list[ImageRef]) -> None:
    """带图提问的入参校验（数量 + 单图大小），超限抛 400。

    ★ 放在端点层而非 pydantic：规范 §3.2 要求超限返 400，pydantic 校验失败是 422。
    ★ storage_key 不重复卡大小：bucket 对象必经既有上传链路落库，
      kbs.py 上传时已按同一 max_upload_mb 上限拦截过。
    """
    if len(images) > MAX_IMAGES_PER_ASK:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"TOO_MANY_IMAGES: 单轮最多 {MAX_IMAGES_PER_ASK} 张",
        )
    limit_bytes = get_settings().max_upload_mb * 1024 * 1024
    for ref in images:
        if not ref.data_url:
            continue
        # data URL 形如 data:<mime>;base64,<payload>，按 base64 解码后字节数估算
        payload_part = ref.data_url.partition(",")[2]
        estimated = len(payload_part) * 3 // 4
        if estimated > limit_bytes:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"IMAGE_TOO_LARGE: 单图上限 {get_settings().max_upload_mb}MB",
            )


def _sse(event: str, data: dict) -> str:
    """构造 SSE 事件块。data 用 JSON 序列化。"""
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False, default=str)}\n\n"


@router.post("/chat/ask")
async def chat_ask(
    payload: ChatAskRequest,
    user: CurrentUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_db),
    _: None = Depends(check_chat_rate_limit),
) -> StreamingResponse:
    # ── M7 带图提问：形态判定 + 入参校验（StreamingResponse 之前，可 400）──
    # 图片路径过全仓唯一形态判定点（记 rag.router.decision 指标，INV-M-01）；
    # 纯文本是聊天缺省形态，直接用枚举，不调 detect（无显式信号会抛 UnknownModalityError）
    modality = detect(has_images=True) if payload.images else Modality.TEXT
    _validate_images(payload.images)

    # admin 豁免 G2 库级授权校验；普通用户求交集（手册 §3.2.7 G2）
    if user.clearance >= 40:
        effective_kb_ids = list(payload.kb_ids)
    else:
        effective_kb_ids = [
            kb for kb in payload.kb_ids if kb in user.authorized_kb_ids
        ]
    if not effective_kb_ids:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="FORBIDDEN")
    kb_rows = (
        await session.execute(
            select(KnowledgeBase).where(
                KnowledgeBase.id.in_(effective_kb_ids),
                KnowledgeBase.tenant_id == user.tenant_id,
            )
        )
    ).scalars().all()
    if len(kb_rows) != len(effective_kb_ids):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="部分知识库不存在")

    # ── M6 续篇：敏感词输入侧检查（feature flag 守护）─────────
    # 命中即拒答（走 refused 事件，不留替换痕迹）
    # 检查放在会话/消息创建之前，避免污染历史
    sensitive_enabled = await flag_service.is_enabled(
        user.tenant_id, decisions.SENSITIVE_FEATURE_KEY, user.dept_path, user.user_id,
    ) if decisions.SENSITIVE_FILTER_ENABLED else False
    if sensitive_enabled:
        hit, word = await sensitive_service.check_input(payload.question, user.tenant_id)
        if hit:
            # 不创建会话/消息，直接返回 refused 流
            async def _sensitive_refused_stream() -> AsyncIterator[str]:
                yield _sse("meta", {
                    "conversation_id": None,
                    "message_id": None,
                    "stage": "blocked",
                    "trace_id": trace.get_trace_id(),
                    "modality": modality.value,
                })
                yield _sse("refused", {
                    "reason": "SENSITIVE_INPUT",
                    "message": "问题包含敏感词，已被拦截",
                })
                await audit.record(
                    user.tenant_id, user.user_id, "chat.sensitive.block",
                    object_type="conversation", object_id=None,
                    detail={"word": word, "side": "input"},
                )
            return StreamingResponse(
                _sensitive_refused_stream(),
                media_type="text/event-stream",
                headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
            )

    # 复用或创建 conversation
    conversation_id = payload.conversation_id
    if conversation_id is not None:
        conv = await session.get(Conversation, conversation_id)
        if conv is None or conv.tenant_id != user.tenant_id or conv.user_id != user.user_id:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="会话不存在")
    else:
        conv = Conversation(
            tenant_id=user.tenant_id,
            user_id=user.user_id,
            title=payload.question[:50],
        )
        session.add(conv)
        await session.flush()
        conversation_id = conv.id

    # 写 user message，并预占位 assistant message（同一事务提交）。
    # meta 下发的 message_id 立即指向真实行：流式中断/异常也不会留下无主引用，
    # 流结束后由 _persist_assistant 按 id 更新这一占位行。
    message_id = uuid.uuid4()
    user_msg = Message(
        tenant_id=user.tenant_id,
        conversation_id=conversation_id,
        role="user",
        content=payload.question,
    )
    assistant_placeholder = Message(
        id=message_id,
        tenant_id=user.tenant_id,
        conversation_id=conversation_id,
        role="assistant",
        content="",
        citations=[],
        meta={"generating": True},
    )
    session.add_all([user_msg, assistant_placeholder])
    await session.commit()
    await session.refresh(conv)

    async def event_stream() -> AsyncIterator[str]:
        import time as _time
        _t0 = _time.monotonic()
        try:

            # meta 事件（M7 起增补 modality，事件顺序不变：meta→stage→citations→delta...→done/refused）
            yield _sse("meta", {
                "conversation_id": str(conversation_id),
                "message_id": str(message_id),
                "stage": "retrieving",
                "trace_id": trace.get_trace_id(),
                "modality": modality.value,
            })

            # ── M7 带图提问：视觉抽取 → 与原 question 拼接后进既有链路 ─────
            # ★ 不落库（INV-M-03）：转录文本只存在于本轮内存，不调 upload_document、
            #   不建 Document / ParseJob；user 消息 content 仍是用户原始问题。
            # ★ 异常直抛后映射 refused（INV-M-04）：MUST NOT 退回"只看文本凭先验作答"。
            question_for_pipeline = payload.question
            if modality is Modality.IMAGE:
                try:
                    # ≤3 张并行抽取（gather 保序，首个异常直抛）
                    image_blocks = await asyncio.gather(
                        *(extract_text_from_image(ref) for ref in payload.images)
                    )
                except VisionNoContentError:
                    logger.info("chat.vision: 图中无可读内容，拒答")
                    yield _sse("refused", {
                        "reason": "IMAGE_NO_CONTENT",
                        "message": "图片中未识别到可读内容",
                    })
                    await _persist_assistant(
                        conversation_id, user, message_id,
                        text="", refused=True, citations=[],
                        meta={"refused": True, "reason": "IMAGE_NO_CONTENT",
                              "modality": modality.value},
                    )
                    await audit.record(
                        user.tenant_id, user.user_id, "chat.ask",
                        object_type="conversation", object_id=str(conversation_id),
                        detail={
                            "question": payload.question,
                            "kb_ids": [str(kb) for kb in effective_kb_ids],
                            "doc_ids": [],
                            "refused": True,
                            "refuse_reason": "IMAGE_NO_CONTENT",
                            "image_count": len(payload.images),
                        },
                    )
                    return
                except VisionError as exc:
                    logger.warning("chat.vision: 视觉链路不可用: %s", exc)
                    yield _sse("refused", {
                        "reason": "VISION_UNAVAILABLE",
                        "message": "图片理解服务暂时不可用，请稍后重试",
                    })
                    await _persist_assistant(
                        conversation_id, user, message_id,
                        text="", refused=True, citations=[],
                        meta={"refused": True, "reason": "VISION_UNAVAILABLE",
                              "modality": modality.value},
                    )
                    await audit.record(
                        user.tenant_id, user.user_id, "chat.ask",
                        object_type="conversation", object_id=str(conversation_id),
                        detail={
                            "question": payload.question,
                            "kb_ids": [str(kb) for kb in effective_kb_ids],
                            "doc_ids": [],
                            "refused": True,
                            "refuse_reason": "VISION_UNAVAILABLE",
                            "image_count": len(payload.images),
                        },
                    )
                    return
                transcripts = "\n\n".join(
                    f"[图片{i + 1}转录]\n{block.text}"
                    for i, block in enumerate(image_blocks)
                )
                question_for_pipeline = f"{payload.question}\n\n{transcripts}"
                logger.info("chat.vision: %d 张图转录完成，拼接后进既有链路",
                            len(image_blocks))

            # ── Query 改写（检索前，用历史做指代消解）───────────
            # 提前读历史消息（用于改写指代消解 + 后续生成复用，避免读两次 DB）
            # DB 层只做硬上限（500 行），后续各阶段按 token budget 裁剪
            rewrite_history: list[dict] = []
            async with SessionLocal() as rewrite_session:
                rewrite_rows = (await rewrite_session.execute(
                    select(Message)
                    .where(
                        Message.conversation_id == conversation_id,
                        Message.role.in_(["user", "assistant"]),
                    )
                    .order_by(Message.created_at.desc())
                    .limit(decisions.HISTORY_FETCH_MAX_ROWS)
                )).scalars().all()
                # 按时间正序，跳过最新那条（当前正在处理的 user message）
                for m in reversed(rewrite_rows[:-1]):
                    if m.role == "assistant" and not m.content:
                        continue
                    rewrite_history.append({"role": m.role, "content": m.content})

            # 评分 + 改写（一次 LLM 调用完成，score >= 90 直接跳过）
            # 带图时用拼接后的问题（含转录文本），改写模型才能消解对图片内容的指代
            retrieve_query, rewrite_status, rewrite_score = await rewrite_query(
                question_for_pipeline, rewrite_history, user.tenant_id,
            )
            if rewrite_status == "rewritten":
                logger.info("chat.rewrite: score=%d, '%s' → '%s'",
                            rewrite_score, payload.question[:50], retrieve_query[:50])
            else:
                logger.info("chat.rewrite: score=%d, status=%s, 使用原始问题",
                            rewrite_score, rewrite_status)

            # 检索（用改写后的 query，原始 question 留给 generation）
            # ★ 走链式检索入口：按 rewrite_score 复杂度信号决定是否拆解为多轮检索，
            #   拆解不可用时内部降级单趟 —— 对下游是同一个 RetrievalResult 契约
            _t_retrieve = _time.monotonic()
            async with SessionLocal() as retrieve_session:
                retrieval = get_retrieval_service()
                try:
                    result = await retrieval.retrieve_chain(
                        retrieve_session,
                        retrieve_query,  # 改写后的 query 只用于检索
                        user.tenant_id,
                        # 必须传 effective_kb_ids（含 admin G2 豁免 + 前端所选库交集），
                        # 传 user.authorized_kb_ids 会导致 admin 豁免失效、且检索范围
                        # 扩大到用户全部已授权库而非本次选择的库
                        effective_kb_ids,
                        user.clearance,
                        user.subjects,
                        rewrite_score=rewrite_score,  # 复杂度信号（复用改写分）
                    )
                except Exception:
                    logger.exception("检索失败")
                    yield _sse("refused", {
                        "reason": "VECTOR_STORE_DOWN",
                        "message": "知识库暂时不可用，请稍后重试",
                    })
                    return

            # 链式检索：多轮时补一个 stage 事件（前端 SSE switch 无 stage 分支，
            # 未知 stage 会被忽略，新增取值安全）—— 先于 retrieving 告知走了多轮
            if result.chain_mode.startswith("multi"):
                yield _sse("stage", {"stage": "chain", "mode": result.chain_mode})
            yield _sse("stage", {"stage": "retrieving", "ms": result.stage_ms.get("retrieving", 0)})

            _retrieve_ms = int((_time.monotonic() - _t_retrieve) * 1000)
            logger.info("chat.retrieve", extra={
                "user_id": str(user.user_id),
                "retrieve_ms": _retrieve_ms,
                "chunks_found": len(result.chunks),
                "refused": result.refused,
                "chain_mode": result.chain_mode,
            })

            if result.refused:
                yield _sse("refused", {
                    "reason": result.refuse_reason,
                    "message": "知识库中未找到相关内容",
                })
                # 持久化拒答 assistant message
                await _persist_assistant(
                    conversation_id, user, message_id,
                    text="", refused=True, citations=[],
                    meta={"stage_ms": result.stage_ms, "refused": True},
                    chain_mode=result.chain_mode,
                )
                # 审计：拒答也记录（refused=True 无引用）
                await audit.record(
                    user.tenant_id, user.user_id, "chat.ask",
                    object_type="conversation", object_id=str(conversation_id),
                    detail={
                        "question": payload.question,
                        "kb_ids": [str(kb) for kb in effective_kb_ids],
                        "doc_ids": [],
                        "refused": True,
                        # 各阶段耗时快照：便于只看审计就能定位慢在哪一环
                        "stage_ms": result.stage_ms,
                    },
                )
                _total_ms = int((_time.monotonic() - _t0) * 1000)
                logger.info("chat.ask.complete", extra={
                    "user_id": str(user.user_id),
                    "total_ms": _total_ms,
                    "retrieve_ms": _retrieve_ms,
                    "refused": True,
                    "refuse_reason": result.refuse_reason,
                    "chunks_count": 0,
                    "conversation_id": str(conversation_id),
                })
                # M6 可观测：指标持久化（供 P95 聚合查询）
                await audit.record(
                    user.tenant_id, user.user_id, "chat.ask.metric",
                    object_type="conversation", object_id=str(conversation_id),
                    detail={
                        "total_ms": _total_ms,
                        "retrieve_ms": _retrieve_ms,
                        "refused": True,
                        "refuse_reason": result.refuse_reason,
                        "chunks_count": 0,
                    },
                )
                return

            # 生成（含 L3 校验，generation 统一处理 LLM 异常）
            # 1. 复用改写阶段提前读的历史（避免重复 DB 查询），做历史压缩
            history: list[dict] = list(rewrite_history)

            # 不再按条数硬压缩（DB 已放开到 500 行），统一交给后面的
            # ensure_context_within_limit 按 token budget 裁剪 + 超限压缩

            # 3. 读用户画像
            user_memory: dict = {}
            async with SessionLocal() as mem_session:
                u = await mem_session.get(User, user.user_id)
                if u and u.memory:
                    user_memory = u.memory or {}

            # ── M6 续篇：配额检查 + 四级降级（feature flag 守护）─────
            # feature flag 关闭 → 跳过配额检查，用原 history/memory
            # feature flag 开启 → 调 check_and_degrade 返回裁剪方案
            # 软阈值 90% 触发降级，硬超限直接 refused
            memory_prompt = build_memory_prompt(user_memory)

            # ── Context 窗口硬约束 ────────────────────────────────
            # system prompt = qa_system（注册表生效版本）+ memory_prompt，
            # 与实际生成阶段拼装口径保持一致
            system_for_estimate = await build_system_prompt(memory_prompt, False, user.tenant_id)
            # 表格决策 A1'：回捞片追加在命中片尾部 —— 编号 / citations / 配额裁剪
            # 沿用既有机制（L4 按位置裁，超限时回捞片先于命中片被裁，优先级正确）
            current_chunks = list(result.chunks) + list(result.table_recall_chunks)

            # 先估算总 token 数，超限则逐轮压缩 history + 裁剪 chunks
            limit_cap = decisions.CONTEXT_WINDOW_LIMIT_TOKENS - decisions.CONTEXT_OUTPUT_RESERVE_TOKENS
            chunks_text_for_est = "\n\n".join(
                f"[{i+1}] {c.content}" for i, c in enumerate(current_chunks)
            )
            est_before = estimate_context_tokens(
                system_for_estimate, memory_prompt, history,
                chunks_text_for_est, question_for_pipeline,
            )
            if est_before > limit_cap:
                history, current_chunks = await ensure_context_within_limit(
                    system_for_estimate, memory_prompt, history,
                    chunks_text_for_est, question_for_pipeline, current_chunks,
                    user.tenant_id,
                )
                logger.warning("chat.context.compressed: est_before=%d → history=%d, chunks=%d (limit=%d)",
                               est_before, len(history), len(current_chunks), limit_cap)

                # ── 压缩次数追踪 ─────────────────────────────
                # 同一个 conversation 内累计压缩次数，超阈值发 SSE 提示
                async with SessionLocal() as comp_session:
                    conv_for_comp = await comp_session.get(Conversation, conversation_id)
                    if conv_for_comp is not None:
                        conv_for_comp.compression_count = (conv_for_comp.compression_count or 0) + 1
                        await comp_session.commit()
                        current_count = conv_for_comp.compression_count
                        logger.info("chat.compression.count: conv=%s, count=%d",
                                    conversation_id, current_count)
                        if current_count >= decisions.COMPRESSION_WARN_THRESHOLD:
                            # 发 SSE 提示事件（在 citations 之前，前端可提前显示）
                            yield _sse("context_warning", {
                                "compression_count": current_count,
                                "threshold": decisions.COMPRESSION_WARN_THRESHOLD,
                                "need_new_conversation": True,
                                "message": "对话上下文已被压缩多次，建议新开对话重置上下文以获得更好的回答效果。",
                            })

            degraded_from = "none"
            effective_chunks = current_chunks  # context 检查后的 chunks 作为初始值
            quota_enabled = await flag_service.is_enabled(
                user.tenant_id, decisions.QUOTA_FEATURE_KEY, user.dept_path, user.user_id,
            )
            if quota_enabled:
                plan = await quota_service.check_and_degrade(
                    user.tenant_id, user.user_id,
                    question_for_pipeline, history, memory_prompt, effective_chunks,
                )
                if plan.refused:
                    yield _sse("refused", {
                        "reason": plan.refuse_reason,
                        "message": "今日配额已用尽，请明日再试",
                    })
                    await _persist_assistant(
                        conversation_id, user, message_id,
                        text="", refused=True, citations=[],
                        meta={
                            "stage_ms": result.stage_ms,
                            "refused": True,
                            "reason": plan.refuse_reason,
                        },
                        chain_mode=result.chain_mode,
                    )
                    await audit.record(
                        user.tenant_id, user.user_id, "chat.quota.exceeded",
                        object_type="conversation", object_id=str(conversation_id),
                        detail={"reason": plan.refuse_reason},
                    )
                    return
                # 用降级方案替换原参数
                history = plan.history
                memory_prompt = plan.memory_prompt
                effective_chunks = plan.chunks
                degraded_from = plan.degraded_from
                if degraded_from != "none":
                    logger.info("chat.quota.degraded", extra={
                        "user_id": str(user.user_id),
                        "level": degraded_from,
                        "history_kept": len(history),
                        "chunks_kept": len(effective_chunks),
                    })

            # ── 注入防护（§4.21 输入侧防线）──
            # 命中**不丢弃**片段（制度文本里「请忽略」是正常措辞），只标记降权 + 打指标。
            # 必须在 citations 构造之前执行 —— 降权会改变顺序，
            # 而 citations 的 [n] 编号必须与生成 prompt 中的编号一致。
            injection_hits = injection_guard.scan_chunks(effective_chunks)
            if injection_hits:
                effective_chunks = injection_guard.demote(effective_chunks, injection_hits)

            # citations 基于 effective_chunks 构造：编号必须与生成 prompt 中的 [n] 一致，
            # 否则配额裁剪后 prompt 重编号会与前端引用载荷错位。MUST 在 delta 之前下发。
            citations_payload = [
                {
                    "n": i + 1,
                    "chunk_id": str(c.chunk_id),
                    "doc_id": str(c.document_id),
                    "filename": c.filename,
                    "heading_path": c.heading_path,
                    "page_no": c.page_no,
                    "score": c.display_score,
                    "snippet": c.content[:200],
                    # M8-P3b 附件：原始文件下载链接（WU-M-19 鉴权代理端点，
                    # 相对路径满足 INV-M-16 —— 禁止直出 MinIO 地址）
                    "raw_url": f"/api/documents/{c.document_id}/raw",
                }
                for i, c in enumerate(effective_chunks)
            ]
            yield _sse("citations", {"citations": citations_payload})

            # 审计：谁在何时问了什么、引用到哪些文档（手册 F4 / M5 任务 3）
            await audit.record(
                user.tenant_id, user.user_id, "chat.ask",
                object_type="conversation", object_id=str(conversation_id),
                detail={
                    "question": payload.question,
                    "kb_ids": [str(kb) for kb in effective_kb_ids],
                    "doc_ids": sorted({c["doc_id"] for c in citations_payload}),
                    "refused": False,
                    # 带图提问留痕（图片本体与转录文本不落库，只记张数）
                    "image_count": len(payload.images),
                    # 各阶段耗时快照：便于只看审计就能定位慢在哪一环
                    "stage_ms": result.stage_ms,
                    # 口径快照：本条答案由哪个 contract / 模型 / 提示词版本产出（方案二）
                    # chain_mode 为本次检索侧链式推理模式（off / single / multi:N）
                    "caliber": await snapshot_for_message(user.tenant_id, result.chain_mode),
                },
            )

            # 4. 生成（注入画像 + 压缩后的历史 + 可能裁剪后的 chunks）
            #    真流式透传：LLM delta 经行级 grounding / 敏感词 / PII 后立即推给前端
            generation = get_stream_generation_service()
            gen_result = None  # 最终结果（done/refused 时由 stream 填充）
            final_text = ""
            final_stripped = 0
            final_suggestions: list[str] = []
            final_usage: dict = {}
            final_reasoning = ""
            # 首字延迟：从请求起点 _t0（含敏感词/改写/检索）到第一个非空 delta 的间隔
            _ttft_ms: int | None = None
            # 开 CoT 时，前端在收到首个 delta 之前要等"推理段"跑完（推理链不推送），
            # 这里先发一个 stage 事件，让前端有机会显示"思考中"。
            # 前端目前忽略未知 stage 值，所以不加处理也不会坏。
            if decisions.should_enable_cot(rewrite_score):
                yield _sse("stage", {"stage": "reasoning"})

            async for stream_evt in generation.stream_generate(
                question_for_pipeline, effective_chunks,
                history=history,
                memory_prompt=memory_prompt,
                tenant_id=user.tenant_id,
                rewrite_score=rewrite_score,
            ):
                if stream_evt.type == "delta":
                    # 以第一个**非空** delta 为准：空 delta 不代表用户看到内容
                    if stream_evt.text and _ttft_ms is None:
                        _ttft_ms = int((_time.monotonic() - _t0) * 1000)
                    yield _sse("delta", {"text": stream_evt.text})
                elif stream_evt.type == "refused":
                    gen_result = stream_evt
                    break
                elif stream_evt.type == "done":
                    gen_result = stream_evt
                    final_text = stream_evt.full_text
                    final_stripped = stream_evt.stripped_sentences
                    final_suggestions = stream_evt.suggestions
                    final_usage = stream_evt.usage
                    final_reasoning = stream_evt.reasoning

            if gen_result is not None and gen_result.refused:
                reason = gen_result.refuse_reason
                user_message = {
                    "NO_RELEVANT_CONTENT": "知识库中未找到相关内容",
                    "UNGROUNDED": "知识库中未找到相关内容",
                    "LLM_TIMEOUT": "响应较慢，请重试",
                    "LLM_ERROR": "生成服务异常，请稍后重试",
                    "SENSITIVE_OUTPUT": "回答内容包含敏感词，已被拦截",
                }.get(reason, "知识库中未找到相关内容")
                yield _sse("refused", {"reason": reason, "message": user_message})
                await _persist_assistant(
                    conversation_id, user, message_id,
                    text="", refused=True, citations=citations_payload,
                    meta={
                        "stage_ms": result.stage_ms,
                        "refused": True,
                        "reason": reason,
                        "grounding_stripped": gen_result.stripped_sentences,
                    },
                    chain_mode=result.chain_mode,
                )
                _total_ms = int((_time.monotonic() - _t0) * 1000)
                logger.info("chat.ask.complete", extra={
                    "user_id": str(user.user_id),
                    "total_ms": _total_ms,
                    "retrieve_ms": _retrieve_ms,
                    "refused": True,
                    "refuse_reason": reason,
                    "chunks_count": len(result.chunks),
                    "grounding_stripped": gen_result.stripped_sentences,
                    "conversation_id": str(conversation_id),
                })
                await audit.record(
                    user.tenant_id, user.user_id, "chat.ask.metric",
                    object_type="conversation", object_id=str(conversation_id),
                    detail={
                        "total_ms": _total_ms,
                        "retrieve_ms": _retrieve_ms,
                        "refused": True,
                        "refuse_reason": reason,
                        "chunks_count": len(result.chunks),
                        "grounding_stripped": gen_result.stripped_sentences,
                        **result.path_stats,
                    },
                )
                return

            # done — 先做不影响客户端的 Redis/DB 操作（网络延迟不影响流式）
            # 再 yield done，避免 yield 之后客户端已断开导致后续 await 失败
            # ── M6 续篇：配额用量上报（Redis 计数器累加）─────────
            try:
                await quota_service._incr_usage(
                    user.tenant_id, user.user_id,
                    tokens=final_usage.get("prompt_tokens", 0) + final_usage.get("completion_tokens", 0),
                )
            except Exception:
                logger.warning("quota._incr_usage 失败，忽略", exc_info=True)

            yield _sse("done", {
                "finish_reason": "stopped",
                "grounding": {"stripped_sentences": final_stripped},
                "usage": final_usage,
                "suggestions": final_suggestions,
            })

            # M6 可观测：记录端到端指标
            _total_ms = int((_time.monotonic() - _t0) * 1000)
            logger.info("chat.ask.complete", extra={
                "user_id": str(user.user_id),
                "total_ms": _total_ms,
                "ttft_ms": _ttft_ms,
                "retrieve_ms": _retrieve_ms,
                "refused": False,
                "refuse_reason": None,
                "chunks_count": len(result.chunks),
                "grounding_stripped": final_stripped,
                "prompt_tokens": final_usage.get("prompt_tokens", 0),
                "completion_tokens": final_usage.get("completion_tokens", 0),
                "conversation_id": str(conversation_id),
            })
            await audit.record(
                user.tenant_id, user.user_id, "chat.ask.metric",
                object_type="conversation", object_id=str(conversation_id),
                detail={
                    "total_ms": _total_ms,
                    "ttft_ms": _ttft_ms,
                    "retrieve_ms": _retrieve_ms,
                    "refused": False,
                    "refuse_reason": None,
                    "chunks_count": len(result.chunks),
                    "grounding_stripped": final_stripped,
                    "prompt_tokens": final_usage.get("prompt_tokens", 0),
                    "completion_tokens": final_usage.get("completion_tokens", 0),
                    **result.path_stats,
                },
            )

            # 持久化
            await _persist_assistant(
                conversation_id, user, message_id,
                text=final_text, refused=False, citations=citations_payload,
                meta={
                    "stage_ms": result.stage_ms,
                    "grounding_stripped": final_stripped,
                    "usage": final_usage,
                    "suggestions": final_suggestions,
                    "quota_degraded_from": degraded_from,
                    # 思维链只落库、不外发（decisions.COT_REASONING_EXPOSED = False）。
                    # 落库前**必须脱敏**：推理里常直接抄 <context> 内容，
                    # 那里面可能带手机号/身份证（正文走 mask_pii，推理链不经过那条路）。
                    "reasoning": mask_pii(final_reasoning) if final_reasoning else "",
                },
                chain_mode=result.chain_mode,
            )

            # 后台更新用户画像（不阻塞流式响应）
            asyncio.create_task(_update_user_memory(user.user_id, payload.question, final_text))

        except Exception as exc:
            # 顶层兜底：event_stream 任何未捕获异常都会导致已持久化的 user 消息
            # 没有对应的 assistant 回复（孤立 user 消息）。这里持久化一个失败
            # 的 assistant 消息，保持对话完整性。
            logger.exception("chat.event_stream 未捕获异常")
            yield _sse("refused", {
                "reason": "INTERNAL_ERROR",
                "message": "服务暂时异常，请稍后重试",
            })
            try:
                await _persist_assistant(
                    conversation_id, user, message_id,
                    text="", refused=True, citations=[],
                    meta={
                        "refused": True,
                        "reason": "INTERNAL_ERROR",
                        "error": f"{type(exc).__name__}: {exc}",
                    },
                )
            except Exception:
                logger.exception("持久化失败 assistant 消息也失败了")


    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


async def _persist_assistant(
    conversation_id: uuid.UUID,
    user: CurrentUser,
    message_id: uuid.UUID,
    text: str,
    refused: bool,
    citations: list[dict],
    meta: dict,
    chain_mode: str | None = None,
) -> None:
    """更新流开始前占位的 assistant message（citations 存快照，手册 §4.2.9）。

    占位行已在 chat_ask 中与 user message 同事务提交，此处按 message_id 定位更新；
    meta 去掉 generating 标记并写入 refused 等最终状态。
    chain_mode 为本次检索侧链式推理模式（本请求可变），并入口径快照。
    """
    async with SessionLocal() as session:
        msg = await session.get(Message, message_id)
        if msg is None:
            # 占位行与 user message 同事务提交，正常必存在；缺失说明数据被外部清理
            raise RuntimeError(f"assistant 占位消息不存在: {message_id}")
        msg.content = text
        msg.citations = citations
        # 口径快照（方案二）：记录本条答案由哪个 contract / 模型 / 提示词版本产出。
        # 在此**单点注入** —— 正常 / 拒答各条落库路径自动带上，无需逐处调用点传参。
        caliber = await snapshot_for_message(user.tenant_id, chain_mode)
        # trace_id 在此单点注入：正常 / 拒答各条落库路径自动带上，前端历史里可复制
        msg.meta = {
            **meta, "refused": refused, "caliber": caliber,
            "trace_id": trace.get_trace_id(),
        }
        await session.commit()


async def _update_user_memory(
    user_id: uuid.UUID,
    user_msg: str,
    assistant_msg: str,
) -> None:
    """后台更新用户画像。静默失败不阻塞主流程。"""
    try:
        async with SessionLocal() as session:
            u = await session.get(User, user_id)
            if u is None:
                return
            new_memory = await extract_facts(
                user_msg,
                assistant_msg,
                u.memory or {},
                trace_id=trace.get_trace_id() or "",
                tenant_id=u.tenant_id,
            )
            u.memory = new_memory
            await session.commit()
    except Exception:
        logger.exception("用户画像更新失败")


# ── 会话管理接口（多轮对话历史） ────────────────────────────────


@router.get("/conversations")
async def list_conversations(
    user: CurrentUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_db),
) -> list[dict]:
    """当前用户的会话列表（最近 50 条，按最后消息时间倒序）。"""
    # 子查询：每个会话最后一条消息的时间
    from sqlalchemy import func, desc

    last_msg_sq = (
        select(
            Message.conversation_id,
            func.max(Message.created_at).label("last_at"),
        )
        .group_by(Message.conversation_id)
        .subquery()
    )
    rows = (
        await session.execute(
            select(
                Conversation.id,
                Conversation.title,
                Conversation.created_at,
                func.coalesce(last_msg_sq.c.last_at, Conversation.created_at).label("last_at"),
            )
            .outerjoin(last_msg_sq, last_msg_sq.c.conversation_id == Conversation.id)
            .where(Conversation.tenant_id == user.tenant_id, Conversation.user_id == user.user_id)
            .order_by(desc("last_at"))
            .limit(50)
        )
    ).all()
    return [
        {
            "id": str(r.id),
            "title": r.title or "新对话",
            "created_at": r.created_at.isoformat(),
            "last_at": r.last_at.isoformat() if r.last_at else None,
        }
        for r in rows
    ]


@router.get("/conversations/{conversation_id}/messages")
async def get_conversation_messages(
    conversation_id: uuid.UUID,
    user: CurrentUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_db),
) -> list[dict]:
    """加载某会话的全部消息（按时间正序）。

    过滤 meta.generating=true 的占位行：流式期间客户端断连会残留未落定的空
    assistant 消息，此类消息视为本次回答作废，不在历史中展示。
    """
    conv = await session.get(Conversation, conversation_id)
    if conv is None or conv.tenant_id != user.tenant_id or conv.user_id != user.user_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="会话不存在")
    rows = (
        await session.execute(
            select(Message)
            .where(
                Message.conversation_id == conversation_id,
                # IS NOT true：generating 缺失(NULL)或 false 都保留，仅 true 被过滤
                Message.meta["generating"].as_boolean().isnot(True),
            )
            .order_by(Message.created_at)
        )
    ).scalars().all()
    return [
        {
            "id": str(m.id),
            "role": m.role,
            "content": m.content,
            "citations": m.citations,
            "meta": m.meta,
            "created_at": m.created_at.isoformat(),
        }
        for m in rows
    ]


@router.delete("/conversations/{conversation_id}")
async def delete_conversation(
    conversation_id: uuid.UUID,
    user: CurrentUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_db),
) -> None:
    """删除会话（级联删除消息）。"""
    conv = await session.get(Conversation, conversation_id)
    if conv is None or conv.tenant_id != user.tenant_id or conv.user_id != user.user_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="会话不存在")
    await session.delete(conv)
    await session.commit()
