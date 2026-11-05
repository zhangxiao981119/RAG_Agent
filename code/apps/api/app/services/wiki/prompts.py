"""wiki 导航页的 prompt 正文（第 3 步）。

★ 版本号在 config/wiki_profile.py 的 WIKI_PROMPT_VERSION，与正文**同源**：
  改动本文件的任何语义（输出字段、字数、术语口径）MUST 同步 bump 版本号，
  否则新旧页面被判为同口径、混在同一张表里 —— 不报错，只是页越用越旧。
  （这就是 WIKI_PROMPT_VERSION 不进环境变量的理由：见 wiki_profile 的模块说明。）

★ 与图抽取 prompt（services/graph/prompts.py）的分工：
  图抽取是「每个片段都要贡献提及」的稠密任务（分批调用）；
  wiki 是「一篇文档一页」的摘要任务（单次调用，输入按 WIKI_INPUT_MAX_CHARS 截断）。
  两者共用 get_llm_service 的 chat 入口，但互不依赖（图不依赖 wiki，wiki 不依赖图）。

★ 输出契约（与 build.py 的解析一一对应，改字段名要同时改两处）：
    {"title": str,
     "summary": str,          # 200~400 字导航摘要
     "sections": [{"heading_path": str, "summary": str,
                   "key_terms": [str], "chunk_ids": [str]}]}
  · sections 的 chunk_id 必须是给定的 id 之一（模型编造的 id 由代码丢弃）
  · key_terms 是名词性术语，代码会统一 sorted 去重（不信任模型自己保证）
"""

from __future__ import annotations

from typing import Final

WIKI_PROMPT_TEMPLATE: Final[str] = """\
你在为企业知识库的每篇文档生成一个「导航页」：帮助读者不读全文就知道这篇文档
讲什么、按什么结构组织、每个小节值得看什么。这是**导航摘要**，不是全文改写。

严格按以下规则工作：

1. 只依据给定的文档片段。片段里没有出现的概念 MUST NOT 编造。
2. title：文档标题（12 字以内，优先使用文档自身出现的标题性短语）。
3. summary：全文导航摘要，200~400 字。说清这篇文档的主题、适用对象与整体结构，
   MUST NOT 复制原文长句，MUST NOT 出现「本文档」「如下所示」这类指代。
4. sections：按文档的章节结构拆 2~8 个小节。每个小节：
   - heading_path：逐字复制该小节片段给定的章节路径（不要自己发明新层级）；
   - summary：2~4 句话说明这个小节讲了什么、读者什么时候该看它；
   - key_terms：3~8 个名词性术语（指标、制度名、流程名、产品名），MUST 是
     片段中实际出现的词，MUST NOT 是动词短语或整句；
   - chunk_ids：该小节内容来自哪些片段 —— 必须**逐字复制**给定的 id 子集。
5. chunk_id 逐字复制给定 id；片段正文里没有的小节不要拆（宁缺毋滥）。
6. 输出**只有 JSON**，不要任何解释文字、不要 Markdown 代码块围栏。

输出格式：
{"title":"文档标题",
 "summary":"200~400 字的全文导航摘要",
 "sections":[{"heading_path":"章节路径","summary":"小节摘要",
              "key_terms":["术语1","术语2"],
              "chunk_ids":["片段id"]}]}

文档片段（共 __CHUNK_COUNT__ 个；id、章节路径与正文用 TAB 分隔，按文档顺序排列）：
__CHUNKS__
"""


def render_wiki_prompt(chunks: list[tuple[str, str, str]]) -> str:
    """把一篇文档的全部（截断后）chunk 渲染成 wiki 生成 prompt。

    chunks 元素为 (chunk_id, heading_path, content)，**按 chunk_index 排序**：
    摘要要反映文档的叙述顺序，乱序输入会让模型编出错误的章节结构。
    """
    lines: list[str] = []
    for chunk_id, heading_path, content in chunks:
        header = f"[{heading_path}]" if heading_path else "[（无章节路径）]"
        lines.append(f"{chunk_id}\t{header}\t{content}")
    body = "\n\n".join(lines) if lines else "（无）"
    return (
        WIKI_PROMPT_TEMPLATE.replace("__CHUNK_COUNT__", str(len(chunks)))
        .replace("__CHUNKS__", body)
    )


# 单页小节上限：模型可能把每 200 字拆成一节（输出膨胀且导航价值趋零）。
# 超出部分按顺序丢弃 —— 8 节覆盖一篇 12K 字输入的合理结构上限。
WIKI_MAX_SECTIONS: Final[int] = 8

# 单节术语上限（与 prompt 的 3~8 呼应，代码截断保证输出不膨胀）
WIKI_MAX_TERMS_PER_SECTION: Final[int] = 12
# 单个术语的长度上限：超长说明模型把整句当术语（与图抽取实体名同款脏输出形态）
WIKI_TERM_MAX_CHARS: Final[int] = 40
