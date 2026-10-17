"""表格组打标测试（表格决策 A1' 解析侧）。

钉住四件事：
  1. 超大表行组切分的全部分片共享同一 table_group_id（回捞的前提）
  2. 整表不切的单片不打组（无回捞意义）
  3. 打组的 chunk 必为纯表格 —— 表格分片与其他文本混装的 chunk 不打组
  4. 不同表格块的组 id 相互独立
"""
from __future__ import annotations

import uuid

from app.services.chunk import chunk_blocks
from app.services.parse.base import ParsedBlock


def _big_table_block(rows: int = 24) -> ParsedBlock:
    """构造一个必然超过 800 token 的 TSV 表格块（每行约 50+ token，行内容互不相同）。"""
    lines = ["colA\tcolB\tcolC\tcolD"]
    for r in range(rows):
        lines.append("\t".join(f"cell-{r}-{c}" + "x" * 40 for c in range(4)))
    return ParsedBlock(
        text="\n".join(lines), is_table=True, table_header=["colA", "colB", "colC", "colD"]
    )


def test_oversize_table_shares_group():
    """超大表 → 多个分片 chunk，纯表格分片共享同一非空组 id。"""
    blocks = [_big_table_block(rows=24)]
    chunks = chunk_blocks(blocks)
    grouped = [c for c in chunks if c.table_group_id is not None]
    assert len(grouped) >= 2  # 必然切出多个分片
    assert len({c.table_group_id for c in grouped}) == 1  # 全部同组


def test_whole_table_no_group():
    """小表整表成片 → 不打组（单片无回捞意义）。"""
    block = ParsedBlock(text="a\tb\n1\t2", is_table=True, table_header=["a", "b"])
    chunks = chunk_blocks([block])
    assert len(chunks) == 1
    assert chunks[0].table_group_id is None


def test_grouped_chunks_are_pure_table():
    """打组的 chunk 必为纯表格：内容不含同文档其他文本块的内容。"""
    table = _big_table_block(rows=24)
    tail_text = "这是表格之后的普通段落，绝不应该出现在任何打组的分片里。" + "补充" * 20
    chunks = chunk_blocks([table, ParsedBlock(text=tail_text)])
    for c in chunks:
        if c.table_group_id is not None:
            assert "普通段落" not in c.content
    # 混装风险真实存在时（贪心累积把表分片和文本并块），那条 chunk 必然无组标
    mixed = [c for c in chunks if "普通段落" in c.content]
    for c in mixed:
        assert c.table_group_id is None


def test_distinct_blocks_distinct_groups():
    """两个不同的大表块 → 两组 id 相互独立。"""
    a = _big_table_block(rows=24)
    b = _big_table_block(rows=24)
    chunks = chunk_blocks([a, b])
    groups = {c.table_group_id for c in chunks if c.table_group_id is not None}
    assert len(groups) == 2


def test_text_chunk_no_group():
    """普通文本 chunk 恒无组。"""
    chunks = chunk_blocks([ParsedBlock(text="普通内容 " + "词" * 300)])
    assert chunks
    assert all(c.table_group_id is None for c in chunks)


def test_group_id_shape():
    """组 id 为 uuid hex：无需语义，只需唯一且可作 SQL 参数。"""
    chunks = chunk_blocks([_big_table_block(rows=24)])
    grouped = [c for c in chunks if c.table_group_id is not None]
    assert grouped
    uuid.UUID(hex=grouped[0].table_group_id)  # 能解析即合法
