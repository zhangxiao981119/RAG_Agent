"""Step 7 S7-7：配额累计的**键口径**回归（不依赖数据库 / Redis）。

要锁死的一件事：租户日/月 token 是**一个租户一个键**，不因应用或场景而分裂。
`_incr_usage` 的键模板里没有 scenario 段，这是设计事实；本文件用记录型假 Redis
把「到底对哪些键加了什么」原样记下来断言，而不是自己再实现一遍累加语义
（那只会让测试绑到假实现上）。

★ 只 monkeypatch `quota_service._get_redis`（模块内唯一 Redis 入口），
  其余逻辑（键模板、TTL、pipeline 顺序）全部走真实代码。
"""
from __future__ import annotations

import uuid

import pytest

from app.services import quota as quota_service

pytestmark = pytest.mark.asyncio


# ══════════════════════════════════════════════════════════════════════
# 记录型假 Redis：只实现 `_incr_usage` 用到的那一面
# ══════════════════════════════════════════════════════════════════════


class _RecordingPipeline:
    """记录 `incrby` / `expire` 调用，`execute` 返回空列表（调用方不读返回值）。"""

    def __init__(self, redis: _RecordingRedis) -> None:
        self._redis = redis

    def incrby(self, key: str, value: int) -> _RecordingPipeline:
        self._redis.values[key] = self._redis.values.get(key, 0) + int(value)
        return self

    def expire(self, key: str, ttl: int) -> _RecordingPipeline:
        self._redis.ttls[key] = int(ttl)
        return self

    async def execute(self) -> list:
        return []


class _RecordingRedis:
    """`values` = 每个键的累计值，`ttls` = 每个键最后一次被设的 TTL。"""

    def __init__(self) -> None:
        self.values: dict[str, int] = {}
        self.ttls: dict[str, int] = {}

    def pipeline(self) -> _RecordingPipeline:
        return _RecordingPipeline(self)

    async def aclose(self) -> None:
        return None


def _install(monkeypatch, redis: _RecordingRedis) -> _RecordingRedis:
    async def _fake_get_redis() -> _RecordingRedis:
        return redis

    monkeypatch.setattr(quota_service, "_get_redis", _fake_get_redis)
    return redis


def _keys_under(redis: _RecordingRedis, prefix: str) -> list[str]:
    """按前缀筛键（键模板以 `{date}` / `{month}` 结尾，传空串即得前缀）。"""
    return sorted(key for key in redis.values if key.startswith(prefix))


# ══════════════════════════════════════════════════════════════════════
# 一、两个应用共用同一个租户日键
# ══════════════════════════════════════════════════════════════════════


async def test_two_applications_share_one_tenant_key(monkeypatch):
    """两个应用（各自绑定不同场景）累加后，租户日键只有一个且值为两者之和。

    ★ 配额键口径不含 scenario：场景只影响解析与检索口径，不参与计费分桶。
      若这里出现两个租户日键，说明有人把场景 id 塞进了租户键，租户日限额会被
      按场景倍数放大（每个场景各自从 0 开始计），而任何单场景用例都看不出来。
    """
    redis = _install(monkeypatch, _RecordingRedis())
    tenant_id = uuid.uuid4()
    user_id = uuid.uuid4()
    app_a = uuid.uuid4()
    app_b = uuid.uuid4()

    await quota_service._incr_usage(tenant_id, user_id, 1000, application_id=app_a)
    await quota_service._incr_usage(tenant_id, user_id, 2500, application_id=app_b)

    tenant_day_prefix = quota_service._K_TENANT_TOKENS_DAY.format(
        tenant_id=tenant_id, date=""
    )
    tenant_month_prefix = quota_service._K_TENANT_TOKENS_MONTH.format(
        tenant_id=tenant_id, month=""
    )
    # 日键前缀是 "quota:tokens:tenant:<id>:"，月键是 "quota:tokens:tenant_month:<id>:"，
    # 前者本不会命中后者；这里显式再排除一次，避免日后有人调整键模板后本用例假绿。
    tenant_day_keys = [
        key
        for key in _keys_under(redis, tenant_day_prefix)
        if not key.startswith(tenant_month_prefix)
    ]
    assert len(tenant_day_keys) == 1, f"租户日 token 被拆成了多个键：{tenant_day_keys}"
    assert redis.values[tenant_day_keys[0]] == 3500

    tenant_month_keys = _keys_under(redis, tenant_month_prefix)
    assert len(tenant_month_keys) == 1, tenant_month_keys
    assert redis.values[tenant_month_keys[0]] == 3500

    user_token_keys = _keys_under(
        redis, quota_service._K_USER_TOKENS.format(user_id=user_id, date="")
    )
    assert len(user_token_keys) == 1 and redis.values[user_token_keys[0]] == 3500

    user_msg_keys = _keys_under(
        redis, quota_service._K_USER_MSG.format(user_id=user_id, date="")
    )
    assert len(user_msg_keys) == 1 and redis.values[user_msg_keys[0]] == 2

    # 配额键里不允许出现 scenario 片段（口径不按场景分键）
    assert all("scenario" not in key for key in redis.values), sorted(redis.values)

    # 应用键各自独立：互不混算
    app_a_token_keys = _keys_under(
        redis, quota_service._K_APP_TOKENS.format(application_id=app_a, date="")
    )
    app_b_token_keys = _keys_under(
        redis, quota_service._K_APP_TOKENS.format(application_id=app_b, date="")
    )
    assert len(app_a_token_keys) == 1 and redis.values[app_a_token_keys[0]] == 1000
    assert len(app_b_token_keys) == 1 and redis.values[app_b_token_keys[0]] == 2500


