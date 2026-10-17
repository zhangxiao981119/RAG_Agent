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


# ── M8-P1：围栏代码块 ────────────────────────────────────────────────


def test_fence_code_block_verbatim():
    md = (
        "# 用法\n\n"
        "```python\n"
        "def f(x):\n"
        "    # 不是标题\n"
        "    return x.5\n"
        "```\n"
    ).encode()
    blocks = parse_markdown(md)
    assert len(blocks) == 1  # 标题只入栈不产块 → 全文只有代码块
    code = blocks[0]
    assert code.verbatim is True
    assert code.text.splitlines() == ["def f(x):", "    # 不是标题", "    return x.5"]
    assert code.heading_path == "用法"


def test_fence_unclosed_falls_back_to_text():
    md = "```python\ndef f(x):\n    return x\n".encode()
    blocks = parse_markdown(md)
    assert not any(b.verbatim for b in blocks)
    assert "def f(x):" in blocks[0].text


def test_tilde_fence_and_longer_close():
    md = "~~~~\ncode line\n~~~~~~\nafter\n".encode()
    blocks = parse_markdown(md)
    code = [b for b in blocks if b.verbatim]
    assert len(code) == 1
    assert code[0].text == "code line"
    assert blocks[-1].text == "after"


def test_fence_after_text_keeps_block_order():
    md = ("前置段落\n```\ncode\n```\n后置段落\n").encode()
    blocks = parse_markdown(md)
    assert [b.verbatim for b in blocks] == [False, True, False]
    assert blocks[0].text == "前置段落"
    assert blocks[2].text == "后置段落"


def test_indented_fence_is_fence_body():
    """4+ 空格缩进的 ``` 是代码内容，不是闭栏（GFM 口径）。"""
    md = "```\nline1\n    ```\nline2\n```\n".encode()
    blocks = parse_markdown(md)
    code = [b for b in blocks if b.verbatim]
    assert len(code) == 1
    assert "    ```" in code[0].text
    assert "line2" in code[0].text


# ── M8-P1：列表聚合 ──────────────────────────────────────────────────


def test_list_aggregated_into_one_verbatim_block():
    md = "配置步骤：\n- 第一步\n- 第二步\n- 第三步\n结尾段落\n".encode()
    blocks = parse_markdown(md)
    assert [b.verbatim for b in blocks] == [False, True, False]
    lst = blocks[1]
    assert lst.text.splitlines() == ["- 第一步", "- 第二步", "- 第三步"]


def test_list_with_indent_continuation():
    md = ("- 项一\n  续行内容\n- 项二\n普通段落\n").encode()
    blocks = parse_markdown(md)
    lst = blocks[0]
    assert lst.verbatim is True
    assert "  续行内容" in lst.text
    assert blocks[1].text == "普通段落"


def test_ordered_list_aggregated():
    md = "1. 安装\n2. 配置\n3. 启动\n".encode()
    blocks = parse_markdown(md)
    assert len(blocks) == 1
    assert blocks[0].verbatim is True
    assert len(blocks[0].text.splitlines()) == 3


def test_year_like_line_not_list():
    """「1970. 年代」不是有序列表（序号限 3 位防误吞年份文本）。"""
    md = "1970. 年代发生了大事\n后续正文\n".encode()
    blocks = parse_markdown(md)
    assert not any(b.verbatim for b in blocks)


def test_list_inside_heading_uses_stack():
    md = ("## 步骤\n\n- a\n- b\n").encode()
    blocks = parse_markdown(md)
    assert blocks[-1].heading_path == "步骤"


# ── M8-P2：块级公式 + mermaid 钉住 ───────────────────────────────────


def test_formula_block_verbatim():
    md = (
        "# 推导\n\n"
        "$$\n"
        "E = mc^2 with 3.14 radius\n"
        "\\int_0^1 x^2 dx\n"
        "$$\n"
    ).encode()
    blocks = parse_markdown(md)
    assert len(blocks) == 1
    f = blocks[0]
    assert f.verbatim is True
    # 公式行的 3.14 不构成句子切分断点：整体一个 verbatim 块
    assert f.text.splitlines() == ["E = mc^2 with 3.14 radius", "\\int_0^1 x^2 dx"]
    assert f.heading_path == "推导"


def test_formula_unclosed_falls_back():
    md = "$$\nE = mc^2\n".encode()
    blocks = parse_markdown(md)
    assert not any(b.verbatim for b in blocks)
    assert "E = mc^2" in blocks[0].text


def test_single_line_dollar_dollar_not_formula():
    """单行 $$..$$ 不识别（窄口防误判）。"""
    md = "价格是 $$100 起步\n正文\n".encode()
    blocks = parse_markdown(md)
    assert not any(b.verbatim for b in blocks)


def test_mermaid_fence_is_verbatim():
    """mermaid 图是 ``` 围栏，钉住其走 M8-P1 verbatim 保真路径。"""
    md = (
        "# 流程\n\n"
        "```mermaid\n"
        "graph TD\n"
        "    A[开始] --> B{判断}\n"
        "```\n"
    ).encode()
    blocks = parse_markdown(md)
    assert len(blocks) == 1
    assert blocks[0].verbatim is True
    assert "graph TD" in blocks[0].text
