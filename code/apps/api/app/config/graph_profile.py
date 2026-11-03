"""实体抽取口径单一真源 —— 图里那些行是「按什么口径抽出来的」。

抽取口径 = 两个要素：

  · extractor_model  用哪个模型抽的（不同模型抽出的实体集不同构，不可混用）
  · prompt_version   抽取 prompt **正文**的版本（正文在 services/graph/prompts.py）

★ 为什么 prompt_version 是代码常量而不是环境变量：
  版本号必须与正文同源。放进环境变量 = 允许「版本号说 v2、实际跑 v1」——
  于是旧口径的数据被当成新口径混算，而且没有任何报错。放在这里，改正文的人
  一定会看到这个常量（它就是那道提醒），这跟 EMBEDDING_TEXT_VERSION 是同款纪律。

★ 为什么要落到每行上而不是「查的时候现算」：
  换模型 / 改 prompt 后，新旧图数据**不同构**。检索侧按 (extractor_model,
  prompt_version) 过滤，旧口径的行被**排除**而不是混算 —— fail-closed：
  宁可查不到（图路对这篇文档暂时不生效，三路照常），不可查错。

★ 与 index_profile 的分工：
  index_profile 管「向量怎么算」，本模块管「实体与关系怎么抽」。
  两者互不依赖：换嵌入口径不必重抽图（chunk 行不变），换抽取口径不必重算向量。
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Final

from app.config.settings import get_settings

GRAPH_PROMPT_VERSION: Final[str] = "gprompt-v1"
"""抽取 prompt 正文版本。

  - gprompt-v1：services/graph/prompts.py 的 EXTRACT_PROMPT_TEMPLATE 首版。
  改动该模板的**任何语义**（输出字段、类型闭集、判定要求）时 MUST 同步 bump
  （如 gprompt-v2），否则新旧抽取结果被判为同口径、混在同一张图里检索。
"""


@dataclass(frozen=True)
class GraphProfile:
    """一次抽取口径的完整快照值。"""

    extractor_model: str
    prompt_version: str

    @property
    def fingerprint(self) -> str:
        """口径指纹 = sha1(model|prompt)[:12]（与 IndexProfile 同口径）。"""
        raw = "|".join((self.extractor_model, self.prompt_version))
        return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:12]


def current_graph_profile() -> GraphProfile:
    """当前应使用的抽取口径（唯一取值入口）。

    MUST 作为唯一入口 —— **写入盖章**（graph_entity_mentions / graph_relations 的
    两个口径列、documents 的三个 graph_* 口径列）与**读取过滤**（neighbors 的
    WHERE）都从这里读。禁止两处各取一次：改配置后一边新一边旧，表现为
    「刚抽完的文档查不到」，且没有任何报错。

    ★ GRAPH_EXTRACTOR_MODEL 留空时解析为 settings.llm_model 的**实际值**：
      落库必须是具体模型名，不能落空串 —— 空串会让「换主模型」这件事在图上
      悄悄生效（旧数据口径为空，新数据口径为新模型，新旧仍能区分，但
      「空 = 跟随」这个语义一旦落库就再也无法回溯当时到底用了哪个模型）。
    """
    settings = get_settings()
    extractor_model = (settings.graph_extractor_model or "").strip() or settings.llm_model
    return GraphProfile(
        extractor_model=extractor_model,
        prompt_version=GRAPH_PROMPT_VERSION,
    )
