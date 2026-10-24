"""WU-M-13 html 解析清洗器测试。

钉住六件事：
  1. 噪声剔除：script/style/nav/footer/header 内容不进正文
  2. 标题层级：h1/h2 维护 heading_path（与 docx 标题栈同口径）
  3. 表格契约：is_table=True + TSV 序列化 + table_header（chunk 整表契约兼容）
  4. 合并展开：rowspan/colspan 网格法回填（左上值填充，列不错位）
  5. 表格内文本不重复：td 里的 p 不再单独成块（重复入库防护）
  6. 空文档 / 装饰性表格：ParseError / 静默丢弃不产空表块
"""
from __future__ import annotations

import pytest

from app.services.parse.base import ParseError
from app.services.parse.html_parser import parse_html


def test_noise_tags_removed():
    html = """<html><body>
      <nav>导航栏链接</nav>
      <header>站点头部</header>
      <p>正文第一段</p>
      <footer>版权信息</footer>
      <script>alert('x')</script>
      <style>.a{color:red}</style>
    </body></html>""".encode("utf-8")
    blocks = parse_html(html)
    assert len(blocks) == 1
    assert blocks[0].text == "正文第一段"
    all_text = "\n".join(b.text for b in blocks)
    for noise in ("导航栏链接", "站点头部", "版权信息", "alert", "color:red"):
        assert noise not in all_text


def test_heading_stack_maintains_path():
    html = """<html><body>
      <h1>第一章 总则</h1>
      <p>总则正文</p>
      <h2>1.1 目的</h2>
      <p>目的正文</p>
      <h1>第二章 细则</h1>
      <p>细则正文</p>
    </body></html>""".encode("utf-8")
    blocks = parse_html(html)
    assert [b.heading_path for b in blocks] == [
        "第一章 总则",
        "第一章 总则/1.1 目的",
        "第二章 细则",
    ]


def test_table_becomes_is_table_tsv_block():
    html = """<html><body>
      <h1>费用说明</h1>
      <table>
        <tr><th>项目</th><th>金额</th><th>说明</th></tr>
        <tr><td>服务费</td><td>1200</td><td>按年收取</td></tr>
        <tr><td>维护费</td><td>300</td><td>按季度收取</td></tr>
      </table>
    </body></html>""".encode("utf-8")
    blocks = parse_html(html)
    assert len(blocks) == 1
    table = blocks[0]
    assert table.is_table is True
    assert table.heading_path == "费用说明"
    assert table.table_header == ["项目", "金额", "说明"]
    lines = table.text.split("\n")
    assert lines[0] == "项目\t金额\t说明"
    assert lines[1] == "服务费\t1200\t按年收取"
    assert lines[2] == "维护费\t300\t按季度收取"


def test_rowspan_colspan_expanded():
    html = """<html><body>
      <table>
        <tr><th>类别</th><th>项目</th><th>金额</th></tr>
        <tr><td rowspan="2">固定费用</td><td>服务费</td><td>1200</td></tr>
        <tr><td>维护费</td><td>300</td></tr>
        <tr><td colspan="2">一次性费用合计</td><td>5000</td></tr>
      </table>
    </body></html>""".encode("utf-8")
    blocks = parse_html(html)
    table = blocks[0]
    lines = table.text.split("\n")
    # rowspan：第二行首列回填"固定费用"，列不错位
    assert lines[1] == "固定费用\t服务费\t1200"
    assert lines[2] == "固定费用\t维护费\t300"
    # colspan：跨两列各占一格
    assert lines[3] == "一次性费用合计\t一次性费用合计\t5000"


def test_cell_text_inside_table_not_duplicated():
    html = """<html><body>
      <p>表格前的正文</p>
      <table>
        <tr><th>项目</th><th>说明</th></tr>
        <tr><td><p>服务费</p></td><td><p>按年收取</p></td></tr>
      </table>
    </body></html>""".encode("utf-8")
    blocks = parse_html(html)
    # td 里的 <p> 不单独成块：只有"表格前正文"一个文本块 + 一个表格块
    assert len(blocks) == 2
    assert blocks[0].text == "表格前的正文"
    assert blocks[1].is_table is True
    assert blocks[1].text.split("\n")[1] == "服务费\t按年收取"


