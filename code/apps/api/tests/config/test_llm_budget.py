"""decisions.check_llm_budget —— 启动期 LLM 预算校验的测试。

为什么值得单独测：这条校验**只在启动时跑一次**，跑错了没人会发现 ——
服务照常起来，直到某个超长请求炸 400（而那种请求往往出现在演示/压测现场）。
所以必须用测试把每条拒绝路径钉住，而不是靠"上线前记得看一眼"。
"""
from __future__ import annotations

import logging

import pytest

from app.config import decisions


def test_normal_config_passes():
    """当前口径（v4-flash + 预留内的输出上限）应当放行。"""
    decisions.check_llm_budget(
        "deepseek-v4-flash", decisions.CONTEXT_OUTPUT_RESERVE_TOKENS
    )


def test_rejects_output_limit_over_reserve():
    """输出上限超过从窗口里扣掉的预留 → 拒绝启动。

    这条防的是「预留 4K 却允许模型写 8K」——那样预留名不副实，
    prompt 体积 + 输出会越过模型窗口。
    """
    with pytest.raises(RuntimeError, match="llm_max_output_tokens"):
        decisions.check_llm_budget(
            "deepseek-v4-flash", decisions.CONTEXT_OUTPUT_RESERVE_TOKENS + 1
        )


def test_rejects_window_over_model_capability(monkeypatch):
    """窗口上限 + 输出预留越过模型能力 → 拒绝启动。

    用一个假的小窗口模型模拟：这条正是当初缺失的保护 ——
    128K 的窗口配置配在一个小窗口模型上，没有任何环节会报错。
    """
    monkeypatch.setitem(decisions.MODEL_CONTEXT_WINDOWS, "tiny-model", 8_000)
    with pytest.raises(RuntimeError, match="超过模型能力"):
        decisions.check_llm_budget("tiny-model", 1_000)


def test_unknown_model_warns_but_does_not_block(caplog):
    """未登记模型 → 告警放行。

    私有化部署常接自建网关，模型名不可枚举；无法验证 ≠ 有问题。
    但也不能静默跳过 —— 所以断言 warning 确实打出来了。
    """
    with caplog.at_level(logging.WARNING, logger="app.config.decisions"):
        decisions.check_llm_budget("some-self-hosted-model", 1_000)

    assert any("未登记" in r.getMessage() for r in caplog.records), "必须留下告警"


def test_registered_models_cover_settings_default():
    """settings 的**默认**模型 MUST 在登记表里 —— 否则默认配置下校验形同虚设。

    ★ 断言的是类字段默认值，不是 get_settings() 的结果：
      后者会被 .env / 环境变量覆盖，那属于运行时配置，可能指向自建网关的
      任意模型名（那种情况由 check_llm_budget 打 warning 处理）。
      本测试要钉的是"代码里的默认值与登记表同步"，这条不变量必须与运行环境无关。
    """
    from app.config.settings import Settings

    default_model = Settings.model_fields["llm_model"].default
    assert default_model in decisions.MODEL_CONTEXT_WINDOWS, (
        f"默认模型 {default_model!r} 不在 MODEL_CONTEXT_WINDOWS，"
        "意味着默认配置下窗口校验不会生效"
    )
