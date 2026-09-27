"""分块服务回归测试 —— 2026-09-25 优化改动。

    cd code/apps/api
    pytest tests/chunk -v

★ 这里每一条都对应一个【真实踩到的坑】，不是凑覆盖率的边界用例。
  改 _SENTENCE_SPLIT_RE / heading 取值 / 表格表头逻辑时，这些用例必须继续成立。
"""
from __future__ import annotations

from app.config import decisions as D
from app.services.chunk import (
    _block_to_units,
    _count_tokens,
    _split_sentences,
    chunk_blocks,
    to_embedding_text,
)
from app.services.chunk.base import ChunkData
from app.services.parse.base import ParsedBlock


def _tok(s: str) -> int:
    return _count_tokens(s)


# ──────────────────────────────────────────────────────────
# P1-1  中文句子切分
# ──────────────────────────────────────────────────────────

def test_chinese_sentences_are_split_without_trailing_space():
    """中文标点后【没有空格】也必须能切开 —— 本次修的头号问题。

    修改前正则 (?<=[。！？!?\\.])\\s+ 要求标点后有空白，中文不触发，
    长段落被整段丢给硬切，句子被拦腰截断。
    """
    para = "报销标准分为市内交通费。" * 80
    assert _tok(para) > D.CHUNK_MAX_TOKENS  # 前置条件：确实触发降级

    assert len(_split_sentences(para)) > 1, "中文句子应被切开，否则已退化到硬切"


def test_sentence_split_keeps_trailing_punctuation():
    """切分不能把句末标点吞掉。"""
    sents = _split_sentences("第一段讲的是交通费。第二段讲的是住宿费。")
    assert len(sents) == 2
    assert all(s.endswith("。") for s in sents), sents


def test_decimal_and_abbrev_are_not_split():
    """西文句点仍要求后跟空白：保护 3.14 / e.g. 不被误切。"""
    joined = " ".join(_split_sentences("单价按 3.14 元折算，详见 e.g. 附表二。"))
    assert "3.14" in joined and "e.g." in joined


def test_newline_still_splits():
    assert len(_split_sentences("第一行\n第二行\n第三行")) == 3


# ──────────────────────────────────────────────────────────
# P1-2  跨小节 / 跨页
# ──────────────────────────────────────────────────────────

def test_heading_path_takes_start_of_chunk():
    """一块跨了两个小节时，应标【起始】小节。

    修改前取 current[-1]：主体是 3.1 却被标成 3.2，
    而该标题会直接出现在回答的引用位置（generate/__init__.py）。
    """
    blocks = [
        ParsedBlock("市内交通费按实际票据报销。" * 20, heading_path="3.1 报销标准"),
        ParsedBlock("差旅补贴按天数计算。" * 20, heading_path="3.2 差旅补贴"),
    ]
    cross = [c for c in chunk_blocks(blocks)
             if "市内" in c.content and "差旅" in c.content]
    assert cross, "用例意在构造跨小节 chunk；构造失败请调整输入长度"

    assert cross[0].heading_path == "3.1 报销标准", cross[0].heading_path


def test_page_no_prefers_first_available():
    blocks = [
        ParsedBlock("第一段内容。" * 10, heading_path="H", page_no=1),
        ParsedBlock("第二段内容。" * 10, heading_path="H", page_no=2),
    ]
    assert chunk_blocks(blocks)[0].page_no == 1


# ──────────────────────────────────────────────────────────
# P2-1  超宽表：表头不碎、数据不丢
# ──────────────────────────────────────────────────────────

def _make_table(n_cols: int, n_rows: int, name_len: int = 20):
    header = "\t".join(f"col_{i:03d}" + "x" * name_len for i in range(n_cols))
    rows = ["\t".join(f"value_{i}" for i in range(n_cols)) for _ in range(n_rows)]
    return header, rows


def test_header_intact_when_it_fits():
    """表头能放进 MAX 时，行组必须带【完整】表头，绝不能是半个表头。"""
    header, rows = _make_table(n_cols=40, n_rows=6)
    block = ParsedBlock(header + "\n" + "\n".join(rows), heading_path="S", is_table=True)
    units = [u[0] for u in _block_to_units(block)]

    assert units, "应当产出至少一组"
    assert any(u.split("\n")[0] == header for u in units), "至少一组需携带完整表头"
    # 任何一组的首行：要么完整表头，要么是数据行（value_ 开头），不允许碎片
    for u in units:
        first = u.split("\n")[0]
        assert first == header or first.startswith("value_"), first[:60]


