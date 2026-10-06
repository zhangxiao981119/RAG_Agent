"""分块服务 —— 手册 §3.3.1 参数冻结 + 切分优先级。

参数（从 decisions.py 读，MUST NOT 硬编码）：
  CHUNK_TARGET_TOKENS = 400  目标
  CHUNK_MAX_TOKENS    = 800  硬上限
  CHUNK_OVERLAP_TOKENS = 60  相邻块重叠

切分优先级：① 标题层级 → ② 段落 → ③ 句子 → ④ 硬切
表格：整表不切，超限时按行组切并保留表头

输入：ParsedBlock 列表（来自 parse 服务）
输出：ChunkData 列表（待写入 chunks 表）
"""
from __future__ import annotations

import re

import tiktoken

from app.config import decisions
from app.services.chunk.base import ChunkData
from app.services.parse.base import ParsedBlock

# tiktoken 编码器（cl100k_base 是 gpt-4 系列默认；bge-m3 用它计数近似）
_ENCODER = tiktoken.get_encoding("cl100k_base")

# 句子切分：中文句号/问号/感叹号 + 换行切
# ★ 中文标点【后面通常没有空格】，所以不能写成 (?<=[。！？])\s+ —— 那样永远匹配不到，
#   三级降级会直接退化成"段落 → 硬切"。这里中文标点后零宽切分，不要求空白。
# ★ 西文句点 "." 仍要求后跟空白再切：避免把 "3.14"、"e.g." 拦腰截断。
_SENTENCE_SPLIT_RE = re.compile(r"(?<=[。！？!?])|(?<=[.!?])\s+|\n+")
# 段落切分：双换行
_PARAGRAPH_SPLIT_RE = re.compile(r"\n\s*\n")


def _count_tokens(text: str) -> int:
    return len(_ENCODER.encode_ordinary(text))


def _split_sentences(text: str) -> list[str]:
    """按句子切分。空句子丢弃。"""
    parts = _SENTENCE_SPLIT_RE.split(text)
    return [p.strip() for p in parts if p.strip()]


def _split_paragraphs(text: str) -> list[str]:
    parts = _PARAGRAPH_SPLIT_RE.split(text)
    return [p.strip() for p in parts if p.strip()]


def _hard_split(text: str, max_tokens: int) -> list[str]:
    """硬切：按 token 切，每片 <= max_tokens。切完解码回文本。"""
    ids = _ENCODER.encode_ordinary(text)
    pieces: list[str] = []
    for start in range(0, len(ids), max_tokens):
        chunk_ids = ids[start : start + max_tokens]
        pieces.append(_ENCODER.decode(chunk_ids))
    return pieces


def _block_to_units(block: ParsedBlock) -> list[tuple[str, str, int | None]]:
    """把 ParsedBlock 展开为原子单元 (text, heading_path, page_no)。

    表格块：整表作为一个单元（除非超 MAX，按行组切并保留表头）。
    文本块：段落 → 句子 → 硬切，逐级降级。
    """
    if block.is_table:
        # 表格：先看整表是否超 MAX
        if _count_tokens(block.text) <= decisions.CHUNK_MAX_TOKENS:
            return [(block.text, block.heading_path, block.page_no)]
        # 超限：按行组切，每组带表头
        lines = block.text.split("\n")
        if not lines:
            return []

        def _line_pieces(text_line: str) -> list[str]:
            """单行（含表头）超 MAX 时硬切成多片。

            保证下游 _accumulate 收到的每个 unit 都 <= CHUNK_MAX_TOKENS，
            否则超大 unit 会让累积器一个单元都装不下，index 永不推进形成死循环。
            """
            if _count_tokens(text_line) <= decisions.CHUNK_MAX_TOKENS:
                return [text_line]
            return _hard_split(text_line, decisions.CHUNK_MAX_TOKENS)

        # 展平所有行：超长单元格被硬切（极端场景，与文本路径硬切同级降级）
        # ★ 只切【数据行】。原实现把表头行也塞进来一起硬切，导致 flat_pieces[0]
        #   是"表头的第一个碎片"，后续行组拿到的列定义是残缺的。
        raw_header = lines[0]
        header_tokens = _count_tokens(raw_header)

        flat_pieces: list[str] = []
        for ln in lines[1:]:
            flat_pieces.extend(_line_pieces(ln))
        if not flat_pieces:
            return []

        # 表头 + 至少一行能放进 MAX 时才携带表头；表头本身超 MAX 则无处安放，
        # 宁可不给列定义，也不给"半个表头"（半个表头比没表头更有误导性）。
        header_fits = header_tokens <= decisions.CHUNK_MAX_TOKENS

        units: list[tuple[str, str, int | None]] = []
        buffer_lines: list[str] = []
        buffer_tokens = 0
        for piece in flat_pieces:
            piece_tokens = _count_tokens(piece)
            if buffer_lines and buffer_tokens + piece_tokens > decisions.CHUNK_MAX_TOKENS:
                # 装满一组：吐出，下一组重新尝试带表头
                units.append(("\n".join(buffer_lines), block.heading_path, block.page_no))
                buffer_lines, buffer_tokens = [], 0
            if not buffer_lines and header_fits and header_tokens + piece_tokens <= decisions.CHUNK_MAX_TOKENS:
                buffer_lines.append(raw_header)
                buffer_tokens = header_tokens
            buffer_lines.append(piece)
            buffer_tokens += piece_tokens
        if buffer_lines:
            units.append(("\n".join(buffer_lines), block.heading_path, block.page_no))
        return units

    # 文本块：段落 → 句子 → 硬切
    units: list[tuple[str, str, int | None]] = []
    for para in _split_paragraphs(block.text):
        para_tokens = _count_tokens(para)
        if para_tokens <= decisions.CHUNK_MAX_TOKENS:
            units.append((para, block.heading_path, block.page_no))
            continue
        # 段超限：按句子切
        sentences = _split_sentences(para)
        for sent in sentences:
            sent_tokens = _count_tokens(sent)
            if sent_tokens <= decisions.CHUNK_MAX_TOKENS:
                units.append((sent, block.heading_path, block.page_no))
                continue
            # 句子仍超限：硬切
            for piece in _hard_split(sent, decisions.CHUNK_MAX_TOKENS):
                units.append((piece, block.heading_path, block.page_no))
    return units


