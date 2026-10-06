"""引用双向校验 —— 补齐「模型报的引用是否真的存在」这一反向检查。

与 `grounding/__init__.py` 的分工：

| 检查 | 方向 | 处理 |
|---|---|---|
| `check_grounding` | 答案 → 引用 | 含无效 `[n]` 的行直接剥离 |
| **本模块** | 引用 → 检索结果 | 提取模型实际用到的引用，产出受控的引用列表 |

为什么需要反向检查：模型可能在行内写 `[7]`（`check_grounding` 会剥离该行），
也可能行内写得都对、但最终引用列表补齐时把「检索到但模型没用」的片段也带出去，
让用户以为答案依据了那些内容。**引用必须只包含模型真正用到的片段。**

额外约束（来自生产事故）：引用数量需要上下限。太少说明依据不足，
太多会稀释可读性并放大上下文成本。
"""
from __future__ import annotations

import re
from dataclasses import dataclass

_CITATION_RE = re.compile(r"\[(\d+)\]")

# 引用数量边界。下限 1：有答案就必须有依据；
# 上限 8：与 TOP_K_RERANK 对齐，超出说明引用在稀释而非支撑结论。
MIN_CITATIONS = 1
MAX_CITATIONS = 8


@dataclass
class CitationValidation:
    """校验结果。

    used_ns        模型真正引用的编号（已排序去重，且都落在有效范围内）
    dropped_ns     被丢弃的编号（越界？不 —— 越界的由 check_grounding 处理；
                   这里指在有效范围内但超出 MAX_CITATIONS 的）
    hallucinated   模型引用了但超出检索结果范围的编号（幻觉引用）
    """

    used_ns: list[int]
    dropped_ns: list[int]
    hallucinated: list[int]
    insufficient: bool


def extract_cited_ns(text: str) -> list[int]:
    """按出现顺序提取文本中的引用编号（去重）。"""
    seen: list[int] = []
    for m in _CITATION_RE.findall(text):
        n = int(m)
        if n not in seen:
            seen.append(n)
    return seen


def validate_citations(
    text: str,
    retrieved_count: int,
    *,
    min_citations: int = MIN_CITATIONS,
    max_citations: int = MAX_CITATIONS,
) -> CitationValidation:
    """校验并规整引用编号。

    参数：
        text             已经过 grounding 校验的答案文本
        retrieved_count  本次检索到的片段总数（编号 1..retrieved_count 合法）

    规则：
      1. 越界编号（n <= 0 或 n > retrieved_count）计入 hallucinated 并丢弃
      2. 有效编号超出 max_citations → 截断，被截断的计入 dropped_ns
      3. 有效编号不足 min_citations → 标记 insufficient（调用方决定是否降级为拒答）

    ★ 注意：本函数**不**凭空补齐引用。曾经的做法是「不足时从检索结果补」，
    但那会让答案挂上模型从未使用的来源 —— 是误导，不是修复。
    正确的处理是：不足即标记 insufficient，由上层决定拒答还是照常返回。
    """
    if retrieved_count <= 0:
        return CitationValidation([], [], [], insufficient=True)

    cited = extract_cited_ns(text)

    hallucinated: list[int] = []
    valid: list[int] = []
    for n in cited:
        if 1 <= n <= retrieved_count:
            valid.append(n)
        else:
            hallucinated.append(n)

    dropped: list[int] = []
    if len(valid) > max_citations:
        dropped = valid[max_citations:]
        valid = valid[:max_citations]

    return CitationValidation(
        used_ns=valid,
        dropped_ns=dropped,
        hallucinated=hallucinated,
        insufficient=len(valid) < min_citations,
    )
