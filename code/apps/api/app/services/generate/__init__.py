"""生成服务 —— prompt 模板 + LLM 流式 + L3 校验 + 引用回填（手册 §3.1 L2 + §5.2）。

流程：
  1. 构造 prompt（<context> + 引用编号 + 四条约束）
  2. 调 LLM 流式生成（服务端缓冲完整文本）
  3. L3 出口校验（grounding.check_grounding）
  4. 引用回填（从 chunk 取 filename/heading_path/page_no）
  5. 返回 GenerationResult，由 API 层切成 deltas 推送

★ 不做 fallback：LLM 挂了抛错，不降级用模板。
★ 模拟流式：API 层把最终文本按 ~30 字符/秒切片推送（用户偏好）。
"""
from __future__ import annotations

import logging
import re
import uuid
from dataclasses import dataclass, field

from app.config import decisions
from app.config.prompts import NO_ANSWER_TEXT
from app.infra import metrics
from app.services import prompt_registry as prompt_registry_service
from app.services.grounding import check_grounding, check_line
from app.services.llm import LLMError, LLMTimeout, get_llm_service
from app.services.mask import mask_pii
from app.services.retrieve.base import RetrievedChunk

logger = logging.getLogger(__name__)

# 拒答文案（NO_ANSWER_TEXT）来自提示词注册表 app/config/prompts.py，是契约单一来源。
# ★ 生成层必须把这句话识别为 refused=True（见 generate() 末尾），
#   否则会以 delta 形式当成正常回答流出，违反 chat.py 的
#   「拒答 MUST 发 refused 事件，MUST NOT 用 delta 发拒答文案」。
#
# 提示词统一来自注册表：
#   · 约束段（四条约束 + 安全约束 + 拒答文案）锁定在代码里，不可在线改
#   · 其余可编辑区可落库切版本；未配置 active 版本时用内置默认
# ★ 思维链用 prompt 契约而不是厂商参数（如 DeepSeek 的 thinking）：
#   本项目定位"任意 OpenAI 兼容接口"，只有某一家认的字段会在私有化换模型时
#   **静默失效**（参数被忽略、CoT 悄悄不生效，没人会发现）。
#   代价是遵循度不如原生 thinking —— 所以生成层**必须**对"标签没闭合"兜底，
#   不能假设模型一定听话（见 StreamGenerationService 里的 in_reasoning 处理）。

# ── 拒答识别：归一化匹配 ─────────────────────────────────────────────
# 早期实现是 `text.strip().strip('"') == NO_ANSWER_TEXT` 的**严格等值**比较，
# 只要模型给契约句加一层 markdown 或补一句解释就判不出来：
#   · 「知识库中未找到相关内容（现有资料仅含 Java 版）」
#   · 「知识库中未找到 **XYZ-2000 整机保修期** 的相关内容」
#   · 「**知识库中未找到杭州仓的库存清单。** 唯一的库存类文档是……」
# 漏判有两层代价：
#   产品层——不发 refused 事件，前端把「没找到 + 8 条不相关引用」当正常回答渲染；
#   度量层——拒答率被系统性低估（实测 87/96 vs 真实 95/96）。
# 因此改为「归一化 → 首句语义匹配」：只认首句，避免把
# 「知识库中收录了员工手册，其中规定……」这类正常回答误判为拒答。
_MARKUP_RE = re.compile(r"[*_`#>~|]")
_CITE_RE = re.compile(r"\[\d+\]")
_ZW_RE = re.compile(r"[\u200b-\u200f\ufeff]")

# 首句拒答语义：知识库(中/里) + 否定词 + （≤20 字修饰）+ 拒答动词
# 覆盖三类真实写法：
#   「知识库中没有直接规定……」（中间插"直接"）
#   「知识库中没有关于"策略模式适用场景"的正文说明」（中间插一整个定语）
#   「知识库中未找到 **XYZ-2000** 的相关内容」
_REFUSAL_HEAD_RE = re.compile(
    r"^(?:抱歉[，,]?|对不起[，,]?|不好意思[，,]?)?"
    r"知识库(?:中|里|内)?"
    r"(?:未|没有|无法|没能|尚未|暂未)"
    r"[^。！？\n]{0,20}?"
    r"(?:找到|检索到|收录|包含|涵盖|提及|说明|规定|提供)"
)


