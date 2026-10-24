"""Markdown 解析 —— 按 #/##/### 标题层级切分（手册 §3.3.1 切分优先级 ①）。

表格决策 B1'（窄口版）增补：标准管道表识别。
  · 仅识别「表头行 + 分隔行（| --- | --- |）+ 表体行」的标准 GFM 管道表；
    无分隔行的竖线文本（伪表格）一律按普通文本处理，不做表格化；
  · 产出与 html/xlsx/docx 同款 is_table 契约：cell_to_text 消毒 + TSV
    序列化 + table_header —— chunk 层「整表不切 / 行组切分带表头」原样适用；
  · 可信度不足（无表体行 / 表头全空，与 html 可信度阈值同口径）→
    整组行原样回退普通文本，不产空表块。

M8-P1（verbatim 信号）增补：围栏代码块与列表聚合。
  · 围栏（``` / ~~~）代码块 → 独立 verbatim 块，内容逐行保真（围栏内的
    # 不是标题、缩进不是续行）；未闭合围栏 → 整组行回退普通文本；
  · 连续列表行（- / * / + / 1.）连同缩进续行聚合为一个 verbatim 块 ——
    列表项与编号不再被句子切分拆散；
  · chunk 层对 verbatim 块只按行边界切分（代码行/列表项是原子）。

M8-P2（公式）增补：独立行 $$ 定界的块级公式 → verbatim 块。mermaid 图
  本身是 ``` 围栏，已由 M8-P1 覆盖保真；行内 $..$ 不处理（切分不涉及）。

M8-P3（图片语法卫生）增补：图片语法统一转成「[图片] alt」文本。
  · 覆盖 `![alt](url)`、徽章 `[![alt](img)](link)`、引用式 `![alt][ref]`、
    markdown 里直接写的 `<img alt="...">` 四种写法；
  · URL / base64 data URI 整段丢弃 —— 图片路径是噪声，alt 才是知识
    （一条 base64 data URI 截图能独占几千 token，且毫无检索价值）；
  · alt 为空 → 整个图片不留文本（不产「[图片]」这种无知识占位）；
  · 围栏代码块不参与替换（代码样例里的 ![](...) 是内容，保真）；
  · 表格单元格同样清洗（表里贴图常见，不能只洗正文）。
"""
from __future__ import annotations

import re

from app.services.parse.base import ParsedBlock, cell_to_text

_HEADING_RE = re.compile(r"^(#{1,6})\s+(.*)$")
# 表体/表头行：首尾均有管道，内部至少一个竖线（即至少 2 列）
_PIPE_ROW_RE = re.compile(r"^\s*\|(.+)\|\s*$")
# 分隔行：首管道必选、尾管道可选（GFM 允许省略），每列 :--- / --- / :---: / ---:；
# 列数 >= 2（单列表不构成可检索表格，窄口版不识别）
_SEP_ROW_RE = re.compile(r"^\s*\|(?:\s*:?-{3,}:?\s*\|)+\s*:?-{3,}:?\s*\|?\s*$")
# 单元格内转义竖线（\|）的拆分占位符：拆列时不当边界，还原时恢复原文
_ESC_PIPE = "\x01"
# 围栏代码块：开栏（记录字符与长度用于配对）；缩进 0-3 空格内合法，
# 4+ 空格缩进的 ``` 是代码内容（不 match，被当作围栏体吸收）——GFM 口径
_FENCE_RE = re.compile(r"^\s{0,3}(```+|~~~+)\s*([\w+-]*)\s*$")
# 列表行：无序（- / * / +）或有序（1. / 1)），序号限 3 位防止误吞「1970. 年代」类年份文本
_LIST_RE = re.compile(r"^\s*(?:[-*+]|\d{1,3}[.)])\s+")
# 列表项的缩进续行（聚合时归属前一项，保持列表语义完整）
_INDENT_RE = re.compile(r"^\s+\S")
# M8-P2 块级公式：独立行的 $$ 定界（开栏/闭栏同形）。单行 $$..$$ 不识别
# （窄口防误判：行内公式/金额文本碰巧含 $$ 的场景远多于单行公式块）
_FORMULA_DELIM_RE = re.compile(r"^\s*\$\$\s*$")
# M8-P3 图片语法：URL 是噪声（本地路径 / base64 data URI），alt 才是知识。
# 徽章写法必须整体先匹配：否则内层图替换后留下 `[图片] x](link)` 半个链接
_MD_BADGE_RE = re.compile(r"\[!\[([^\]\n]*)\]\([^)\n]*\)\]\([^)\n]*\)")
# 行内图 ![alt](url)：URL 允许一层配对括号（维基媒体文件名常带 "(123)"）
_MD_IMAGE_RE = re.compile(r"!\[([^\]\n]*)\]\((?:[^()\n]|\([^()\n]*\))*\)")
# 引用式图 ![alt][ref]：定义行 [ref]: url 本身是普通文本，不在本规则范围
_MD_REF_IMAGE_RE = re.compile(r"!\[([^\]\n]*)\]\[[^\]\n]*\]")
# markdown 里直接写的 HTML 图片标签（alt 从属性里取）
_MD_HTML_IMG_RE = re.compile(r"<img\b[^>\n]*>", re.IGNORECASE)
_MD_ALT_ATTR_RE = re.compile(r"""alt\s*=\s*(?:"([^"]*)"|'([^']*)')""", re.IGNORECASE)