# ══════════════════════════════════════════════════════════════════════
# 二、同一应用跨两个场景仍只写一个租户日键
# ══════════════════════════════════════════════════════════════════════


async def test_same_application_across_two_scenarios_keeps_one_tenant_key(monkeypatch):
    """同一应用连记两次（两次请求分别走不同场景），租户日键仍只有一个。

    两次各 500 token：租户日键 1000、应用日键 1000、应用消息 2、用户日 token 1000。
    场景在两次调用之间变化，对键集合**零影响** —— 这正是「计费口径与场景解耦」。
    """
    redis = _install(monkeypatch, _RecordingRedis())
    tenant_id = uuid.uuid4()
    user_id = uuid.uuid4()
    application_id = uuid.uuid4()

    await quota_service._incr_usage(tenant_id, user_id, 500, application_id=application_id)
    await quota_service._incr_usage(tenant_id, user_id, 500, application_id=application_id)

    tenant_day_keys = _keys_under(
        redis,
        quota_service._K_TENANT_TOKENS_DAY.format(tenant_id=tenant_id, date=""),
    )
    assert len(tenant_day_keys) == 1, tenant_day_keys
    assert redis.values[tenant_day_keys[0]] == 1000

    app_token_keys = _keys_under(
        redis,
        quota_service._K_APP_TOKENS.format(application_id=application_id, date=""),
    )
    assert len(app_token_keys) == 1 and redis.values[app_token_keys[0]] == 1000

    app_msg_keys = _keys_under(
        redis,
        quota_service._K_APP_MSG.format(application_id=application_id, date=""),
    )
    assert len(app_msg_keys) == 1 and redis.values[app_msg_keys[0]] == 2

    user_token_keys = _keys_under(
        redis, quota_service._K_USER_TOKENS.format(user_id=user_id, date="")
    )
    assert len(user_token_keys) == 1 and redis.values[user_token_keys[0]] == 1000


# ══════════════════════════════════════════════════════════════════════
# 三、应用级日键仍按应用各自计数
# ══════════════════════════════════════════════════════════════════════


async def test_application_daily_keys_stay_per_application(monkeypatch):
    """应用日键按应用各自计（token 与消息次数都不混算）。"""
    redis = _install(monkeypatch, _RecordingRedis())
    tenant_id = uuid.uuid4()
    user_id = uuid.uuid4()
    app_a = uuid.uuid4()
    app_b = uuid.uuid4()

    await quota_service._incr_usage(tenant_id, user_id, 700, application_id=app_a)
    await quota_service._incr_usage(tenant_id, user_id, 1300, application_id=app_b)

    expected = {app_a: 700, app_b: 1300}
    for application_id, tokens in expected.items():
        token_keys = _keys_under(
            redis,
            quota_service._K_APP_TOKENS.format(application_id=application_id, date=""),
        )
        assert len(token_keys) == 1, token_keys
        assert redis.values[token_keys[0]] == tokens

        msg_keys = _keys_under(
            redis,
            quota_service._K_APP_MSG.format(application_id=application_id, date=""),
        )
        assert len(msg_keys) == 1, msg_keys
        assert redis.values[msg_keys[0]] == 1
