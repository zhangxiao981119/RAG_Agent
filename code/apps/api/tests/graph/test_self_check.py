"""启动自检 C1~C8 的 fail-closed 回归测试。

★ 为什么这些断言值得单独钉：
  「配了却不生效」比「起不来」危险得多 —— 本项目吃过一次「只重建 api 镜像、
  worker 静默跑旧代码、任务照常返回成功」的亏。这里的每一条都是把
  「静默降级」变成「进程退出」。

★ 覆盖边界（诚实标注）：C3/C4/C5（表存在 / 存储可达 / wiki_pages）需要真实数据库，
  本机 Docker 不可用 → **未验证**。这里只能钉住「顺序」：C1/C2 在查库之前就拒绝，
  所以它们的拒绝路径不需要 DB 也是真实可测的。
"""
from __future__ import annotations

import pytest

from app.config import self_check_ext
from app.config.settings import get_settings


@pytest.fixture
def env(monkeypatch):
    get_settings.cache_clear()
    yield monkeypatch
    get_settings.cache_clear()
    monkeypatch.undo()


@pytest.fixture
def no_db(monkeypatch):
    """任何数据库访问都立即失败（用来证明「关闭态不查库」）。"""

    class _Boom:
        def __call__(self, *args, **kwargs):
            raise AssertionError("关闭能力时 MUST NOT 建立数据库会话")

    monkeypatch.setattr(self_check_ext, "SessionLocal", _Boom())
    return monkeypatch


@pytest.mark.asyncio
async def test_disabled_capabilities_skip_all_db_checks(env, no_db):
    env.setenv("GRAPH_ENABLED", "false")
    env.setenv("WIKI_ENABLED", "false")
    get_settings.cache_clear()
    # 不抛异常即通过；no_db 保证它没有偷偷查库
    await self_check_ext.check_capabilities()


@pytest.mark.asyncio
async def test_unimplemented_backend_is_rejected(env, no_db):
    env.setenv("GRAPH_ENABLED", "true")
    env.setenv("GRAPH_STORE_BACKEND", "neo4j")
    get_settings.cache_clear()
    with pytest.raises(RuntimeError) as exc:
        await self_check_ext.check_capabilities()
    message = str(exc.value)
    # 文案 MUST 说清「哪里错、为什么不能静默回落」（§12.4）
    assert "[GRAPH_STORE_BACKEND]" in message
    assert "neo4j" in message
    assert "拒绝启动" in message


@pytest.mark.asyncio
async def test_graph_enabled_requires_llm_key(env, no_db):
    env.setenv("GRAPH_ENABLED", "true")
    env.setenv("GRAPH_STORE_BACKEND", "postgres")
    env.setenv("LLM_API_KEY", "")
    get_settings.cache_clear()
    with pytest.raises(RuntimeError) as exc:
        await self_check_ext.check_capabilities()
    assert "[GRAPH_LLM]" in str(exc.value)


@pytest.mark.asyncio
async def test_retrieve_timeout_range_enforced(env, no_db):
    # 关闭态也校验：配错的值不会因为「暂时关着」变成对的
    env.setenv("GRAPH_ENABLED", "false")
    env.setenv("GRAPH_RETRIEVE_TIMEOUT_MS", "10")
    get_settings.cache_clear()
    with pytest.raises(RuntimeError) as exc:
        await self_check_ext.check_capabilities()
    assert "[GRAPH_TIMEOUT]" in str(exc.value)


@pytest.mark.asyncio
async def test_wiki_input_chars_range_enforced(env, no_db):
    env.setenv("WIKI_ENABLED", "false")
    env.setenv("WIKI_INPUT_MAX_CHARS", "100")
    get_settings.cache_clear()
    with pytest.raises(RuntimeError) as exc:
        await self_check_ext.check_capabilities()
    assert "[WIKI_INPUT]" in str(exc.value)


@pytest.mark.asyncio
async def test_extract_timeout_must_be_positive(env, no_db):
    env.setenv("WIKI_ENABLED", "false")
    env.setenv("GRAPH_EXTRACT_TIMEOUT_SECONDS", "0")
    get_settings.cache_clear()
    with pytest.raises(RuntimeError) as exc:
        await self_check_ext.check_capabilities()
    assert "[GRAPH_TIMEOUT]" in str(exc.value)


@pytest.mark.asyncio
async def test_wiki_scope_kb_is_rejected(env):
    """WIKI_SCOPE=kb MUST 拒绝启动（不是「悄悄按 doc 处理」）。

    ★ 本用例**故意不 stub** `_check_wiki_dependencies`：C5 要查 LLM 密钥与 wiki_pages
      表，本机没有数据库。若实现把 C5 排到 C6 前面，这里会先撞上缺表/连接错误，
      断言 `[WIKI_SCOPE]` 就失败 —— 这正是「具体文案必须先命中」的回归保护。
      （配置错误应当先报最具体的那条：否则运维会去补密钥、跑迁移，方向全错。）
    """
    env.setenv("GRAPH_ENABLED", "false")
    env.setenv("WIKI_ENABLED", "true")
    env.setenv("WIKI_SCOPE", "kb")
    env.setenv("LLM_API_KEY", "dummy-key")
    get_settings.cache_clear()

    with pytest.raises(RuntimeError) as exc:
        await self_check_ext.check_capabilities()
    message = str(exc.value)
    assert "[WIKI_SCOPE]" in message
    assert "kb" in message


@pytest.mark.asyncio
async def test_wiki_scope_unknown_value_is_rejected(env):
    env.setenv("GRAPH_ENABLED", "false")
    env.setenv("WIKI_ENABLED", "true")
    env.setenv("WIKI_SCOPE", "department")
    env.setenv("LLM_API_KEY", "dummy-key")
    get_settings.cache_clear()

    with pytest.raises(RuntimeError) as exc:
        await self_check_ext.check_capabilities()
    assert "[WIKI_SCOPE]" in str(exc.value)


@pytest.mark.asyncio
async def test_wiki_scope_checked_before_wiki_dependencies(env, monkeypatch):
    """显式钉住顺序：C6（取值合法性）MUST 先于 C5（外部依赖）。"""
    env.setenv("GRAPH_ENABLED", "false")
    env.setenv("WIKI_ENABLED", "true")
    env.setenv("WIKI_SCOPE", "kb")
    env.setenv("LLM_API_KEY", "dummy-key")
    get_settings.cache_clear()

    called = {"deps": False}

    async def _deps_should_not_run() -> None:
        called["deps"] = True

    monkeypatch.setattr(self_check_ext, "_check_wiki_dependencies", _deps_should_not_run)
    with pytest.raises(RuntimeError):
        await self_check_ext.check_capabilities()
    assert called["deps"] is False, (
        "WIKI_SCOPE 非法时 MUST NOT 先去查 wiki 依赖 —— "
        "否则运维看到的是「缺密钥/缺表」，会往错误方向修"
    )