def _split_pipe_row(line: str) -> list[str]:
    """管道行 → 单元格列表。处理 \\| 转义（转义竖线不是列边界）。"""
    body = line.strip().strip("|").replace("\\|", _ESC_PIPE)
    return [c.replace(_ESC_PIPE, "|").strip() for c in body.split("|")]


def _image_note(alt: str) -> str:
    """alt → 「[图片] alt」；空 alt 返回空串（无知识的占位不留）。"""
    alt = " ".join(alt.split())
    return f"[图片] {alt}" if alt else ""


def _html_img_alt(tag_text: str) -> str:
    """从 `<img ...>` 标签文本里取 alt 属性值（无 alt / 单双引号都覆盖）。"""
    match = _MD_ALT_ATTR_RE.search(tag_text)
    if match is None:
        return ""
    return match.group(1) if match.group(1) is not None else match.group(2)


def _clean_image_markup(text: str) -> str:
    """图片语法 → 「[图片] alt」，URL / base64 data URI 整段丢弃（M8-P3）。

    替换顺序固定：徽章 → 行内图 → 引用式图 → HTML 标签。徽章必须最先整体
    匹配，否则内层图先被替换成「[图片] alt」，外层链接只剩半个括号。
    """
    text = _MD_BADGE_RE.sub(lambda m: _image_note(m.group(1)), text)
    text = _MD_IMAGE_RE.sub(lambda m: _image_note(m.group(1)), text)
    text = _MD_REF_IMAGE_RE.sub(lambda m: _image_note(m.group(1)), text)
    return _MD_HTML_IMG_RE.sub(lambda m: _image_note(_html_img_alt(m.group(0))), text)


def _cell_text(value: object) -> str:
    """表格单元格文本：先做图片语法清洗，再过 cell_to_text 做行列消毒。"""
    return cell_to_text(_clean_image_markup("" if value is None else str(value)))


