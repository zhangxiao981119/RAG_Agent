"""实体 / 关系抽取的 prompt 正文。

★ 版本号在 config/graph_profile.py 的 GRAPH_PROMPT_VERSION，与正文**同源**：
  改动本文件的任何语义（输出字段、类型闭集、判定要求）MUST 同步 bump 版本号，
  否则新旧抽取结果被判为同口径、混在同一张图里检索 —— 不报错，只是图越用越脏。
  （这就是 GRAPH_PROMPT_VERSION 不进环境变量的理由：见 graph_profile 的模块说明。）

★ 为什么不用 str.format 渲染：正文里含 JSON 示例的花括号，format 会把它当占位符。
  用显式占位串 `__CHUNKS__` 替换，只有一处替换点，也不存在转义地狱。

★ 输出契约（与 extract.py 的解析一一对应，改字段名要同时改两处）：
    {"entities":[{"name","type","aliases"}],
     "mentions":[{"name","chunk_id","surface","confidence"}],
     "relations":[{"src","dst","relation"}]}
  · entities 是**全篇去重后**的实体清单（归一由代码做，不让模型自己归一 ——
    模型写 `NFKC`/`lower` 这类规则必然不一致，而一致性是合并的前提）
  · mentions 的 chunk_id 必须是给定的 id 之一（模型输出不存在的 id 会被丢弃）
  · relations 的 src/dst 必须是 entities 里的 name（按字面匹配）
"""
from __future__ import annotations

from typing import Final

from app.config import decisions

EXTRACT_PROMPT_TEMPLATE: Final[str] = """\
你在为一个企业知识库构建实体图。任务：从下面给出的文档片段里抽取**实体**、
实体在哪个片段出现（提及），以及实体之间的关系。

严格按以下规则工作：

1. 只依据给定的片段内容。片段里没有出现的实体 MUST NOT 编造。
2. 实体类型只能是这 8 个之一：PERSON / ORG / PRODUCT / TERM / POLICY / PROCESS / SYSTEM / OTHER。
   - ORG：公司、部门、组织；PERSON：人名、岗位角色；
   - PRODUCT：产品、型号、软件；TERM：专业术语、指标、专有名词；
   - POLICY：制度、规定、办法；PROCESS：流程、步骤；
   - SYSTEM：系统、平台、工具；OTHER：确实无法归类的。
3. 实体名 MUST 是**名词性短语**，长度不超过 40 个字符。
   MUST NOT 把整句话、带标点的长句、或"根据上述内容""本办法"这类指代当实体名。
4. entities 是全篇**去重后**的清单：同一实体在多个片段出现只写一条，取最完整的那次写法。
   不要自己做大小写归一、全半角归一或同义词合并 —— 归一由系统统一完成。
5. mentions：每个实体在每个出现过的片段各写一条。chunk_id 必须**逐字复制**给定的 id，
   surface 写该片段里出现的原始写法。confidence 是你对该提及的置信度（0~1）。
6. relations：只写片段中**明确表述**的关系（同句或紧邻句），谓词用 2~8 个字的动宾/动词短语
   （如"负责""包含""审批""属于"）。MUST NOT 推断未明说的因果或归属。
   src 与 dst 必须是 entities 里出现过的实体名，且两者不同。
7. 每个片段最多 12 个实体、12 条关系；宁少勿滥 —— 噪声实体会把图扩展变成随机漫步。
8. 输出**只有 JSON**，不要任何解释文字、不要 Markdown 代码块围栏。字段缺失时用空数组。

输出格式：
{"entities":[{"name":"实体名","type":"ORG","aliases":["别名"]}],
 "mentions":[{"name":"实体名","chunk_id":"片段id","surface":"原文写法","confidence":0.9}],
 "relations":[{"src":"头实体名","dst":"尾实体名","relation":"负责"}]}

待抽取片段（共 __CHUNK_COUNT__ 个；id 与正文用 TAB 分隔）：
__CHUNKS__
"""


def render_extract_prompt(chunks: list[tuple[str, str, str]]) -> str:
    """把一批 chunk 渲染成抽取 prompt。

    chunks 元素为 (chunk_id, heading_path, content)。heading_path 作为上下文提示
    （章节标题常含实体所属的业务域），但**不参与**实体名匹配。
    """
    lines: list[str] = []
    for chunk_id, heading_path, content in chunks:
        # TAB 分隔 id 与正文：正文里可能含冒号/换行，用「行首 id」最容易让模型逐字复制
        header = f"[{heading_path}] " if heading_path else ""
        lines.append(f"{chunk_id}\t{header}{content}")
    body = "\n\n".join(lines) if lines else "（无）"
    return (
        EXTRACT_PROMPT_TEMPLATE.replace("__CHUNK_COUNT__", str(len(chunks)))
        .replace("__CHUNKS__", body)
    )


# 一次调用只处理 GRAPH_EXTRACT_BATCH_CHUNKS 个片段，所以「单批实体上限」直接复用
# 单 chunk 上限 × 批量 —— 让 prompt 里的规则与代码截断口径同源（否则模型按 A 规则
# 写、代码按 B 规则砍，被砍掉的往往是重要的那个）。
EXTRACT_MAX_ENTITIES_PER_CALL: Final[int] = (
    decisions.GRAPH_MAX_ENTITIES_PER_CHUNK * decisions.GRAPH_EXTRACT_BATCH_CHUNKS
)