def _normalize_for_refusal(text: str) -> str:
    """去掉 markdown 标记、引用编号、零宽字符与全部空白，便于做前缀/首句匹配。"""
    t = _MARKUP_RE.sub("", text)
    t = _CITE_RE.sub("", t)
    t = _ZW_RE.sub("", t)
    t = t.replace("\u3000", "")
    return re.sub(r"\s+", "", t)


def is_no_answer_output(text: str) -> bool:
    """生成结果是否为拒答。命中任一条件即判拒答：

    1. 归一化后以 NO_ANSWER_TEXT 开头（契约原句，允许尾随补充说明）
    2. 首句呈现「知识库中未找到/没有…」的拒答语义（覆盖加粗、插入主语、
       换说法、给替代信息等变体）
    """
    if not text or not text.strip():
        return False
    normalized = _normalize_for_refusal(text)
    if normalized.startswith(NO_ANSWER_TEXT):
        return True
    head = re.split(r"[。！？；]", normalized, maxsplit=1)[0]
    return bool(_REFUSAL_HEAD_RE.match(head))


def is_unattributed_output(text: str, citation_count: int) -> bool:
    """检索到了内容，但答案里一条有效引用都没有 —— 判为脱离检索结果作答。

    提示词契约要求结论后标 `[n]`。零引用意味着模型在凭先验自由发挥，
    而不是在概括 <context>。真实案例：问「帮我写一段快速排序的 Python 代码」，
    语料只有 Java 版，模型却直接吐了一份 Python 实现（`#### 结论` 开头，
    全程无引用），既绕过了引用校验（无引用的行一律保留），也绕过了拒答契约。

    兜底放在生成层出口而不是提示词：温度再低也有跑偏的时候，
    「有没有引用」是当前上下文里唯一一个不依赖模型自觉的硬信号。
    """
    if citation_count <= 0:
        return False
    return not _CITE_RE.search(text)


# 「绕道作答」的典型形态：模型不敢直接说"没找到"，改用一个小标题把
# 「知识库中的相关信息」包装成答案交出去。两条真实样本：
#   「#### 知识库中关于上下文切换的说明」→ 正文讲的是定义，没答"有哪些开销"
#   「#### 知识库中的相关信息」→ 正文讲的是别的制度的餐补
# 这类输出带引用、也够"礼貌"，前面的规则一条都拦不住，只能认形态。
# 注意动词表刻意不含「规定」：`#### 知识库中的相关规定` 通常是真的在答（如考勤制度）。
_REFUSAL_TITLE_RE = re.compile(
    r"^#{1,6}\s*(?:知识库|资料库?)(?:中|里)?(?:关于|的)?"
    r"[^\n]{0,30}?"
    r"(?:说明|信息|内容|记录|资料)(?![库])"
)


def is_offtopic_fallback(text: str) -> bool:
    """识别「以『知识库中的相关信息』这类标题开头、用相关性内容替代答案」的输出。"""
    if not text or not text.strip():
        return False
    return bool(_REFUSAL_TITLE_RE.match(text.lstrip()))


async def build_system_prompt(
    memory_prompt: str,
    cot_enabled: bool,
    tenant_id: uuid.UUID | None = None,
) -> str:
    """拼装 system prompt：基础约束 → 用户画像 →（可选）思维链指令。

    CoT 指令放**最后**：离 user prompt 最近，模型的遵循度更高。
    """
    parts = [await prompt_registry_service.resolve_prompt("qa_system", tenant_id)]
    if memory_prompt:
        parts.append(memory_prompt)
    if cot_enabled:
        parts.append(await prompt_registry_service.resolve_prompt("qa_cot", tenant_id))
    return "".join(parts)