def parse_markdown(data: bytes) -> list[ParsedBlock]:
    text = data.decode("utf-8", errors="replace")
    lines = text.split("\n")

    blocks: list[ParsedBlock] = []
    # 当前标题路径栈，每个元素是 (level, title)
    stack: list[tuple[int, str]] = []
    buffer: list[str] = []

    def flush() -> None:
        if not buffer:
            return
        # M8-P3：正文块统一做图片语法清洗（围栏块走独立分支，不经过这里）
        content = _clean_image_markup("\n".join(buffer)).strip()
        if content:
            blocks.append(
                ParsedBlock(
                    text=content,
                    heading_path=" > ".join(title for _, title in stack),
                )
            )
        buffer.clear()

    i = 0
    n = len(lines)
    while i < n:
        line = lines[i]
        match = _HEADING_RE.match(line)
        if match:
            flush()
            level = len(match.group(1))
            title = match.group(2).strip()
            # 弹掉层级 >= 当前的（同级或更深的）
            while stack and stack[-1][0] >= level:
                stack.pop()
            stack.append((level, title))
            i += 1
            continue
        # 围栏代码块（M8-P1）：开栏配对收集，内容逐行保真
        fence_match = _FENCE_RE.match(line)
        if fence_match:
            fence_mark = fence_match.group(1)
            # 收集围栏体：直到配对闭栏（同字符、长度不小于开栏）或 EOF
            j = i + 1
            closed = False
            while j < n:
                m2 = _FENCE_RE.match(lines[j])
                if m2 and m2.group(1)[0] == fence_mark[0] and len(m2.group(1)) >= len(fence_mark):
                    closed = True
                    break
                j += 1
            if closed:
                flush()  # 围栏前的普通文本先落块，保证块序
                body = "\n".join(lines[i + 1 : j]).strip("\n")
                if body.strip():
                    blocks.append(
                        ParsedBlock(
                            text=body,
                            heading_path=" > ".join(title for _, title in stack),
                            verbatim=True,
                        )
                    )
                i = j + 1  # 跳过闭栏
                continue
            # 未闭合围栏 → 整组行回退普通文本（原文保真，不产半截代码块）
            buffer.extend(lines[i:n])
            i = n
            continue
        # 块级公式（M8-P2）：独立行 $$ 定界，内容逐行保真。
        # 公式内的 3.14 / e.g. / x! 会成为句子切分的错误断点，verbatim 块
        # 只按行边界切分（复用 M8-P1 分支），公式行保持原子
        if _FORMULA_DELIM_RE.match(line):
            j = i + 1
            while j < n and not _FORMULA_DELIM_RE.match(lines[j]):
                j += 1
            if j < n:  # 找到闭栏
                flush()  # 公式前的普通文本先落块，保证块序
                body = "\n".join(lines[i + 1 : j]).strip("\n")
                if body.strip():
                    blocks.append(
                        ParsedBlock(
                            text=body,
                            heading_path=" > ".join(title for _, title in stack),
                            verbatim=True,
                        )
                    )
                i = j + 1  # 跳过闭栏
                continue
            # 未闭合 → 整组行回退普通文本
            buffer.extend(lines[i:n])
            i = n
            continue
        # 列表聚合（M8-P1）：连续列表行 + 缩进续行 → 一个 verbatim 块。
        # 聚合块可能超限，chunk 层按行边界切分兜底（列表项是原子）。
        if _LIST_RE.match(line):
            j = i + 1
            while j < n and (_LIST_RE.match(lines[j]) or _INDENT_RE.match(lines[j])):
                j += 1
            flush()  # 列表前的普通文本先落块，保证块序
            blocks.append(
                ParsedBlock(
                    text="\n".join(lines[i:j]),
                    heading_path=" > ".join(title for _, title in stack),
                    verbatim=True,
                )
            )
            i = j
            continue
        # 标准管道表：表头行 + 紧随分隔行 才构成表格起点（窄口判定）
        if _PIPE_ROW_RE.match(line) and i + 1 < n and _SEP_ROW_RE.match(lines[i + 1]):
            # 收集表体：连续管道行，直到首个非管道行
            j = i + 2
            while j < n and _PIPE_ROW_RE.match(lines[j]):
                j += 1
            # grid[0] 为表头，其余为表体（分隔行仅作边界判定，不产出）
            grid = [_split_pipe_row(line)] + [_split_pipe_row(lines[k]) for k in range(i + 2, j)]
            header = [_cell_text(c) for c in grid[0]]
            if len(grid) >= 2 and any(header):
                flush()  # 表格前的普通文本先落块，保证块序
                blocks.append(
                    ParsedBlock(
                        text="\n".join(
                            "\t".join(_cell_text(c) for c in row) for row in grid
                        ),
                        heading_path=" > ".join(title for _, title in stack),
                        is_table=True,
                        table_header=header,
                    )
                )
            else:
                # 可信度不足 → 整组行回退普通文本（原文保真）
                buffer.extend(lines[i:j])
            i = j
            continue
        buffer.append(line)
        i += 1

    flush()
    return blocks
