"""`fake_settings()`：一个**不会腐烂**的假配置工厂（全仓测试共用）。

★ 它为什么存在（2026-11 这次腐烂的实录，别删）：
  Step 3 给 `retrieve/` 加了两个配置读取 ——
  `_graph_should_recall` 读 `graph_recall_selective`、
  `_wiki_should_nav` 读 `wiki_nav_selective`。
  而两个测试文件当时是这样造配置的：

      monkeypatch.setattr(retrieve_mod, "get_settings",
                          lambda: SimpleNamespace(rerank_enabled=False, ...))

  只列了 4~5 个字段。于是**每一个新配置项**都会让「根本没提到这个配置」的测试文件
  集体变红，报错还是 `AttributeError: 'types.SimpleNamespace' object has no attribute
  'graph_recall_selective'` —— 落在与改动看起来毫无关系的位置。这类红最难归因：
  它既不像语法错，也不像断言失败，而像"某人把别的文件改坏了"。

★ 为什么以**类默认值**为底，而不是 `get_settings()` 实例：
  用实例意味着测试依赖 `.env` 与运行环境（本机 / CI / 容器各不相同）。
  那种红只在特定机器上出现，且改一下环境变量就消失 —— 是最难复现的一类。
  类默认值则是**代码里写死的常量**：同一份源码在任何机器上给出同一份假配置。
  代价是它不校验"这个默认值在生产上是否合理"，那不是测试的职责（那是标定手册的事）。

★ 为什么要留一条自检断言：
  这个 helper 本身也会腐烂 —— 若哪天 pydantic 升级改了 `model_fields` / `get_default`
  的行为，或者 `Settings` 换成别的基类，`values` 会**静默变少**，于是又回到
  "读到某个字段就 AttributeError"的老路，而且这次是在 helper 里、更难看出成因。
  断言放在**每次调用**上（71 个字段的集合比较是微秒级），而不是只在模块导入时跑一次：
  它必须对"有人在运行期偷偷改 `Settings.model_fields`"这种事也成立。

★ 为什么还查"未知覆盖键"：
  `fake_settings(graph_enable=True)`（少了一个 d）在纯 dict 更新下会被静默塞进
  假配置里，而 `graph_enabled` 仍是默认值 —— 用例绿了，但它声称覆盖的开关其实没被覆盖。
  这是"补上字段就绿"之外的第二条静默失效路径，所以覆盖键也必须校验。

★ 为什么放在 `tests/fake_settings.py` 而不是某个子目录的 conftest：
  conftest 里的名字只对**该目录及其子目录**的测试可见 —— `tests/wiki/` 读不到
  `tests/retrieve/conftest.py`。而"造一个假配置"是全仓共性需求：把 helper 关在
  子目录 conftest 里，等于把它的可用范围绑死在那个子目录上，别的目录只好继续手写
  `SimpleNamespace(只列几个字段)`，腐烂面反而扩大。做成普通模块后
  `from tests.fake_settings import fake_settings` 处处可用。
  `tests/retrieve/conftest.py` 保留为一行 re-export 的兼容层（历史 import 不用改）。

★ 迁移现状（U4-15 已收口，2026-11）：
  原先用「整体替换 `get_settings` + 只列部分字段」写法的 13 个测试文件、18 处调用，
  现已**全部**改为 `fake_settings(...)`，仓库里不再有这一形态的 settings 替身。
  「为什么以类默认值为底」「为什么留自检断言」两条在迁移后仍然成立：前者决定
  同一份源码在任何机器上给同一份假配置，后者保证 helper 自己也不会静默腐烂。
  仍有两处 `SimpleNamespace(generator_model=..., prompt_version=...)` 保留原样 ——
  那是 `current_wiki_profile()` 的替身，不是 settings，不归本 helper 管。
  另有 5 个文件用自定义 `_FakeSettings` / `_S` / `_settings()` 闭包做同类替身
  （形态不同，不在 U4-15 原文范围内）：它们同样只覆盖个别字段，`retrieve/`、
  `llm/` 等模块再加配置读取时会以同样的方式变红，后续可一并迁到本 helper。
"""
from __future__ import annotations

from app.config.settings import Settings


def fake_settings(**overrides: object) -> Settings:
    """按 `Settings` 的类默认值造一个配置对象，再用 `overrides` 覆盖个别字段。

    用法（覆盖的字段就是这条用例**声明**它关心的配置，其余一律取默认值）::

        monkeypatch.setattr(
            retrieve_mod, "get_settings",
            lambda: fake_settings(graph_enabled=True, rerank_enabled=False),
        )

    ★ 返回的是**真的 `Settings` 实例**（`model_construct` 跳过环境读取与校验），
      不是 `SimpleNamespace`：生产代码若对 settings 调用 `model_copy` /
      `model_dump` / 任何 `Settings` 上的方法，替身也跟得上。用 `SimpleNamespace`
      的话，那些调用会以一个看起来像"业务逻辑坏了"的 `AttributeError` 暴露在测试里。
    """
    unknown = sorted(set(overrides) - set(Settings.model_fields))
    assert not unknown, (
        f"这些覆盖键不在 Settings 里（拼错或字段已改名）：{unknown}。"
        "静默忽略它们会让用例失去它声称的覆盖 —— 宁可现在红"
    )

    values = {
        name: field.get_default(call_default_factory=True)
        for name, field in Settings.model_fields.items()
    }
    values.update(overrides)

    settings = Settings.model_construct(**values)

    # 自检：`set(fake_settings().__dict__) == set(Settings.model_fields)`
    assert set(settings.__dict__) == set(Settings.model_fields), (
        "假 settings 的字段集与 Settings.model_fields 不一致 —— helper 已腐烂："
        f"多 {sorted(set(settings.__dict__) - set(Settings.model_fields))}，"
        f"少 {sorted(set(Settings.model_fields) - set(settings.__dict__))}"
    )
    return settings