# 思维链剥离（非流式路径用；流式在 _flush_lines 里逐行处理）
_REASONING_BLOCK_RE = re.compile(r"<reasoning>(.*?)</reasoning>", re.DOTALL)
_REASONING_OPEN_RE = re.compile(r"<reasoning>")
_REASONING_CLOSE = "</reasoning>"


def _extract_reasoning(text: str, cot_enabled: bool) -> tuple[str, str]:
    """剥离思维链，返回 (正文, 推理链)。

    ★ 未闭合时（模型写了 <reasoning> 但忘了收尾标签）**不修改正文** ——
      整段原样返回。理由：漏出推理链只是"展示了不该展示的内容"，
      而"未闭合就全丢"会让用户看到空白回答 —— 那是功能性故障，
      比泄漏实现细节严重得多。这种情形记指标 `rag.cot.unclosed`，
      用来观察模型的格式遵循率（长期偏高就该考虑换回原生 thinking 参数）。
    """
    if not cot_enabled:
        return text, ""
    m = _REASONING_BLOCK_RE.search(text)
    if m:
        reasoning = m.group(1).strip()
        clean = (text[: m.start()] + text[m.end():]).strip()
        return clean, reasoning
    if _REASONING_OPEN_RE.search(text):
        metrics.incr("rag.cot.unclosed")
        logger.warning("思维链标签未闭合，正文按原样返回（推理链可能外泄）")
    return text, ""


_LIST_ITEM_RE = re.compile(r"(?<!\n)\s+(\d+\.\s)")

# 匹配 Markdown 小标题（#### / ### / ## / #），捕获小标题及其后的内容
_HEADING_RE = re.compile(r"(?<!\n)(#{1,6}\s+[^\n#]+?)(?=\s+\d+\.\s|\s*$)")

# 匹配段落之间挤在一起的情况：引用编号后跟中文/英文，且紧跟下一个引用编号或标题
# 例："...已超过 70%¹公司倡导开放、协作..." → "...已超过 70%¹\n\n公司倡导开放、协作..."
_REF_PARA_RE = re.compile(r"(\[\d+\])\s+(?=[^\s#\-*>\[\d])")

# 匹配 "小标题后直接跟编号" 的情况
# 例："#### 核心业务1. 自然语言处理" → "#### 核心业务\n\n1. 自然语言处理"
_HEADING_LIST_RE = re.compile(r"(#{1,6}\s+[^\n\d#]+)(\d+\.\s)")

# 解析 <followups> 块：从 LLM 输出末尾提取追问建议并从正文剥离
_FOLLOWUPS_RE = re.compile(r"\s*<followups>\s*(.*?)\s*</followups>\s*", re.DOTALL)


def _extract_followups(text: str) -> tuple[str, list[str]]:
    """从 LLM 输出中提取 <followups> 追问建议，返回 (干净正文, 建议列表)。"""
    m = _FOLLOWUPS_RE.search(text)
    if not m:
        return text, []
    lines = [ln.strip() for ln in m.group(1).splitlines() if ln.strip()]
    clean = _FOLLOWUPS_RE.sub("", text).rstrip()
    return clean, lines


