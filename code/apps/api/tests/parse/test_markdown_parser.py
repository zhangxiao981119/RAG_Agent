r"""markdown 管道表识别测试（表格决策 B1' 窄口版）。

覆盖：标准管道表 → is_table 契约（TSV + table_header）、\| 转义、
cell_to_text 消毒、伪表格/低可信度回退、块序与标题归属。
"""
from __future__ import annotations

from app.services.parse.markdown_parser import parse_markdown


def _tables(blocks):
    return [b for b in blocks if b.is_table]


# ── 既有行为回归 ────────────────────────────────────────────────────


def test_heading_split_unchanged():
    blocks = parse_markdown("# 标题\n\n正文一段\n\n## 子标题\n\n正文二段\n".encode())
    assert [b.heading_path for b in blocks] == ["标题", "标题 > 子标题"]
    assert [b.text for b in blocks] == ["正文一段", "正文二段"]
    assert not any(b.is_table for b in blocks)


# ── 标准管道表识别 ──────────────────────────────────────────────────


def test_pipe_table_basic():
    md = (
        "# 制度\n\n"
        "| 名称 | 数额 |\n"
        "| --- | --- |\n"
        "| 差旅 | 500 |\n"
        "| 餐补 | 100 |\n"
    ).encode()
    blocks = parse_markdown(md)
    tables = _tables(blocks)
    assert len(tables) == 1
    t = tables[0]
    assert t.heading_path == "制度"
    assert t.table_header == ["名称", "数额"]
    lines = t.text.split("\n")
    assert lines[0] == "名称\t数额"
    assert lines[1] == "差旅\t500"
    assert lines[2] == "餐补\t100"


def test_pipe_table_alignment_row_and_escape():
    md = (
        "| 名称 | 备注 | 归属 |\n"
        "| :---: | --- | --- |\n"
        "| 报销 | 含\\|合计 | 财务 |\n"
    ).encode()
    blocks = parse_markdown(md)
    t = _tables(blocks)[0]
    # 对齐冒号不进数据；\| 转义不当列边界（单元格内保留竖线，列数不变）
    assert t.table_header == ["名称", "备注", "归属"]
    assert t.text.split("\n")[1] == "报销\t含|合计\t财务"


def test_cell_sanitized():
    md = "| a | b |\n| --- | --- |\n| x\ty | z |\n".encode()
    t = _tables(parse_markdown(md))[0]
    # 制表符进 TSV 会制造伪列 → 必须消毒为空格
    assert t.text.split("\n")[1] == "x y\tz"


# ── 窄口边界：不识别的形态 ──────────────────────────────────────────


def test_pseudo_table_without_separator_kept_as_text():
    md = "| 只有 | 竖线 |\n没有分隔行\n".encode()
    blocks = parse_markdown(md)
    assert not any(b.is_table for b in blocks)
    assert "| 只有 | 竖线 |" in blocks[0].text


def test_header_only_table_falls_back_to_text():
    md = "| a | b |\n| --- | --- |\n".encode()
    blocks = parse_markdown(md)
    assert not any(b.is_table for b in blocks)
    assert "| a | b |" in blocks[0].text


def test_blank_header_table_falls_back_to_text():
    md = "| | |\n| --- | --- |\n| x | y |\n".encode()
    blocks = parse_markdown(md)
    assert not any(b.is_table for b in blocks)


def test_separator_requires_two_columns():
    md = "| 单列 |\n| --- |\n| x |\n".encode()
    blocks = parse_markdown(md)
    assert not any(b.is_table for b in blocks)


# ── 块序与标题归属 ──────────────────────────────────────────────────


def test_text_before_table_flushed_in_order():
    md = (
        "段落前置\n"
        "| a | b |\n| --- | --- |\n| x | y |\n"
        "段落后置\n"
    ).encode()
    blocks = parse_markdown(md)
    assert [b.is_table for b in blocks] == [False, True, False]
    assert blocks[0].text == "段落前置"
    assert blocks[2].text == "段落后置"


def test_table_uses_current_heading_stack():
    md = (
        "## 一级\n\n"
        "intro\n\n"
        "| a | b |\n| --- | --- |\n| x | y |\n\n"
        "### 二级\n\n"
        "| c | d |\n| --- | --- |\n| m | n |\n"
    ).encode()
    blocks = parse_markdown(md)
    tables = _tables(blocks)
    assert tables[0].heading_path == "一级"
    assert tables[1].heading_path == "一级 > 二级"