def _accumulate(units: list[tuple[str, str, int | None]]) -> list[ChunkData]:
    """贪心累积 + 重叠。

    累积到 TARGET 时切；超过 MAX 时立刻切。
    相邻块共享前 OVERLAP tokens 的尾部单元。
    """
    chunks: list[ChunkData] = []
    index = 0
    overlap_units: list[tuple[str, str, int | None]] = []

    while index < len(units):
        current = list(overlap_units)
        current_tokens = sum(_count_tokens(u[0]) for u in current)
        consumed = 0
        # 累积
        while index < len(units):
            unit = units[index]
            unit_tokens = _count_tokens(unit[0])
            if current_tokens + unit_tokens > decisions.CHUNK_MAX_TOKENS:
                break
            current.append(unit)
            current_tokens += unit_tokens
            index += 1
            consumed += 1
            if current_tokens >= decisions.CHUNK_TARGET_TOKENS:
                break

        if consumed == 0:
            # 重叠单元挤占了空间（overlap 构造是先取后判，可能含大单元），
            # 导致本轮一个新单元都装不下：丢弃重叠重新累积。
            # 空累积必然能装下至少一个单元（每个单元 <= CHUNK_MAX_TOKENS），
            # 保证 index 必然推进，避免死循环。
            overlap_units = []
            continue

        if not current:
            # 单个单元就超 MAX，硬切兜底（理论上前面已硬切过，这里不应该到）
            index += 1
            continue

        text = "\n\n".join(u[0] for u in current)
        # ★ 一块内容可能跨小节/跨页。标【起始】位置比标"最深的那个"更贴近直觉：
        #   原实现取 current[-1]，跨小节时会把整块标成末尾小节的名字，
        #   而引用溯源（generate/__init__.py）直接把这个 heading_path 展示给用户。
        heading_path = current[0][1] or current[-1][1]
        page_no = next((u[2] for u in current if u[2] is not None), None)
        chunks.append(
            ChunkData(
                content=text,
                token_count=_count_tokens(text),
                heading_path=heading_path,
                page_no=page_no,
            )
        )

        # 构造 overlap：从 current 尾部取单元，凑够 OVERLAP tokens
        overlap_units = []
        overlap_tokens = 0
        for unit in reversed(current):
            if overlap_tokens >= decisions.CHUNK_OVERLAP_TOKENS:
                break
            overlap_units.insert(0, unit)
            overlap_tokens += _count_tokens(unit[0])

    return chunks


def to_embedding_text(chunk: ChunkData) -> str:
    """生成喂给 embedding 模型的文本 —— ≠ 落库的 content，两者刻意不同。

    ★ 背景：markdown / pdf 解析层把标题行抽成了 heading_path，content 里并【没有】
      标题文字；而 worker 原本只拿 content 去 embed，导致"报销标准"这类只出现在
      章节标题里的词，向量召回路完全抓不回（只有关键词路的 heading_path ILIKE 能兜住）。

    ★ docx 解析器是例外：它把标题也作为一个 block，标题正文本就在 content 里。
      所以这里的前缀会让 docx 的标题**重复出现一次**（"[报销标准]\n报销标准"）。
      这是刻意接受的轻微冗余 —— 与其为去重引入 parser 来源判断（本层看不到来源，
      且一个 chunk 会混合多个 block），不如让标题词权重略高。标题词本就是典型
      检索词，权重高一点不吃亏。

    ★ 所以这里把章节路径作为前缀补进 embedding 输入；落库的 content 保持原文纯净，
      前端展示/引用回跳不受影响。

    ⚠ 改动本函数等价于变更索引口径：历史文档必须【重新索引】，否则新旧向量不同构。
    """
    prefix = f"[{chunk.heading_path}]\n" if chunk.heading_path else ""
    return prefix + chunk.content


def chunk_blocks(blocks: list[ParsedBlock]) -> list[ChunkData]:
    """对解析后的 blocks 做分块。空 block 会被跳过。"""
    units: list[tuple[str, str, int | None]] = []
    for block in blocks:
        if not block.text.strip():
            continue
        units.extend(_block_to_units(block))
    if not units:
        return []
    return _accumulate(units)