def _ensure_line_breaks(text: str) -> str:
    """把 LLM 输出的挤在一起的内容按 Markdown 规则正确换行。

    处理：
    1. 小标题后补空行
    2. 小标题后直接跟数字编号 → 拆成两行
    3. 行内数字编号前补换行
    4. 引用编号后如果紧跟另一个内容 → 补段落分隔
    """
    if not text:
        return text
    text = text.replace("\r\n", "\n")

    # 1. 小标题后直接跟编号 → 拆开
    text = _HEADING_LIST_RE.sub(r"\1\n\n\2", text)

    # 2. 小标题后补空行（确保每个小标题独占一行且前后有间距）
    #    先把 "#### 标题后续内容" 改成 "#### 标题\n\n后续内容"
    text = re.sub(r"(#{1,6}\s+[^\n#]+)\s+(?=[^\s#\-\*>\d\[`])", r"\1\n\n", text)

    # 3. 行内数字编号前补换行（编号后必须跟空格，避免误切版本号）
    text = _LIST_ITEM_RE.sub(r"\n\1", text)

    # 4. 清理多余空行（3+ → 2）
    text = re.sub(r"\n{3,}", "\n\n", text)
    # 5. 清理编号项之间的多余空行（列表项之间不应有空行）
    text = re.sub(r"(\d+\.\s[^\n]+)\n\n(\d+\.\s)", r"\1\n\2", text)

    return text.strip()


def _build_context(chunks: list[RetrievedChunk]) -> str:
    """构造 <context> 块。每个 chunk 一个编号 [n]。"""
    parts: list[str] = []
    for index, chunk in enumerate(chunks, start=1):
        location = f"{chunk.filename}"
        if chunk.heading_path:
            location += f" / {chunk.heading_path}"
        if chunk.page_no is not None:
            location += f" / 第 {chunk.page_no} 页"
        parts.append(
            f"[{index}] {location}\n内容：{chunk.content}"
        )
    return "\n\n".join(parts)


def _build_user_prompt(query: str, chunks: list[RetrievedChunk]) -> str:
    context = _build_context(chunks)
    return f"<context>\n{context}\n</context>\n\n问题：{query}\n\n请依据 <context> 回答，每个结论后附 [n] 引用编号。"


@dataclass
class Citation:
    """引用回填结构（手册 §5.2 citations 字段）。"""

    n: int
    chunk_id: uuid.UUID
    doc_id: uuid.UUID
    filename: str
    heading_path: str
    page_no: int | None
    score: float


@dataclass
class GenerationResult:
    text: str
    raw_text: str
    stripped_sentences: int
    refused: bool
    refuse_reason: str
    citations: list[Citation] = field(default_factory=list)
    usage: dict = field(default_factory=dict)
    suggestions: list[str] = field(default_factory=list)
    # 思维链（开启时才有）。**不对外输出**（见 decisions.COT_REASONING_EXPOSED），
    # 只随消息落库供审计 —— 里面含试探性表述与内部规则，直接展示会泄漏实现细节。
    reasoning: str = ""


