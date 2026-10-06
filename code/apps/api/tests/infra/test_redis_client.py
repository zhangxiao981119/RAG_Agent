"""共享 Redis 客户端单测（§4.23 连接池化）。

**不连真实 Redis** —— 这里验证的是两个契约：

  1. `get_redis()` 进程内单例（否则等于每次新建池，池化白做）
  2. `aclose()` 是 **no-op**（否则请求级调用会把进程级连接池关掉）

第 2 条尤其关键：它错了在功能测试里完全看不出来 ——
单请求跑一遍「建池 → 用 → 关池」是正常的，只有在并发下才会暴露
（前一个请求把池关了，后一个请求拿到的是关闭状态）。

所以必须在这里钉住。
"""
from __future__ import annotations

import asyncio

import pytest

from app.infra import redis_client


@pytest.fixture(autouse=True)
def _reset_client():
    """每个用例前后清掉单例引用，避免相互污染。"""
    redis_client.reset_for_tests()
    yield
    redis_client.reset_for_tests()


@pytest.mark.asyncio
async def test_get_redis_is_singleton():
    a = await redis_client.get_redis()
    b = await redis_client.get_redis()
    assert a is b, "同一进程内 MUST 返回同一实例，否则连接池复用失效"


@pytest.mark.asyncio
async def test_aclose_does_not_destroy_shared_pool():
    """★ 核心契约：调用方 `aclose()` MUST NOT 关掉共享连接池。"""
    first = await redis_client.get_redis()
    await first.aclose()
    again = await redis_client.get_redis()
    assert again is first, "aclose 后 MUST 仍返回同一实例（池未被销毁）"


@pytest.mark.asyncio
async def test_close_is_also_noop():
    """同步风格的 `close()` 同样 MUST 是 no-op。"""
    first = await redis_client.get_redis()
    await first.close()
    assert await redis_client.get_redis() is first


@pytest.mark.asyncio
async def test_underlying_client_identity_survives_aclose():
    """调用方 aclose 后，底层客户端对象 MUST 未被替换。

    ★ 不检查 `connection_pool._closed` —— 那是 redis-py 的内部标志，
      语义是「池未就绪」（新建池即为 True），拿来判断「已被关闭」会误判。
      测外部可观察的行为：对象身份不变、池对象仍在。
    """
    wrapper = await redis_client.get_redis()
    underlying = wrapper._client
    pool_before = underlying.connection_pool

    await wrapper.aclose()

    assert wrapper._client is underlying, "底层客户端被替换了"
    assert underlying.connection_pool is pool_before, "底层连接池被替换了"
    assert await redis_client.get_redis() is wrapper


@pytest.mark.asyncio
async def test_wrapper_forwards_attributes():
    """wrapper MUST 透明转发 —— 否则所有 redis 命令都不可用。"""
    wrapper = await redis_client.get_redis()
    for name in ("incr", "get", "set", "setex", "delete", "expire", "pipeline"):
        assert hasattr(wrapper, name), f"wrapper 未转发 {name}"


@pytest.mark.asyncio
async def test_concurrent_first_calls_create_one_client():
    """并发首次调用 MUST 只建一个客户端（双检锁 + asyncio.Lock）。

    不测这条的话，冷启动瞬间的并发请求会各建一个池 ——
    恰恰是最需要池化的时刻建出最多的池。
    """
    results = await asyncio.gather(*[redis_client.get_redis() for _ in range(20)])
    assert len({id(r) for r in results}) == 1


@pytest.mark.asyncio
async def test_close_redis_then_get_creates_new_instance():
    """`close_redis()` 后再次获取 MUST 得到新实例（应用重启路径）。"""
    first = await redis_client.get_redis()
    await redis_client.close_redis()
    second = await redis_client.get_redis()
    assert second is not first


def test_settings_has_pool_knobs():
    """连接池相关配置必须存在 —— 否则池上限无从设置。"""
    from app.config.settings import get_settings

    settings = get_settings()
    assert settings.redis_max_connections > 0
    assert settings.redis_socket_timeout_s > 0
