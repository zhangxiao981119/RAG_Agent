"""F.1（NO-DB 层）：迁移 0028 的种子行字面量、checksum 同源、链位置、零破坏性 DML。

这一层**不需要 PG**：断言对象是迁移模块里的常量与源码文本。它们的价值是
「CI 红在改代码的那一刻，而不是迁移跑到生产的那一刻」：

  · 代码里 bump 了 `EMBEDDING_TEXT_VERSION` 却没同步迁移 ⇒ 这里红；
  · 迁移里悄悄加了一条 `UPDATE documents` ⇒ 这里红（零影响承诺 Z-1 的语言级防线）。

★ 4 个代码常量用**字面量**断言（不是「读常量再比常量」）：自证式断言永远不会红，
  而它要防的恰恰是「常量被 bump 了」。
"""
from __future__ import annotations

import pathlib
import re

import pytest

from app.config import decisions
from app.config.graph_profile import GRAPH_PROMPT_VERSION, current_graph_profile
from app.config.index_profile import EMBEDDING_TEXT_VERSION, current_index_profile
from app.config.settings import get_settings
from app.config.wiki_profile import WIKI_PROMPT_VERSION, current_wiki_profile
from app.services.scenario.snapshot import compute_checksum


def _expanded_dna(dna: dict) -> dict[str, object]:
    """展开 `env_bound`：把「由环境决定」的 4 项按当前配置解析成实际值。

    4 个字段名 MUST 与三个 `current_*_profile()` 的字段一一对应 ——
    这是「声明镜像 == 平台口径」能逐字段比较的前提。
    """
    settings = get_settings()
    resolved: dict[str, object] = {}
    for name in dna["env_bound"]:
        if name == "embedding_model":
            resolved[name] = settings.embedding_model
        elif name == "embedding_dim":
            resolved[name] = settings.embedding_dim
        elif name == "graph_extractor_model":
            # 空串必须解析为 llm_model 的**实际值**（graph_profile.py:61-65 的纪律）
            resolved[name] = current_graph_profile().extractor_model
        elif name == "wiki_generator_model":
            resolved[name] = current_wiki_profile().generator_model
        else:
            raise AssertionError(f"env_bound 里出现了未定义的项：{name}")
    return resolved


def test_seed_dna_matches_platform_profiles(migration):
    """展开 env_bound 后，8 个 DNA 字段与三个 current_*_profile() 逐字段相等。

    8 = IndexProfile(4) + GraphProfile(2) + WikiProfile(2)。
    """
    dna = migration._SEED_DNA
    index = current_index_profile()
    graph = current_graph_profile()
    wiki = current_wiki_profile()
    env = _expanded_dna(dna)

    # ── 4 个代码常量：字面量断言（防「读常量再比常量」的自证式假测试）──
    assert dna["embedding_text_version"] == "etext-v1"
    assert dna["chunker_version"] == "c400-800-60"
    assert dna["graph_prompt_version"] == "gprompt-v1"
    assert dna["wiki_prompt_version"] == "wprompt-v1"

    # ── 8 个字段逐项比对（左 = 迁移声明，右 = 代码当前值）──
    pairs = {
        "embedding_model": (env["embedding_model"], index.embedding_model),
        "embedding_dim": (env["embedding_dim"], index.embedding_dim),
        "embedding_text_version": (dna["embedding_text_version"], index.embedding_text_version),
        "chunker_version": (dna["chunker_version"], index.chunker_version),
        "graph_extractor_model": (env["graph_extractor_model"], graph.extractor_model),
        "graph_prompt_version": (dna["graph_prompt_version"], graph.prompt_version),
        "wiki_generator_model": (env["wiki_generator_model"], wiki.generator_model),
        "wiki_prompt_version": (dna["wiki_prompt_version"], wiki.prompt_version),
    }
    assert len(pairs) == 8
    for name, (declared, actual) in pairs.items():
        assert declared == actual, f"DNA 字段 {name} 漂移：迁移={declared!r} 代码={actual!r}"


def test_seed_env_bound_is_declaration_not_value(migration):
    """4 个环境绑定项**不落值**：既不在种子行里出现值，也必须被 env_bound 声明。

    写死等于把某个部署环境的配置写进所有部署的迁移（§A.6 障碍 1）。
    """
    dna = migration._SEED_DNA
    assert dna["env_bound"] == [
        "embedding_model",
        "embedding_dim",
        "graph_extractor_model",
        "wiki_generator_model",
    ]
    for name in dna["env_bound"]:
        assert name not in dna, f"{name} 是环境绑定项，MUST NOT 在迁移里落值"
    settings = get_settings()
    # 反证：确实与 settings 有关（否则「不落值」这条断言可能因字段名写错而空转）
    assert isinstance(settings.embedding_model, str)
    assert isinstance(settings.embedding_dim, int)