def test_super_wide_table_no_header_fragment():
    """表头本身 > MAX 时宁可不给列定义，也不能给半个表头。"""
    header, rows = _make_table(n_cols=120, n_rows=6, name_len=60)
    assert _tok(header) > D.CHUNK_MAX_TOKENS, "前置条件：表头行确实超限"
    block = ParsedBlock(header + "\n" + "\n".join(rows), heading_path="S", is_table=True)
    units = [u[0] for u in _block_to_units(block)]

    for u in units:
        assert not u.startswith("col_"), "出现了表头碎片，应整体放弃携带表头"
        assert u != header, "不允许纯表头 unit"


def test_table_all_rows_preserved():
    """无论怎么切，每一行数据都不能丢。"""
    header, rows = _make_table(n_cols=60, n_rows=10)
    block = ParsedBlock(header + "\n" + "\n".join(rows), heading_path="S", is_table=True)
    joined = "\n".join(u[0] for u in _block_to_units(block))

    assert joined.count("value_0\t") >= 10, "数据行数不符，说明切分时丢了行"


def test_table_intact_when_fits_in_max():
    """整表能放下时必须原样保留。"""
    table = "姓名\t金额\n张三\t100\n李四\t200"
    units = _block_to_units(ParsedBlock(table, heading_path="S", is_table=True))
    assert len(units) == 1 and units[0][0] == table


# ──────────────────────────────────────────────────────────
# P0-1  embedding 输入必须包含章节标题
# ──────────────────────────────────────────────────────────

def test_embedding_text_carries_heading_path():
    """本次收益最大的修复：标题语义进向量。

    解析层把标题抽成 heading_path，content 里没有；
    不补前缀的话，问"报销标准"向量路一条都召不回。
    """
    c = ChunkData(content="市内交通费按票据报销。", token_count=8,
                  heading_path="财务制度 > 3.1 报销标准", page_no=None)
    out = to_embedding_text(c)
    assert out.startswith("[财务制度 > 3.1 报销标准]\n")
    assert "报销标准" in out and "市内交通费" in out


def test_embedding_text_without_heading_has_no_bracket():
    c = ChunkData(content="正文", token_count=2, heading_path="", page_no=None)
    assert to_embedding_text(c) == "正文"


def test_embedding_prefix_does_not_pollute_stored_content():
    """落库 content 必须保持原文干净（前端展示 / 引用回跳依赖它）。"""
    c = ChunkData(content="原文内容", token_count=4, heading_path="章节", page_no=None)
    to_embedding_text(c)
    assert c.content == "原文内容"


# ──────────────────────────────────────────────────────────
# 口径回归：没坏就别动的部分
# ──────────────────────────────────────────────────────────

def test_units_never_exceed_max_tokens():
    """每个 unit ≤ MAX 是 _accumulate 不卡死的前提。"""
    blocks = [
        ParsedBlock("很短的一段。" * 5, heading_path="H"),
        ParsedBlock("文档正文内容，用于构造较长文本。" * 400, heading_path="H"),
        ParsedBlock(_make_table(80, 4)[0] + "\n" + "\n".join(_make_table(80, 4)[1]),
                    heading_path="S", is_table=True),
    ]
    for b in blocks:
        for u in _block_to_units(b):
            assert _tok(u[0]) <= D.CHUNK_MAX_TOKENS


def test_chunks_respect_max_and_overlap():
    """目标 400 / 上限 800 / 重叠 ≥60 的既有口径不能被改坏。"""
    paras = [
        f"第 {i} 段关于报销与差旅的说明性文字，包含若干完整句子用于测试累积逻辑，措辞各不相同。"
        for i in range(40)
    ]
    chunks = chunk_blocks([ParsedBlock("\n\n".join(paras), heading_path="H")])
    assert chunks
    for c in chunks:
        assert c.token_count <= D.CHUNK_MAX_TOKENS

    if len(chunks) > 1:
        b0, b1 = chunks[0].content, chunks[1].content
        shared = 0
        for L in range(min(len(b0), len(b1)), 0, -1):
            if b0.endswith(b1[:L]):
                shared = L
                break
        assert _tok(b1[:shared]) >= D.CHUNK_OVERLAP_TOKENS


def test_no_infinite_loop_when_units_are_large():
    """consumed == 0 的防死循环分支：大 unit 场景仍能正常收敛。"""
    big_unit = "超长句子没有标点仅靠长度触发硬切处理" * 200
    chunks = chunk_blocks([ParsedBlock(big_unit, heading_path="H")])
    assert chunks and all(c.content.strip() for c in chunks)


def test_empty_blocks_skip():
    assert chunk_blocks([ParsedBlock("   ", heading_path="H")]) == []
