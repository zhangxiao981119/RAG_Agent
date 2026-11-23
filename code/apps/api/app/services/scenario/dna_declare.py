"""`dna_declared` 的唯一生成处（写入侧）。

`dna_declared` 是**声明镜像**：只用于审计与漂移检测，`resolver` 绝不读它
（DNA 只从三个 `current_*_profile()` 读，fail-closed）。

★ 生成的形状 MUST 与迁移 `0028_add_scenarios.py::_SEED_DNA` **同构**：
  4 个代码常量落值 + 4 个环境绑定项只声明**字段名**、不落值。
  - 与种子同构 ⇒ 任何路径产出的 dna_declared 形状相同，`ck_..._dna_keyset`
    之外不需要第二条一致性规则；
  - 落环境值等于把「写这一行时的部署」冻进审计内容，模型一换这行就成了会过期的
    断言；而 S-9 的漂移检测只对 is_builtin 行生效，非内置行写值只会制造一堆
    永远没人看的差异。
"""
from __future__ import annotations

from typing import Final

from app.services.scenario.snapshot import current_dna

ENV_BOUND_FIELDS: Final[tuple[str, ...]] = (
    "embedding_model",
    "embedding_dim",
    "graph_extractor_model",
    "wiki_generator_model",
)
"""只声明字段名、不落值的四项（与迁移 `_SEED_DNA['env_bound']` 逐字相同）。"""


def build_dna_declared() -> dict[str, object]:
    """生成版本行的 `dna_declared` 声明镜像（与迁移种子同构）。"""
    index, graph, wiki = current_dna()
    return {
        "embedding_text_version": index.embedding_text_version,
        "chunker_version": index.chunker_version,
        "graph_prompt_version": graph.prompt_version,
        "wiki_prompt_version": wiki.prompt_version,
        "env_bound": list(ENV_BOUND_FIELDS),
    }