def test_single_row_table_dropped():
    # 只有表头行的装饰性 table 不构成可检索表格（与 pdf 可信度阈值同源）
    html = """<html><body>
      <p>正文</p>
      <table><tr><th>项目</th><th>金额</th></tr></table>
    </body></html>""".encode("utf-8")
    blocks = parse_html(html)
    assert len(blocks) == 1
    assert blocks[0].text == "正文"


def test_cell_tab_and_newline_sanitized():
    html = """<html><body>
      <table>
        <tr><th>项目</th><th>说明</th></tr>
        <tr><td>服\t务费</td><td>按年\n收取</td></tr>
      </table>
    </body></html>""".encode("utf-8")
    blocks = parse_html(html)
    # cell_to_text 消毒：\t/\n 不破坏 TSV 结构
    assert blocks[0].text.split("\n")[1] == "服 务费\t按年 收取"


def test_empty_document_raises_parse_error():
    with pytest.raises(ParseError, match="未提取到可检索内容"):
        parse_html("<html><body><nav>只有导航</nav><script>1</script></body></html>".encode("utf-8"))


# ── M8-P1：<pre> 代码块逐行保真 ──────────────────────────────────────


def test_pre_block_verbatim_keeps_newlines():
    html = (
        "<html><body><h2>示例</h2>"
        "<pre>def f(x):\n    return x + 1\n</pre>"
        "<p>正文段落</p>"
        "</body></html>"
    ).encode("utf-8")
    blocks = parse_html(html)
    pre = [b for b in blocks if b.verbatim]
    assert len(pre) == 1
    # get_text() 不压平：换行与缩进保留（此前 get_text(" ") 会把三行压成一行）
    assert pre[0].text.splitlines() == ["def f(x):", "    return x + 1"]
    assert pre[0].heading_path == "示例"
    # h2 只入栈不产块 → blocks = [pre, p]
    assert [b.verbatim for b in blocks] == [True, False]


def test_pre_inside_table_not_duplicated():
    """td 里的 <pre> 归表格块，不重复进正文。"""
    html = (
        "<html><body><table>"
        "<tr><th>说明</th></tr>"
        "<tr><td><pre>code\nhere</pre></td></tr>"
        "</table></body></html>"
    ).encode("utf-8")
    blocks = parse_html(html)
    assert not any(b.verbatim for b in blocks)
    assert len(blocks) == 1  # 只有表格块
    assert "code" in blocks[0].text


# ── M8-P3 图片 alt 卫生 ────────────────────────────────────────────


def test_img_alt_joins_paragraph():
    html = '<html><body><p>外观 <img src="a.png" alt="AX3000 白色"> 如上</p></body></html>'
    blocks = parse_html(html.encode("utf-8"))
    assert len(blocks) == 1
    assert blocks[0].text == "外观 [图片] AX3000 白色 如上"


def test_standalone_img_becomes_block():
    html = '<html><body><div><img src="b.png" alt="网络拓扑"></div></body></html>'
    assert [b.text for b in parse_html(html.encode("utf-8"))] == ["[图片] 网络拓扑"]


def test_img_without_alt_dropped():
    html = (
        '<html><body><p>正文<img src="icon.png"></p>'
        '<div><img src="x.png" alt=""></div></body></html>'
    )
    assert [b.text for b in parse_html(html.encode("utf-8"))] == ["正文"]


def test_img_in_table_cell_kept():
    html = (
        "<html><body><table>"
        "<tr><th>型号</th><th>外观</th></tr>"
        '<tr><td>AX3000</td><td><img src="a.png" alt="白色"> 备注</td></tr>'
        "<tr><td>AX6000</td><td>黑色</td></tr>"
        "</table></body></html>"
    )
    table = parse_html(html.encode("utf-8"))[0]
    assert table.is_table is True
    assert "[图片] 白色" in table.text and "a.png" not in table.text


def test_img_in_pre_not_replaced():
    """<pre> 里的 <img> 是代码样例内容，不注入 alt 文本。"""
    html = '<html><body><pre>before <img src="a.png" alt="x"> after</pre></body></html>'
    blocks = parse_html(html.encode("utf-8"))
    assert "[图片]" not in blocks[0].text


def test_figcaption_becomes_text_block():
    html = (
        "<html><body><figure>"
        '<img src="a.png" alt="拓扑">'
        "<figcaption>图 1 网络拓扑示意</figcaption>"
        "</figure></body></html>"
    )
    texts = [b.text for b in parse_html(html.encode("utf-8"))]
    assert "[图片] 拓扑" in texts
    assert "图 1 网络拓扑示意" in texts
