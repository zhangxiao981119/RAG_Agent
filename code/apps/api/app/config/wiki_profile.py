"""wiki 生成口径单一真源 —— 导航页是「按什么口径生成的」。

口径 = generator_model（生成模型）+ prompt_version（prompt 正文版本）。

★ 本模块在第 1 步只落**版本号与口径取值函数**，不含任何生成逻辑：
  wiki 的生成 / 读接口 / 术语扩展属第 3、4 步。此处先建好，是为了让
  settings 的 WIKI_* 配置项、启动自检与能力横幅有同一个口径真源，
  避免第 3 步再回头改自检与横幅（改一处漏一处的典型来源）。

★ prompt_version 是代码常量而非环境变量：理由同 graph_profile ——
  版本号必须与 prompts.py 的正文同源，否则可以「版本号说 v2、实际跑 v1」。
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Final

from app.config.settings import get_settings

WIKI_PROMPT_VERSION: Final[str] = "wprompt-v1"
"""wiki 生成 prompt 正文版本。

  - wprompt-v1：预留（第 3 步 services/wiki/prompts.py 的首版正文）。
  正文落地时若语义与设计稿不同（字段、字数、术语抽取口径）MUST 同步 bump。
"""


@dataclass(frozen=True)
class WikiProfile:
    """一次 wiki 生成口径的完整快照值。"""

    generator_model: str
    prompt_version: str

    @property
    def fingerprint(self) -> str:
        """口径指纹 = sha1(model|prompt)[:12]（与 GraphProfile 同口径）。"""
        raw = "|".join((self.generator_model, self.prompt_version))
        return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:12]


def current_wiki_profile() -> WikiProfile:
    """当前应使用的 wiki 生成口径（唯一取值入口，理由同 current_graph_profile）。"""
    settings = get_settings()
    generator_model = (settings.wiki_generator_model or "").strip() or settings.llm_model
    return WikiProfile(
        generator_model=generator_model,
        prompt_version=WIKI_PROMPT_VERSION,
    )