class GenerationService:
    async def generate(
        self,
        query: str,
        chunks: list[RetrievedChunk],
        history: list[dict] | None = None,
        memory_prompt: str = "",
        tenant_id: uuid.UUID | None = None,
        rewrite_score: int | None = None,
    ) -> GenerationResult:
        # 构造引用映射
        valid_ns: set[int] = set()
        citations: list[Citation] = []
        for index, chunk in enumerate(chunks, start=1):
            valid_ns.add(index)
            citations.append(
                Citation(
                    n=index,
                    chunk_id=chunk.chunk_id,
                    doc_id=chunk.document_id,
                    filename=chunk.filename,
                    heading_path=chunk.heading_path,
                    page_no=chunk.page_no,
                    score=chunk.display_score,
                )
            )

        # 构造 messages：system(+memory) → history → 当前 user prompt
        cot_enabled = decisions.should_enable_cot(rewrite_score)
        system_content = await build_system_prompt(memory_prompt, cot_enabled, tenant_id)
        messages: list[dict] = [{"role": "system", "content": system_content}]
        if history:
            messages.extend(history)
        messages.append({"role": "user", "content": _build_user_prompt(query, chunks)})

        # 调 LLM（流式生成，服务端缓冲完整文本）
        # 异常处理（手册 §3.4 故障 3/4）：
        #   · 首 token 超时 / 调用失败 + 无已输出 → refused=True
        #   · 中途报错 + 有已输出 → 保留部分文本（不 refused）
        llm = get_llm_service()
        raw_text_parts: list[str] = []
        llm_failed = False
        llm_fail_reason = ""
        try:
            async for delta in llm.stream_chat(messages, decisions.GENERATION_TEMPERATURE):
                raw_text_parts.append(delta)
        except LLMTimeout as exc:
            logger.warning("LLM 首 token 超时: %s", exc)
            llm_failed = True
            llm_fail_reason = "LLM_TIMEOUT"
        except LLMError as exc:
            logger.error("LLM 生成失败: %s", exc)
            llm_failed = True
            llm_fail_reason = "LLM_ERROR"

        raw_text = "".join(raw_text_parts)
        # 剥离思维链（开启 CoT 时模型会先输出 <reasoning>...</reasoning>）。
        # MUST 在 grounding 校验之前剥 —— 推理链里的 [n] 会被当成引用去校验，
        # 而那部分文字根本不该进正文。
        raw_text, reasoning = _extract_reasoning(raw_text, cot_enabled)
        if llm_failed and not raw_text:
            return GenerationResult(
                text="",
                raw_text="",
                stripped_sentences=0,
                refused=True,
                refuse_reason=llm_fail_reason,
                citations=citations,
            )

        # L3 出口校验
        grounding_result = check_grounding(raw_text, valid_ns)
        if grounding_result.refused:
            return GenerationResult(
                text="",
                raw_text=raw_text,
                stripped_sentences=grounding_result.stripped_sentences,
                refused=True,
                refuse_reason=grounding_result.refuse_reason,
                citations=citations,
                reasoning=reasoning,
            )

        # 从正文剥离 <followups> 追问建议
        clean_text, suggestions = _extract_followups(grounding_result.text)

        # 格式修复 + PII 脱敏（M5 任务 2）
        formatted = _ensure_line_breaks(clean_text)

        # ── M6 续篇：输出侧敏感词检查（feature flag 守护）─────
        # 命中即拒答（refused=True, refuse_reason="SENSITIVE_OUTPUT"）
        # 与 PII 脱敏职责不同：PII 是隐私数据打码（保留语义），
        # 敏感词是政策性禁用词，直接拒答，不留替换痕迹
        if decisions.SENSITIVE_FILTER_ENABLED and tenant_id is not None:
            # lazy import 避免 services/generate → services/sensitive 的潜在循环
            from app.services.sensitive import check_output
            hit, word = await check_output(formatted, tenant_id)
            if hit:
                logger.warning("SENSITIVE_OUTPUT hit: word=%s", word)
                return GenerationResult(
                    text="",
                    raw_text=raw_text,
                    stripped_sentences=grounding_result.stripped_sentences,
                    refused=True,
                    refuse_reason="SENSITIVE_OUTPUT",
                    citations=citations,
                    reasoning=reasoning,
                )

        if decisions.MASK_PII_ENABLED:
            formatted = mask_pii(formatted)

        # ★ 提示词契约闭环：LLM 只输出拒答文案 ⇒ 判定为拒答，而不是正常回答。
        #   覆盖模型对契约句的常见变体（加粗/插主语/补一句解释），见 is_no_answer_output。
        _is_no_answer = is_no_answer_output(formatted)
        # ★ 有上下文却零引用 ⇒ 模型脱离检索结果自由发挥（幻觉），同样按拒答处理。
        _unattributed = is_unattributed_output(formatted, len(citations))
        # ★ 用「知识库中的相关信息」当标题绕道作答 ⇒ 没答问题，按拒答处理。
        _offtopic = is_offtopic_fallback(formatted)
        _refused = _is_no_answer or _unattributed or _offtopic

        return GenerationResult(
            text="" if _refused else formatted,
            raw_text=raw_text,
            stripped_sentences=grounding_result.stripped_sentences,
            refused=_refused,
            refuse_reason=(
                "NO_RELEVANT_CONTENT" if _is_no_answer
                else "UNGROUNDED" if (_unattributed or _offtopic)
                else ""
            ),
            # 拒答时不返回引用：这些 chunk 已被判定不足以支撑回答，
            # 挂上去只会误导（用户点开引用看到的是不相关内容）
            citations=[] if _refused else citations,
            usage={"prompt_tokens": 0, "completion_tokens": len(raw_text)},
            suggestions=[] if _refused else suggestions,
            reasoning=reasoning,
        )


