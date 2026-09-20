"""L3 出口校验 —— 归因检查 + 剥离无归因内容（手册 §3.1 L3）。

核心设计：按行处理，保留原始 markdown 换行结构，只精确删除"含无效引用"的行。
（之前的"按句切分→过滤→拼回去"方案会吞掉所有换行，导致 markdown 格式全部丢失。）

处理规则：
  1. 按 \\n 分割为多行
  2. 每行提取 [n] 引用编号
  3. 含无效 n 的行 → 丢弃
  4. 无引用 / 全部有效 → 保留
  5. 剥离后全部为空 → 降级拒答
输出：GroundingResult(text, stripped_count, refused)
"""
from __future__ import annotations

import re
from dataclasses import dataclass

# 匹配 [n] 引用编号（n 为正整数）
_CITATION_RE = re.compile(r"\[(\d+)\]")


def check_line(line: str, valid_citation_ns: set[int]) -> tuple[bool, bool]:
    """单行归因校验。

    返回 (keep, is_stripped)：
      keep=True 保留该行；keep=False 丢弃（含无效引用）。
      is_stripped 表示该行因无效引用被丢弃（用于统计 stripped_sentences）。
      无引用的行（标题、空行、连接句）直接保留，is_stripped=False。
    """
    ns = [int(m) for m in _CITATION_RE.findall(line)]
    if not ns:
        return True, False
    invalid_ns = [n for n in ns if n not in valid_citation_ns]
    if invalid_ns:
        return False, True
    return True, False


@dataclass
class GroundingResult:
    text: str
    stripped_sentences: int
    refused: bool
    refuse_reason: str = ""


def check_grounding(
    generated_text: str, valid_citation_ns: set[int]
) -> GroundingResult:
    """L3 校验。按行检查引用有效性，保留原始 markdown 格式。"""
    lines = generated_text.split("\n")
    kept_lines: list[str] = []
    stripped = 0

    for line in lines:
        keep, is_stripped = check_line(line, valid_citation_ns)
        if is_stripped:
            stripped += 1
        if keep:
            kept_lines.append(line)

    final_text = "\n".join(kept_lines)
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
