"""第 4 步常量契约的一致性单测（设计 §3 / N2 / N3 / N6）。

钉住四件事：
  1. N3 裁决的 7 个先验值**原样落地**（禁止因某轮评测不达标临时调）；
  2. 跨模块上限一致性（`WIKI_NAV_MAX_TERM_CHARS <= WIKI_TERM_MAX_CHARS`）——
     这条**不进** `decisions.self_check()`（配置层禁止反向 import 服务层）；
  3. `self_check()` 的四条断言真的会拒绝非法值（防「写了但恒真」），
     且函数体里没有 `app.services.*` 的 import（关闭态零加载纪律）；
  4. N2 的新配置项 `wiki_nav_timeout_ms` 同步进了跨角色白名单与快照
     （经验层：靠字符串键取子集的逻辑 MUST 有测试钉住，拼错会静默少算一项）。
"""
from __future__ import annotations

import inspect

import pytest

from app.config import decisions
from app.config.config_profile import CROSS_ROLE_FIELDS, runtime_config_snapshot
from app.config.settings import get_settings
from app.services.wiki.prompts import WIKI_TERM_MAX_CHARS


def test_n3_prior_values_are_landed_verbatim():
    """先验值，首轮实测后重设；改它们 = 217 条基线不可比，须走代码评审。"""
    assert decisions.WIKI_NAV_MAX_TERMS == 2
    assert decisions.WIKI_NAV_MAX_FTS_TOKENS == 6
    assert decisions.WIKI_NAV_MAX_PAGES == 1
    assert decisions.WIKI_NAV_MAX_TERM_CHARS == 12
    assert decisions.WIKI_NAV_MIN_CJK_CHARS == 2
    assert decisions.WIKI_NAV_MIN_ASCII_CHARS == 4
    assert decisions.WIKI_NAV_CANDIDATE_PAGE_GUARD == 5_000
    # N6：lexicon 别名独立预算与 wiki 二级预算同范式、同量级
    assert decisions.LEXICON_FTS_ALIAS_MAX_TERMS == decisions.WIKI_NAV_MAX_FTS_TOKENS == 6


def test_cross_module_upper_bound_is_consistent():
    """检索侧词元上限 MUST <= 第 3 步生成侧上限，否则页里根本不会有这么长的术语。"""
    assert decisions.WIKI_NAV_MAX_TERM_CHARS <= WIKI_TERM_MAX_CHARS
    assert WIKI_TERM_MAX_CHARS == 40


def test_internal_consistency_holds():
    assert 1 <= decisions.WIKI_NAV_MAX_TERMS <= 2
    assert decisions.WIKI_NAV_MAX_FTS_TOKENS >= decisions.WIKI_NAV_MAX_TERMS
    assert decisions.WIKI_NAV_MIN_CJK_CHARS <= decisions.WIKI_NAV_MAX_TERM_CHARS
    assert decisions.WIKI_NAV_MIN_ASCII_CHARS <= decisions.WIKI_NAV_MAX_TERM_CHARS
    assert decisions.WIKI_NAV_CANDIDATE_PAGE_GUARD > 0


def test_self_check_has_no_reverse_import_of_services():
    """配置层反向依赖服务层 → 启动期拉出检索模块，破坏「关闭态零加载」。"""
    src = inspect.getsource(decisions.self_check)
    assert "from app.services" not in src
    assert "import app.services" not in src
    # 模块级也不许（避免有人把跨模块一致性挪到 import 区）
    module_src = inspect.getsource(decisions)
    assert "from app.services" not in module_src.split("def self_check")[0]


@pytest.mark.parametrize(
    ("name", "bad_value", "message"),
    [
        ("WIKI_NAV_MAX_TERMS", 3, "WIKI_NAV_MAX_TERMS MUST"),
        ("WIKI_NAV_MAX_FTS_TOKENS", 1, "FTS token 预算 MUST"),
        ("WIKI_NAV_MAX_PAGES", 2, "本步只支持单页扩展"),
        ("WIKI_NAV_MIN_CJK_CHARS", 13, "词元长度下限 MUST"),
    ],
)
def test_self_check_rejects_invalid_wiki_nav_constants(monkeypatch, name, bad_value, message):
    """四条断言逐条可证伪 —— 不是写了但永远不触发的装饰。"""
    monkeypatch.setattr(decisions, name, bad_value)
    with pytest.raises(RuntimeError, match=message):
        decisions.self_check()


def test_wiki_nav_selective_min_terms_is_landed():
    """WIKI_NAV_SELECTIVE 路由阈值（站位值，待 217 条 A/B 标定）原样落地为 3。"""
    assert decisions.WIKI_NAV_SELECTIVE_MIN_TERMS == 3
    # 与 ILIKE_MAX_TERMS 同源上界：len(ilike_terms) 上限即 3，故 3 = 最保守活跃值
    assert decisions.WIKI_NAV_SELECTIVE_MIN_TERMS <= decisions.ILIKE_MAX_TERMS


@pytest.mark.parametrize(
    ("bad_value", "message"),
    [
        (0, "WIKI_NAV_SELECTIVE_MIN_TERMS MUST"),  # 低于下界 1
        (decisions.ILIKE_MAX_TERMS + 1, "WIKI_NAV_SELECTIVE_MIN_TERMS MUST"),  # 超上界 = 永不跳过（空操作）
    ],
)
def test_self_check_rejects_invalid_wiki_nav_selective_min_terms(monkeypatch, bad_value, message):
    """两条边界断言逐条可证伪（模型 on tests/graph/test_self_check.py 的 monkeypatch 范式）。

    自 decisions.py:1300 —— WIKI_NAV_SELECTIVE_MIN_TERMS MUST ∈ [1, ILIKE_MAX_TERMS]，
    否则超上限会让路由变成「永不跳过」的空操作。monkeypatch 在用例级自动 undo，
    不污染其它用例对站立值 3 的断言。
    """
    monkeypatch.setattr(decisions, "WIKI_NAV_SELECTIVE_MIN_TERMS", bad_value)
    with pytest.raises(RuntimeError, match=message):
        decisions.self_check()


def test_self_check_passes_at_legal_standing_value(monkeypatch):
    """恢复合法站立值后 self_check() MUST 通过（证明上面的拒绝路径不是「恒拒绝」）。"""
    monkeypatch.setattr(decisions, "WIKI_NAV_SELECTIVE_MIN_TERMS", 3)
    # 不抛异常即通过
    decisions.self_check()


def test_wiki_nav_timeout_is_in_settings_and_cross_role_snapshot():
    """N2：settings 一份真源 + CROSS_ROLE_FIELDS / runtime_config_snapshot 同步。"""
    settings = get_settings()
    assert settings.wiki_nav_timeout_ms == 50
    assert 10 <= settings.wiki_nav_timeout_ms <= 200
    assert "wiki_nav_timeout_ms" in CROSS_ROLE_FIELDS
    assert runtime_config_snapshot()["wiki_nav_timeout_ms"] == settings.wiki_nav_timeout_ms