def get_generation_service() -> GenerationService:
    return GenerationService()


# ── 真流式生成（供 /chat/ask 透传，降低首字延迟）──────────────────


@dataclass
class StreamDelta:
    """流式事件。type=delta 时 text 为增量文本；type=refused/done 时 text 为空。"""

    type: str  # "delta" | "done" | "refused"
    text: str = ""
    refused: bool = False
    refuse_reason: str = ""
    stripped_sentences: int = 0
    full_text: str = ""
    suggestions: list[str] = field(default_factory=list)
    usage: dict = field(default_factory=dict)
    # 思维链（开启时才有）。不外发，由 done 事件带回给 API 层落库审计。
    reasoning: str = ""


# 匹配 <followups> 开始标签（流式中遇到即切换为收集追问模式）
_FOLLOWUPS_OPEN_RE = re.compile(r"<followups>")


class StreamGenerationService(GenerationService):
    """真流式生成：LLM delta 经行级 grounding / PII / 敏感词校验后立即透传。

    与非流式 generate 的差异：
      · 不缓冲完整回答，收到完整行就校验并推送，首字延迟 = LLM 首字延迟
      · grounding 按行做（check_line），无效引用行在推送前丢弃，前端不会看到后撤回
      · 敏感词按行检查，命中即中止并发 refused
      · <followups> 块不推给前端，只在 done 事件里返回 suggestions
    """

    async def stream_generate(
        self,
        query: str,
        chunks: list[RetrievedChunk],
        history: list[dict] | None = None,
        memory_prompt: str = "",
        tenant_id: uuid.UUID | None = None,
        rewrite_score: int | None = None,
    ):
        # 引用映射（同 generate）
        valid_ns: set[int] = set()
        citations: list[Citation] = []
        for index, chunk in enumerate(chunks, start=1):
            valid_ns.add(index)
            citations.append(
                Citation(
                    n=index,
                    chunk_id=chunk.chunk_id,
                    doc_id=chunk.document_id,
                    filename=chunk.filename,
                    heading_path=chunk.heading_path,
                    page_no=chunk.page_no,
                    score=chunk.display_score,
                )
            )

        cot_enabled = decisions.should_enable_cot(rewrite_score)
        system_content = await build_system_prompt(memory_prompt, cot_enabled, tenant_id)
        messages: list[dict] = [{"role": "system", "content": system_content}]
        if history:
            messages.extend(history)
        messages.append({"role": "user", "content": _build_user_prompt(query, chunks)})

        llm = get_llm_service()
        # 流式状态
        line_buf = ""          # 尚未换行的累积片段
        full_text = ""         # 已通过校验并推送给前端的累计正文
        stripped = 0
        in_followups = False
        followups_buf = ""
        in_reasoning = False   # 思维链区（收集不推送）
        reasoning_buf = ""
        llm_failed = False
        llm_fail_reason = ""
        aborted = False        # 敏感词命中后置 True，中止流

        async def _flush_lines(buf: str, is_final: bool = False):
            """把 buf 按行切分，完整行逐行校验并 yield delta。直接修改外层 line_buf。"""
            nonlocal line_buf, full_text, stripped, in_followups, followups_buf, aborted, in_reasoning, reasoning_buf
            pieces = buf.split("\n")
            if is_final:
                complete = pieces
                line_buf = ""
            else:
                complete = pieces[:-1]
                line_buf = pieces[-1]
            for line in complete:
                if aborted:
                    return

                # ── 思维链区：收集不推送 ──────────────────────────────
                # MUST 排在 followups 之前：reasoning 出现在回答**最前面**，
                # 是"头部丢弃"，与 followups 的"尾部丢弃"方向相反。
                # 「不推给前端」是硬要求（decisions.COT_REASONING_EXPOSED = False）——
                # 推理链含试探性表述与内部规则，展示会泄漏实现细节。
                if in_reasoning:
                    if _REASONING_CLOSE in line:
                        idx = line.index(_REASONING_CLOSE)
                        reasoning_buf += line[:idx]
                        in_reasoning = False
                        line = line[idx + len(_REASONING_CLOSE):]
                        if not line.strip():
                            continue
                        # 闭合标签同行之后还有正文 → 落到下面按正文处理
                    else:
                        reasoning_buf += line + "\n"
                        continue

                # 开标签（可能独占一行，也可能行内）
                m_reason = _REASONING_OPEN_RE.search(line)
                if m_reason:
                    pre = line[: m_reason.start()].strip()
                    rest = line[m_reason.end():]
                    if pre:
                        keep_pre, stripped_pre = check_line(pre, valid_ns)
                        if stripped_pre:
                            stripped += 1
                        elif keep_pre:
                            out_pre = await self._post_check(pre, tenant_id)
                            if out_pre is None:
                                yield StreamDelta(type="refused", refused=True, refuse_reason="SENSITIVE_OUTPUT", stripped_sentences=stripped, full_text=full_text)
                                aborted = True
                                return
                            to_emit = out_pre + "\n"
                            full_text += to_emit
                            yield StreamDelta(type="delta", text=to_emit)
                    in_reasoning = True
                    if _REASONING_CLOSE in rest:
                        idx = rest.index(_REASONING_CLOSE)
                        reasoning_buf += rest[:idx]
                        in_reasoning = False
                        line = rest[idx + len(_REASONING_CLOSE):]
                        if not line.strip():
                            continue
                    else:
                        reasoning_buf += rest + "\n"
                        continue

                # 进入 followups 区：收集不推送
                if in_followups:
                    if "</followups>" in line:
                        idx = line.index("</followups>")
                        followups_buf += line[:idx]
                        in_followups = False
                    else:
                        followups_buf += line + "\n"
                    continue
                # 检测 <followups> 开标签
                m = _FOLLOWUPS_OPEN_RE.search(line)
                if m:
                    before = line[: m.start()]
                    after = line[m.end():]
                    if before.strip():
                        keep, is_stripped = check_line(before, valid_ns)
                        if is_stripped:
                            stripped += 1
                        elif keep:
                            out = await self._post_check(before, tenant_id)
                            if out is None:
                                yield StreamDelta(type="refused", refused=True, refuse_reason="SENSITIVE_OUTPUT", stripped_sentences=stripped, full_text=full_text)
                                aborted = True
                                return
                            full_text += out
                            yield StreamDelta(type="delta", text=out)
                    in_followups = True
                    if "</followups>" in after:
                        idx = after.index("</followups>")
                        followups_buf += after[:idx]
                        in_followups = False
                    else:
                        followups_buf += after + "\n"
                    continue
                # 普通正文行：grounding + 敏感词 + PII
                keep, is_stripped = check_line(line, valid_ns)
                if is_stripped:
                    stripped += 1
                    continue
                if not keep:
                    continue
                out = await self._post_check(line, tenant_id)
                if out is None:
                    yield StreamDelta(type="refused", refused=True, refuse_reason="SENSITIVE_OUTPUT", stripped_sentences=stripped, full_text=full_text)
                    aborted = True
                    return
                to_emit = out + "\n"
                full_text += to_emit
                yield StreamDelta(type="delta", text=to_emit)

        try:
            async for delta in llm.stream_chat(messages, decisions.GENERATION_TEMPERATURE):
                line_buf += delta
                async for evt in _flush_lines(line_buf):
                    if evt.type == "refused":
                        yield evt
                        return
                    yield evt
                if aborted:
                    return
        except LLMTimeout as exc:
            logger.warning("LLM 首 token 超时: %s", exc)
            llm_failed = True
            llm_fail_reason = "LLM_TIMEOUT"
        except LLMError as exc:
            logger.error("LLM 生成失败: %s", exc)
            llm_failed = True
            llm_fail_reason = "LLM_ERROR"

        # 处理最后一段未换行内容
        # LLM 失败时也保留已生成的 partial 内容，避免已接收的文本被丢弃
        if line_buf and not aborted:
            async for evt in _flush_lines(line_buf, is_final=True):
                if evt.type == "refused":
                    yield evt
                    return
                yield evt

        if llm_failed and not full_text.strip():
            yield StreamDelta(
                type="refused", refused=True, refuse_reason=llm_fail_reason,
                stripped_sentences=stripped, full_text="",
            )
            return

        # ── 兜底：模型没写 </reasoning> 就把流结束了 ────────────────
        # 把收集到的推理内容当正文推出去，而不是让它凭空消失。
        # 取舍：漏出推理链 = 展示了不该展示的内容；丢弃 = 用户看到空白回答。
        # 后者是功能性故障，比前者严重。记指标观察模型遵循率（长期偏高就该
        # 考虑改用原生 thinking 参数，而不是继续靠 prompt 契约）。
        if in_reasoning and reasoning_buf.strip():
            metrics.incr("rag.cot.unclosed")
            logger.warning("思维链标签未闭合（流结束），剩余内容按正文返回")
            fallback = reasoning_buf.strip()
            full_text += fallback + "\n"
            in_reasoning = False
            yield StreamDelta(type="delta", text=fallback + "\n")

        # 解析 followups
        suggestions = [ln.strip() for ln in followups_buf.splitlines() if ln.strip()]

        # 全部被剥离 → 拒答
        if not full_text.strip():
            yield StreamDelta(
                type="refused", refused=True, refuse_reason="UNGROUNDED",
                stripped_sentences=stripped, full_text="",
            )
            return

        # ★ 提示词契约闭环（流式）：LLM 只输出拒答文案 ⇒ 发 refused，而不是 done。
        #   注意此时 delta 已随流发出，前端收到 refused 后应清空正文与引用，
        #   展示统一的拒答态——内容相同，但状态正确、且不再挂 8 条误导引用。
        if is_no_answer_output(full_text):
            yield StreamDelta(
                type="refused", refused=True, refuse_reason="NO_RELEVANT_CONTENT",
                stripped_sentences=stripped, full_text="",
            )
            return

        # ★ 有上下文却零引用 ⇒ 模型脱离检索结果自由发挥（幻觉）。
        #   流式下 delta 已经发出，前端收到 refused 后清空正文与引用。
        if is_unattributed_output(full_text, len(citations)) or is_offtopic_fallback(full_text):
            logger.warning(
                "UNGROUNDED: 检索到 %d 条上下文但答案零引用/绕道作答，按拒答处理",
                len(citations),
            )
            yield StreamDelta(
                type="refused", refused=True, refuse_reason="UNGROUNDED",
                stripped_sentences=stripped, full_text="",
            )
            return

        yield StreamDelta(
            type="done",
            full_text=full_text,
            stripped_sentences=stripped,
            suggestions=suggestions,
            usage={"prompt_tokens": 0, "completion_tokens": len(full_text)},
            reasoning=reasoning_buf.strip(),
        )

    async def _post_check(self, text: str, tenant_id: uuid.UUID | None) -> str | None:
        """PII 脱敏 + 敏感词检查。命中敏感词返回 None（上层发 refused）。"""
        if decisions.MASK_PII_ENABLED:
            text = mask_pii(text)
        if decisions.SENSITIVE_FILTER_ENABLED and tenant_id is not None:
            from app.services.sensitive import check_output
            hit, _word = await check_output(text, tenant_id)
            if hit:
                return None
        return text


def get_stream_generation_service() -> StreamGenerationService:
    return StreamGenerationService()