def test_seed_policy_is_empty(migration):
    """种子版本行是空覆盖集 + unrestricted + 空 kb_ids。

    这三条合起来是**零影响的数学表达**：空覆盖集 ⇒ 解析结果 = 平台默认值。
    """
    assert migration._SEED_POLICY == {}
    assert migration._SEED_KB_IDS == []
    assert migration._SEED_BINDING_MODE == "unrestricted"
    # C-B1 的联动前提：unrestricted 必须配空数组
    assert migration._SEED_KB_IDS == []


def test_seed_checksum_matches_runtime(migration):
    """迁移内计算出的 checksum == 运行时 compute_checksum()（同源守门人）。

    DB 无法重算（`jsonb::text` 的键序与空格不受控），所以这一条只能靠测试。
    """
    runtime = compute_checksum(
        policy=migration._SEED_POLICY,
        kb_ids=migration._SEED_KB_IDS,
        binding_mode=migration._SEED_BINDING_MODE,
        dna_declared=migration._SEED_DNA,
    )
    assert runtime == migration._SEED_CHECKSUM
    # C-V 兜底格式：16 位小写 hex
    assert re.fullmatch(r"[0-9a-f]{16}", runtime)


def test_migration_chain_position(migration):
    """链位置：0028 ← 0027，且分支/依赖标签为空（单一线性链）。"""
    assert migration.revision == "0028"
    assert migration.down_revision == "0027"
    assert migration.branch_labels is None
    assert migration.depends_on is None


def test_migration_has_no_destructive_dml(migration):
    """迁移**不碰既有表的数据**：没有任何 UPDATE / DELETE / TRUNCATE / ALTER COLUMN。

    这是零影响承诺 Z-1 的语言级防线。对照：0011 在 `chunks` 上做 UPDATE + 收紧
    NOT NULL，那才是需要回滚预案的迁移；本迁移只 CREATE / ADD COLUMN / INSERT 新表。
    """
    source = pathlib.Path(migration.__file__).read_text(encoding="utf-8")
    upgrade_body = source.split("def upgrade()")[1].split("def downgrade()")[0]
    for keyword in ("UPDATE ", "DELETE ", "TRUNCATE ", "ALTER COLUMN", "DROP COLUMN"):
        assert keyword not in upgrade_body, f"upgrade() 里出现了破坏性语句：{keyword}"
    # 唯一打数据的语句是新表的种子 INSERT，且必须幂等
    assert "ON CONFLICT (tenant_id, scenario_key) DO NOTHING" in upgrade_body


def test_migration_keyset_and_version_constants(migration):
    """迁移的 policy 闭集 == 设计规定的 4 个 Step 3 键。

    这里只断言**迁移自己的**常量；与运行时 `WIRED_KEYS` 的相等性由
    `test_resolver_policy.py::test_policy_keyset_matches_migration` 负责（S-1）。
    """
    assert migration._POLICY_KEYS == [
        "recall_top_k",
        "rerank_enabled",
        "relevance_threshold_vector",
        "relevance_threshold_rerank",
    ]


@pytest.mark.parametrize(
    "const, expected",
    [
        (EMBEDDING_TEXT_VERSION, "etext-v1"),
        (GRAPH_PROMPT_VERSION, "gprompt-v1"),
        (WIKI_PROMPT_VERSION, "wprompt-v1"),
    ],
)
def test_code_constants_are_the_ones_seeded(const, expected):
    """**反向**断言：代码常量的当前值就是迁移里写的那个。

    与 `test_seed_dna_matches_platform_profiles` 互补 —— 那一条比的是「迁移 vs 代码」，
    这一条直接钉住代码值本身，避免两边同时被改成同一个错值而测试仍绿。
    """
    assert const == expected


def test_chunker_version_literal_derives_from_decisions():
    """`c400-800-60` 必须真的是 CHUNK_* 常量的派生结果（不是随手写死的串）。"""
    assert (
        f"c{decisions.CHUNK_TARGET_TOKENS}"
        f"-{decisions.CHUNK_MAX_TOKENS}"
        f"-{decisions.CHUNK_OVERLAP_TOKENS}"
    ) == "c400-800-60"
