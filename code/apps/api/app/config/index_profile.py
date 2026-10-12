"""索引口径单一真源 —— 向量是怎么算出来的。

索引口径 = 「chunk 的向量由什么参数产出」的完整描述，四个要素：

  · embedding_model        用哪个模型算的（不同模型向量空间不同构，不可混用）
  · embedding_dim          维度（不同维度不可比）
  · embedding_text_version 喂给模型的**文本公式**版本（to_embedding_text 变了就要 bump）
  · chunker_version        分块参数（由 CHUNK_* 常量派生，改参数自动变）

★ 为什么要落到每个 chunk 上：
  换模型 / 改向量文本公式后，新旧向量**不同构**。把它们混在一个向量空间里检索会
  「静默劣化」—— 不报错，只是答不准（见 scripts/reindex_embeddings.py 的原始说明）。
  落库后可支撑两件事：
    ① 检索时按当前口径过滤，物理隔离不同构向量（fail-closed：宁可查不到，不可查错）；
    ② 脚本 / 启动检查能算出「有多少 chunk 已过期、需要重算」。

★ 三要素 vs 四要素：
  「向量是否同构」只看前三者（模型 / 维度 / 文本公式）；
  chunker_version 影响的是**分块粒度**而非向量空间 —— 同一模型同一公式下，旧切法的
  向量仍可与新向量比较、检索。所以 fingerprint（用于过滤）**不含** chunker_version，
  它只作「是否建议重切」的提示（可观测，不硬过滤）。

★ embedding_text_version 必须**人工** bump：
  向量文本公式是**语义**变更，代码无法自动感知（改了函数体，输出也可能恰好不变）。
  改动 chunk.to_embedding_text() 时 MUST 同步 bump 本常量 —— 不是靠记忆，而是这个
  常量本身就是那道「提醒」：看到它就会去核对公式有没有变。
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Final

from app.config import decisions
from app.config.settings import get_settings

EMBEDDING_TEXT_VERSION: Final[str] = "etext-v1"
"""喂给 embedding 的文本公式版本。

  - etext-v1：当前 to_embedding_text() = "[heading_path]\\n" 前缀 + content。
  改动该函数语义时 MUST 同步 bump（如 etext-v2），否则新旧向量被判为同构、混用检索。
"""


def _chunker_version() -> str:
    """分块参数指纹：由 CHUNK_* 常量派生，参数一改自动变，无需人工记。"""
    return (
        f"c{decisions.CHUNK_TARGET_TOKENS}"
        f"-{decisions.CHUNK_MAX_TOKENS}"
        f"-{decisions.CHUNK_OVERLAP_TOKENS}"
    )


@dataclass(frozen=True)
class IndexProfile:
    """一次索引口径的完整快照值。"""

    embedding_model: str
    embedding_dim: int
    embedding_text_version: str
    chunker_version: str

    @property
    def fingerprint(self) -> str:
        """影响「向量是否同构」的三要素指纹（不含 chunker，见模块说明）。"""
        raw = "|".join(
            (
                self.embedding_model,
                str(self.embedding_dim),
                self.embedding_text_version,
            )
        )
        return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:12]


def current_index_profile() -> IndexProfile:
    """当前应使用的索引口径（取自 settings + decisions）。

    MUST 作为唯一的取值入口 —— 写入 chunk 与检索过滤都从这里读，
    避免两处各取一次、改配置后一边新一边旧。
    """
    settings = get_settings()
    return IndexProfile(
        embedding_model=settings.embedding_model,
        embedding_dim=settings.embedding_dim,
        embedding_text_version=EMBEDDING_TEXT_VERSION,
        chunker_version=_chunker_version(),
    )