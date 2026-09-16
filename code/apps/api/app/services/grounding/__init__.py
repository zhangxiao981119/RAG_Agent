"""L3 出口校验 —— 归因检查 + 剥离无归因句（手册 §3.1 L3）。

输入：生成文本 + 引用列表（n → chunk_id）
处理：
  1. 按句切分
  2. 每句提取 [n] 编号
  3. 若该句含 [n] 但 n 不在 citations → 剥离整句
  4. 剥离后空 → 降级拒答
输出：GroundingResult(text, stripped_count, refused)

★ MUST NOT 只记日志不剥离（手册原话）。
★ MUST 真删掉无归因句，不能只标记。
"""
from __future__ import annotations

import re
from dataclasses import dataclass

# 匹配 [n] 引用编号（n 为正整数）
_CITATION_RE = re.compile(r"\[(\d+)\]")
# 句子切分：按中文句号/问号/感叹号/换行切
_SENTENCE_SPLIT_RE = re.compile(r"[。！？!?]|\n+")


@dataclass
class GroundingResult:
    text: str
    stripped_sentences: int
    refused: bool
    refuse_reason: str = ""


def _split_sentences(text: str) -> list[str]:
    """切句。空句丢弃。"""
    parts = _SENTENCE_SPLIT_RE.split(text)
    return [p.strip() for p in parts if p.strip()]


def _extract_citation_ns(sentence: str) -> list[int]:
    return [int(m) for m in _CITATION_RE.findall(sentence)]


def check_grounding(
    generated_text: str, valid_citation_ns: set[int]
) -> GroundingResult:
    """L3 校验。

    valid_citation_ns: 已下发给 LLM 的引用编号集合（来自 citations 列表）。
    """
    sentences = _split_sentences(generated_text)
    kept: list[str] = []
    stripped = 0

    for sent in sentences:
        ns = _extract_citation_ns(sent)
        if not ns:
            # 无引用的句子：保留（如开头寒暄、连接句）
            # 手册没要求"无引用句必须剥离"，只要求"有引用但无效的剥离"
            kept.append(sent)
            continue
        # 有引用：检查是否都在有效集合里
        invalid_ns = [n for n in ns if n not in valid_citation_ns]
        if invalid_ns:
            stripped += 1
            continue
        kept.append(sent)

    final_text = "".join(kept) if kept else ""
    # 剥离后空 → 降级拒答
    if not final_text.strip():
        return GroundingResult(
            text="",
            stripped_sentences=stripped,
            refused=True,
            refuse_reason="UNGROUNDED",
        )

    return GroundingResult(
        text=final_text,
        stripped_sentences=stripped,
        refused=False,
    )


def get_grounding_service() -> object:
    """占位：M2 用纯函数 check_grounding，不需要 service 类。"""
    return None
